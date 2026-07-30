"""GPT yozgan Python kodini izolyatsiya qilingan holda bajaradi.

Bu modul FAQAT kod bajarish bilan shug'ullanadi — Telegram yoki OpenAI
haqida hech narsa bilmaydi. Chaqiruvchi (services.py) kodni va (bo'lsa)
kirish faylini beradi, natijada muvaffaqiyat/xato va yaratilgan fayllar
ro'yxatini oladi.

IZOLYATSIYA (nima himoyalangan va nima yo'q — ochiq-oydin):
  ✓ Muhit o'zgaruvchilari TOZALANADI — bola-jarayon BOT_TOKEN,
    OPENAI_API_KEY, DATABASE_URL kabi sirlarni umuman ko'rmaydi.
  ✓ Vaqtinchalik papka — har bajarilishda yangi, oxirida butunlay
    o'chiriladi. Kod faqat shu papka ichida ishlaydi (cwd).
  ✓ 60 soniya timeout — cheksiz sikl butun jarayon guruhi bilan
    o'ldiriladi.
  ✓ CPU / xotira / fayl hajmi / jarayon soni chegaralari (Linux'da
    RLIMIT orqali; Windows'da bu chegaralar ishlamaydi, faqat timeout).
  ✓ `python -I` (izolyatsiya rejimi) — PYTHONPATH va foydalanuvchi
    site-packages e'tiborga olinmaydi.
  ✗ Tarmoq BLOKLANMAGAN. Haqiqiy konteyner/namespace izolyatsiyasisiz
    (Railway'da Docker-in-Docker mavjud emas) buni ta'minlab bo'lmaydi.
    Xavf cheklangan, chunki muhit tozalangani uchun o'g'irlanadigan
    sir yo'q, timeout esa suiiste'molni 60 soniya bilan cheklaydi.
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

SANDBOX_TIMEOUT = 60           # soniya — kod bajarilishining qattiq chegarasi
MAX_OUTPUT_FILES = 10          # bittada qaytariladigan fayllar soni chegarasi
MAX_OUTPUT_FILE_SIZE = 45 * 1024 * 1024   # Telegram 50 MB — biroz zaxira bilan

try:
    import resource  # POSIX-only (Railway = Linux). Windows'da yo'q.
    _HAS_RESOURCE = True
except ImportError:
    _HAS_RESOURCE = False


@dataclass
class SandboxResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    traceback: str = ""
    output_files: List[Tuple[str, bytes]] = field(default_factory=list)


def _build_child_env(work_dir: Path) -> dict:
    """Bola-jarayon uchun MINIMAL muhit — barcha sirlar olib tashlanadi.

    Bu eng muhim himoya qatlami: BOT_TOKEN / OPENAI_API_KEY /
    DATABASE_URL kabi qiymatlar bola-jarayonga umuman uzatilmaydi,
    shuning uchun GPT yozgan kod (hatto prompt-injection natijasida
    ataylab yomon niyat bilan yozilgan bo'lsa ham) ularni o'qiy olmaydi.

    HOME/MPLCONFIGDIR vaqtinchalik papkaga yo'naltiriladi: matplotlib va
    boshqa kutubxonalar ishga tushganda yoziladigan config/kesh papkasini
    talab qiladi, uni tashqarida emas, shu yerda ushlab turamiz (papka
    oxirida butunlay o'chiriladi).
    """
    cache_dir = work_dir / ".cache"
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",   # matplotlib ekransiz (headless) muhitda ishlashi uchun
        "MPLCONFIGDIR": str(cache_dir / "mpl"),
        "XDG_CACHE_HOME": str(cache_dir),
        "XDG_CONFIG_HOME": str(cache_dir),
        "HOME": str(work_dir),
        "TMPDIR": str(work_dir),
    }
    if os.name == "nt":  # mahalliy Windows'da sinash uchun
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        env["USERPROFILE"] = str(work_dir)
        env["TEMP"] = str(work_dir)
        env["TMP"] = str(work_dir)
    return env


def _apply_limits() -> None:
    """Bola-jarayonda exec'dan OLDIN chaqiriladi (faqat POSIX)."""
    # CPU: 55s yumshoq / 60s qattiq — timeout bilan bir xil tartibda.
    resource.setrlimit(resource.RLIMIT_CPU, (55, 60))
    # Virtual xotira: 2 GB. pandas/numpy ko'p manzil maydonini band qiladi,
    # shuning uchun bundan pastga tushirmaslik kerak.
    resource.setrlimit(resource.RLIMIT_AS, (2048 * 1024 * 1024, 2048 * 1024 * 1024))
    # Yaratiladigan fayl hajmi: 100 MB.
    resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024, 100 * 1024 * 1024))
    # Fork bombasidan himoya.
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    except (ValueError, OSError):
        pass
    # Yangi jarayon guruhi — timeoutda butun daraxtni o'ldirish uchun.
    os.setsid()


def _collect_output_files(output_dir: Path) -> Tuple[List[Tuple[str, bytes]], List[str]]:
    """output/ papkasidagi fayllarni o'qiydi.

    Qaytaradi: (fayllar, ogohlantirishlar). Ogohlantirishlar GPT'ga
    qaytariladi, shunda u masalan faylni kichraytirishga urinishi mumkin.
    """
    files: List[Tuple[str, bytes]] = []
    warnings: List[str] = []

    if not output_dir.is_dir():
        return files, warnings

    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        if len(files) >= MAX_OUTPUT_FILES:
            warnings.append(f"{MAX_OUTPUT_FILES} tadan ortiq fayl yaratildi — qolganlari tashlab yuborildi.")
            break
        size = path.stat().st_size
        if size > MAX_OUTPUT_FILE_SIZE:
            warnings.append(f"'{path.name}' juda katta ({size // (1024 * 1024)} MB) — yuborilmadi.")
            continue
        try:
            files.append((path.name, path.read_bytes()))
        except Exception as e:
            warnings.append(f"'{path.name}' o'qib bo'lmadi: {e}")

    return files, warnings


async def run_in_sandbox(
    code: str,
    input_file_bytes: Optional[bytes] = None,
    input_filename: Optional[str] = None,
) -> SandboxResult:
    """Kodni vaqtinchalik, tozalangan muhitda bajaradi.

    Kirish fayli (bo'lsa) ish papkasida `input.<kengaytma>` nomi bilan
    turadi. Kod `output/` papkasiga yozgan barcha fayllar o'qib
    qaytariladi. Papka har qanday holatda (xato/timeout ham) o'chiriladi.
    """
    if not code or not code.strip():
        return SandboxResult(success=False, traceback="Bo'sh kod yuborildi.")

    tmp_dir = tempfile.mkdtemp(prefix="filetask_")
    work_dir = Path(tmp_dir)
    output_dir = work_dir / "output"

    try:
        output_dir.mkdir(exist_ok=True)
        (work_dir / ".cache").mkdir(exist_ok=True)

        if input_file_bytes is not None and input_filename:
            ext = input_filename.rsplit(".", 1)[-1].lower() if "." in input_filename else "bin"
            # Kengaytmani tozalaymiz — yo'l bilan o'ynashning oldini olish uchun.
            ext = "".join(c for c in ext if c.isalnum())[:10] or "bin"
            (work_dir / f"input.{ext}").write_bytes(input_file_bytes)

        (work_dir / "script.py").write_text(code, encoding="utf-8")

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "script.py",
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_child_env(work_dir),
            preexec_fn=_apply_limits if _HAS_RESOURCE else None,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=SANDBOX_TIMEOUT)
        except asyncio.TimeoutError:
            _kill_process_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            return SandboxResult(
                success=False,
                traceback=(
                    f"TIMEOUT: kod {SANDBOX_TIMEOUT} soniyada tugamadi va to'xtatildi. "
                    "Kodni sezilarli tezlashtiring (masalan butun faylni emas, "
                    "faqat kerakli qismini qayta ishlang)."
                ),
            )

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")

        if proc.returncode != 0:
            logger.info(f"[Sandbox] kod xato bilan tugadi (exit={proc.returncode})")
            return SandboxResult(
                success=False,
                stdout=stdout[:2000],
                stderr=stderr[:3000],
                traceback=(stderr or stdout or "Noma'lum xatolik")[:3000],
            )

        files, warnings = _collect_output_files(output_dir)
        combined_stdout = stdout[:2000]
        if warnings:
            combined_stdout += "\n[OGOHLANTIRISH] " + " ".join(warnings)

        logger.info(f"[Sandbox] muvaffaqiyat, {len(files)} ta fayl yaratildi")
        return SandboxResult(success=True, stdout=combined_stdout, output_files=files)

    except Exception as e:
        logger.error(f"[Sandbox] kutilmagan xatolik: {e}")
        return SandboxResult(success=False, traceback=f"Sandbox xatosi: {e}"[:2000])

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _kill_process_tree(proc) -> None:
    """Timeoutda butun jarayon guruhini o'ldiradi (bola jarayonlar bilan)."""
    try:
        if _HAS_RESOURCE:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    async def _demo():
        """Qo'lda ishga tushiriladigan tekshiruv: `python sandbox.py`"""
        # 1) Oddiy fayl yaratish
        r = await run_in_sandbox(
            "with open('output/result.txt', 'w', encoding='utf-8') as f:\n"
            "    f.write('salom sandbox')\n"
            "print('tayyor')\n"
        )
        assert r.success, f"kutilmagan xato: {r.traceback}"
        assert r.output_files, "output_files bo'sh bo'lmasligi kerak"
        assert r.output_files[0][0] == "result.txt", r.output_files[0][0]
        assert r.output_files[0][1] == "salom sandbox".encode(), r.output_files[0][1]
        assert "tayyor" in r.stdout, r.stdout

        # 2) Xato kod -> success=False, traceback ichida sabab
        r = await run_in_sandbox("raise ValueError('ataylab xato')")
        assert not r.success, "xato kod success=True qaytarmasligi kerak"
        assert "ataylab xato" in r.traceback, r.traceback

        # 3) Kirish fayli ko'rinishi kerak
        r = await run_in_sandbox(
            "data = open('input.txt', encoding='utf-8').read()\n"
            "open('output/echo.txt', 'w', encoding='utf-8').write(data.upper())\n",
            input_file_bytes=b"salom",
            input_filename="test.txt",
        )
        assert r.success, r.traceback
        assert r.output_files[0][1] == b"SALOM", r.output_files[0]

        # 4) Sirlar bola-jarayonga o'tmasligi kerak
        os.environ["SANDBOX_SECRET_PROBE"] = "maxfiy_qiymat"
        r = await run_in_sandbox(
            "import os\n"
            "open('output/env.txt', 'w').write(os.environ.get('SANDBOX_SECRET_PROBE', 'YOQ'))\n"
        )
        assert r.success, r.traceback
        assert r.output_files[0][1] == b"YOQ", "muhit o'zgaruvchisi sizib chiqdi!"

        # 5) Bo'sh kod
        r = await run_in_sandbox("")
        assert not r.success

        print("sandbox.py: barcha tekshiruvlar o'tdi.")

    asyncio.run(_demo())

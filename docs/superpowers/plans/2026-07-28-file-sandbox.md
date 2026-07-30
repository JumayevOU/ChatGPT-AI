# File Sandbox (Advanced Data Analysis) Implementation Plan

> **HOLAT: BAJARILDI (2026-07-28).** Bu reja tarixiy hujjat sifatida
> saqlanadi. Amalda ikkita og'ish bo'ldi:
>
> 1. **E2B ishlatilmadi** — kod bot konteynerining o'zida, tozalangan
>    muhit + RLIMIT chegaralari bilan bajariladi (`sandbox.py`). Sabab va
>    xavfsizlik tahlili spec faylining boshida. Shu sababli Task 1'dagi
>    `E2B_API_KEY`/`e2b` paketi va butun Task 7 (E2B template) BEKOR
>    QILINDI — o'rniga `requirements.txt`ga fayl kutubxonalari
>    (openpyxl, pandas, reportlab, matplotlib va h.k.) qo'shildi.
> 2. **Testlar rejadagidan ko'proq** — `test_file_task_loop.py` qo'shildi
>    (tool-loop dispatch, kvota, qayta urinish, guest-rejim gating va
>    internet_search regressiyasi uchun 7 ta ssenariy).
>
> Yakuniy holat uchun kodning o'ziga qarang: `sandbox.py`,
> `file_task_quota.py`, `services.py`, `handlers_messages.py`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the bot the ability to edit uploaded files and generate brand-new files (Excel, Word, PDF, CSV, ZIP, etc.) by having GPT write Python code that runs in an isolated E2B sandbox on our own infrastructure — OpenAI never executes code or sees the file after upload.

**Architecture:** Extend the existing `internet_search` function-calling tool-loop in `services.py` (used by `get_openai_reply`) with a second tool, `run_python_sandbox`. When GPT calls it, the code is executed via a new `sandbox.py` module (E2B SDK), output files are collected in-memory, and the calling handler in `handlers_messages.py` sends them back to the user as Telegram documents after the text reply completes. Quota is charged once per user turn via a small isolated `file_task_quota.py` helper, refunded if every attempt fails.

**Tech Stack:** Python 3.11, aiogram 3.29, OpenAI Responses API (existing), E2B Python SDK (new), asyncpg (existing, via `database.py`).

## Global Constraints

- OpenAI must never execute code or receive the file after the initial prompt — it only sees a text preview/prompt and returns generated Python code as a tool call. (Spec requirement: "OpenAI hech qanday kod bajarmasin.")
- Sandbox execution happens only via `sandbox.py` → E2B; no other code path may shell out to run GPT-generated code.
- This feature is available to **all users** (not premium-gated), cost-controlled via a new quota price: `MESSAGE_COST_FILE_TASK = 250`.
- Sandbox timeout: 60 seconds. No network access inside the sandbox (E2B default — do not add network access).
- Max 3 tool-calling rounds per user turn (existing `MAX_TOOL_ROUNDS = 3` in `services.py`, shared with `internet_search` — do not raise it).
- **Out of scope for this plan** (explicitly, not an oversight): guest mode (`guest_handlers.py`) does not get this tool — guest replies structurally cannot attach documents (`caller_chat_id` is always `None`, confirmed by production logs in an earlier debugging session), so offering the tool there would burn sandbox executions that can never be delivered. Photo captions (`handle_photo` / `get_vision_reply`) are also out of scope — `get_vision_reply` has no tool-calling loop today and wiring one up is a separate, non-trivial change.

## Deviations from the approved spec

The spec (`docs/superpowers/specs/2026-07-28-file-sandbox-design.md`) is the source of intent; these are implementation-level simplifications made while turning it into tasks, kept because they satisfy the same requirement with less new code:

- **No per-format "preview" table.** The spec's Komponentlar §2 proposed building a bespoke preview (sheet names, first rows, etc.) per file format. This plan instead reuses the *existing* `extract_text_from_document()` output for the prompt (unchanged behavior for plain read/summarize requests) and relies on GPT being able to call `run_python_sandbox` more than once per turn (already required for error-retry) to write cheap "inspect the file" code first if the extracted preview isn't enough. This also happens to fix the original bug report on its own: even when `extract_text_from_document` produces garbage for a binary `.xls` (it does — see Task 8 Step 2's note), GPT still has the raw bytes available inside the sandbox and can open the file properly with `openpyxl`/`pandas` there, independent of the preview quality.
- **`output_files` list instead of a `[FILE]<path>` stream sentinel.** The spec proposed signaling produced files through the text-chunk stream. This plan instead has the caller pass in a plain Python list that the generator `.extend()`s with `(filename, bytes)` tuples as a side channel — avoids encoding binary content into a string-only stream and avoids any change to `process_stream_draft`'s return type or internals.
- **Two already-existing guards satisfy two of the spec's Xavfsizlik bullet points, no new code needed:**
  - "Kirish fayli hajmi (20MB) chegarasi" — `handle_document` already rejects anything over 5MB (`handlers_messages.py:862`), which is stricter than the spec's suggested ceiling.
  - "Bir userga bir vaqtda 1 ta faol sandbox" — the existing `GeneratingState` FSM (set via `state.set_state(GeneratingState.generating)` in every handler, guarded by a busy-handler registered before the normal routers in `main.py`) already blocks a user from starting a second request while one is in flight, which caps sandbox concurrency per user at 1 as a side effect.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `sandbox.py` | new | Talks to E2B only. Runs one script, returns stdout/stderr/output files. No Telegram/OpenAI knowledge. |
| `file_task_quota.py` | new | Charge-once / refund-if-all-attempts-failed bookkeeping for the file-task quota price. No E2B/OpenAI knowledge. |
| `test_file_task_quota.py` | new | Manual assert-based test for `file_task_quota.py` (same style as `test_refund_quota.py`). |
| `sandbox_template/e2b.Dockerfile` | new | Defines the pre-installed library set for the E2B sandbox image (no internet access at runtime, so everything must be baked in at build time). |
| `sandbox_template/e2b.toml` | new | E2B template config (template name referenced by `sandbox.py`). |
| `services.py` | modified | New tool schema, tool-dispatch branch, new keyword params on `get_openai_reply`/`get_gpt_reply`. |
| `config.py` | modified | `E2B_API_KEY`, `MESSAGE_COST_FILE_TASK`. |
| `handlers_messages.py` | modified | New `"file_task"` status/emoji entries, `process_stream_draft` recognizes the new status, `handle_text`/`handle_document`/`handle_voice` pass the new params and send back any output files. |
| `.env` | modified (manual) | Add `E2B_API_KEY=...` line. |
| `requirements.txt` | modified | Add `e2b`. |

---

### Task 1: Dependencies, env var, quota price

**Files:**
- Modify: `requirements.txt`
- Modify: `config.py`
- Modify: `.env` (manual — no secret value can be written by an automated step)

**Interfaces:**
- Produces: `config.E2B_API_KEY: Optional[str]`, `config.MESSAGE_COST_FILE_TASK: int`

- [ ] **Step 1: Add the E2B SDK to `requirements.txt`**

Add a new line (alphabetical position not required — this file isn't sorted):

```
e2b
```

- [ ] **Step 2: Install it locally so later tasks can import it**

Run: `pip install e2b`

- [ ] **Step 3: Add `E2B_API_KEY` next to the other env vars in `config.py`**

In `config.py`, right after the existing `OPENAI_BASE_URL` line (around line 19):

```python
E2B_API_KEY: Optional[str] = os.getenv("E2B_API_KEY")  # sandbox.py uchun — https://e2b.dev
```

Do **not** add it to `_REQUIRED_ENV_VARS` — the bot must still start and work normally (minus the file-sandbox tool) if this key is missing, matching how the rest of the bot degrades gracefully when optional integrations aren't configured.

- [ ] **Step 4: Add the new quota price next to the other `MESSAGE_COST_*` constants**

In `config.py`, right after `MESSAGE_COST_VOICE` (around line 449):

```python
MESSAGE_COST_FILE_TASK: int = 250   # fayl tahrirlash/yaratish (GPT tool-loop + sandbox) — photo'dan qimmatroq
```

- [ ] **Step 5: Add the placeholder line to `.env`**

Append to `.env` (the real value must be filled in by hand from an E2B account — this step only adds the line so `os.getenv` has something to find once it's filled in):

```
E2B_API_KEY=
```

- [ ] **Step 6: Commit**

```bash
git add requirements.txt config.py
git commit -m "Add E2B dependency, API key setting, and file-task quota price"
```

(`.env` is expected to already be gitignored — do not force-add it. If `git status` shows it as untracked/ignored, that's correct; skip adding it.)

---

### Task 2: `sandbox.py` — E2B execution wrapper

**Files:**
- Create: `sandbox.py`
- Test: manual self-check inside `sandbox.py`'s `if __name__ == "__main__":` block (needs a real `E2B_API_KEY` and a built template — cannot run in CI, matches this repo's existing `test_refund_quota.py` "manual, no framework" convention)

**Interfaces:**
- Produces: `SandboxResult` dataclass (`success: bool`, `stdout: str`, `stderr: str`, `traceback: str`, `output_files: list[tuple[str, bytes]]`), `async def run_in_sandbox(code: str, input_file_bytes: bytes | None = None, input_filename: str | None = None) -> SandboxResult`

- [ ] **Step 1: Write `sandbox.py`**

```python
"""E2B sandbox ichida GPT yozgan Python kodini bajaradi.

Bu modul FAQAT E2B bilan gaplashadi — Telegram yoki OpenAI haqida
hech narsa bilmaydi. Chaqiruvchi (services.py) kodni va (bo'lsa)
kirish faylini beradi, natijada muvaffaqiyat/xato va yaratilgan
fayllar ro'yxatini oladi.

DIQQAT: quyidagi E2B SDK chaqiruvlari (AsyncSandbox.create, files.write,
files.read, commands.run) joriy E2B hujjatiga (https://e2b.dev/docs)
qarab implementatsiya paytida tasdiqlanishi kerak — SDK versiyalari
orasida metod imzolari o'zgargan bo'lishi mumkin.
"""
from dataclasses import dataclass, field
from typing import Optional

from e2b import AsyncSandbox

from loader import logger
from config import E2B_API_KEY

E2B_TEMPLATE = "file-sandbox"  # sandbox_template/ orqali qurilgan custom template nomi
SANDBOX_TIMEOUT = 60  # soniya


@dataclass
class SandboxResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    traceback: str = ""
    output_files: list[tuple[str, bytes]] = field(default_factory=list)


async def run_in_sandbox(
    code: str,
    input_file_bytes: Optional[bytes] = None,
    input_filename: Optional[str] = None,
) -> SandboxResult:
    """GPT yozgan kodni izolyatsiya qilingan E2B konteynerida bajaradi.

    Kirish fayli (bo'lsa) /work/input.<kengaytma> sifatida yoziladi.
    Kod /work/output/ papkasiga yozgan barcha fayllar o'qib qaytariladi.
    """
    if not E2B_API_KEY:
        return SandboxResult(success=False, traceback="E2B_API_KEY sozlanmagan (.env).")

    sbx = None
    try:
        sbx = await AsyncSandbox.create(
            template=E2B_TEMPLATE, api_key=E2B_API_KEY, timeout=SANDBOX_TIMEOUT
        )

        if input_file_bytes is not None and input_filename:
            ext = input_filename.rsplit(".", 1)[-1] if "." in input_filename else "bin"
            await sbx.files.write(f"/work/input.{ext}", input_file_bytes)

        await sbx.files.write("/work/script.py", code)

        result = await sbx.commands.run(
            "mkdir -p /work/output && python /work/script.py",
            cwd="/work",
            timeout=SANDBOX_TIMEOUT,
        )

        if result.exit_code != 0:
            error_text = (result.stderr or result.stdout or "Noma'lum xatolik")[:4000]
            logger.info(f"[Sandbox] kod xato bilan tugadi (exit={result.exit_code}): {error_text[:300]}")
            return SandboxResult(
                success=False,
                stdout=(result.stdout or "")[:2000],
                stderr=(result.stderr or "")[:2000],
                traceback=error_text,
            )

        list_result = await sbx.commands.run("ls -1 /work/output", timeout=10)
        filenames = [f.strip() for f in (list_result.stdout or "").splitlines() if f.strip()]

        output_files: list[tuple[str, bytes]] = []
        for fname in filenames:
            content = await sbx.files.read(f"/work/output/{fname}")
            if isinstance(content, str):
                content = content.encode("utf-8")
            output_files.append((fname, content))

        return SandboxResult(success=True, stdout=(result.stdout or "")[:2000], output_files=output_files)

    except Exception as e:
        logger.error(f"[Sandbox] xatolik: {e}")
        return SandboxResult(success=False, traceback=str(e)[:2000])
    finally:
        if sbx is not None:
            try:
                await sbx.kill()
            except Exception:
                pass


if __name__ == "__main__":
    import asyncio

    async def _demo():
        """Qo'lda ishga tushiriladigan tekshiruv: `python sandbox.py`
        E2B_API_KEY .env'da to'ldirilgan va Task 8'dagi template
        qurilgan bo'lishi kerak."""
        result = await run_in_sandbox(
            code=(
                "with open('/work/output/result.txt', 'w') as f:\n"
                "    f.write('salom, sandbox ishlayapti')\n"
                "print('tayyor')\n"
            )
        )
        assert result.success, f"kutilmagan xato: {result.traceback}"
        assert result.output_files, "output_files bo'sh bo'lmasligi kerak"
        name, content = result.output_files[0]
        assert name == "result.txt", f"kutilgan result.txt, keldi {name}"
        assert content == b"salom, sandbox ishlayapti", f"mazmun mos emas: {content!r}"

        bad_result = await run_in_sandbox(code="raise ValueError('ataylab xato')")
        assert not bad_result.success, "xato kod success=True qaytarmasligi kerak"
        assert "ataylab xato" in bad_result.traceback, "traceback xato matnini o'z ichiga olishi kerak"

        print("sandbox.py: barcha tekshiruvlar o'tdi.")

    asyncio.run(_demo())
```

- [ ] **Step 2: Verify E2B SDK method names against current docs**

Before running the demo, open https://e2b.dev/docs (Python SDK reference) and confirm `AsyncSandbox.create(...)`, `sbx.files.write(...)`, `sbx.files.read(...)`, `sbx.commands.run(...)`, `sbx.kill()` still match this signature. Fix any mismatches in the file above before proceeding — do not skip this, the SDK has changed shape across versions.

- [ ] **Step 3: Run the demo (after Task 8's template exists and `.env` has a real `E2B_API_KEY`)**

Run: `python sandbox.py`
Expected: `sandbox.py: barcha tekshiruvlar o'tdi.` printed, no assertion errors.

(If Task 8 isn't done yet, skip running this now and come back to it — the code review for this task can still proceed on the strength of the code + step 2's doc-check.)

- [ ] **Step 4: Commit**

```bash
git add sandbox.py
git commit -m "Add sandbox.py — E2B-based execution of GPT-generated Python code"
```

---

### Task 3: `file_task_quota.py` — charge-once / refund-if-failed

**Files:**
- Create: `file_task_quota.py`
- Test: `test_file_task_quota.py`

**Interfaces:**
- Consumes: `database.check_and_consume_quota(user_id: int, cost: int) -> dict`, `database.refund_quota(user_id: int, cost: int) -> None` (both already exist, confirmed at `database.py:829` and `database.py:863`)
- Produces: `class FileTaskQuota` with `__init__(self, user_id: int, cost: int)`, `async def ensure_charged(self) -> bool`, `def mark_success(self) -> None`, `async def refund_if_unused(self) -> None`

This is pulled out of the tool-dispatch loop into its own tiny module specifically so it can be unit-tested without dragging in the OpenAI streaming machinery `services.py` initializes at import time.

- [ ] **Step 1: Write the failing test**

Create `test_file_task_quota.py`:

```python
"""file_task_quota.FileTaskQuota uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python test_file_task_quota.py
"""
import asyncio

import file_task_quota


class FakeQuota:
    def __init__(self, allowed=True, unlimited=False):
        self.allowed = allowed
        self.unlimited = unlimited
        self.charge_calls = []
        self.refund_calls = []

    async def check_and_consume_quota(self, user_id, cost):
        self.charge_calls.append((user_id, cost))
        return {"allowed": self.allowed, "unlimited": self.unlimited}

    async def refund_quota(self, user_id, cost):
        self.refund_calls.append((user_id, cost))


async def _with_fake(fake, coro_fn):
    real_check = file_task_quota.check_and_consume_quota
    real_refund = file_task_quota.refund_quota
    file_task_quota.check_and_consume_quota = fake.check_and_consume_quota
    file_task_quota.refund_quota = fake.refund_quota
    try:
        return await coro_fn()
    finally:
        file_task_quota.check_and_consume_quota = real_check
        file_task_quota.refund_quota = real_refund


async def main():
    # 1) Ruxsat bor, muvaffaqiyat -> bitta marta yechiladi, qaytarilmaydi
    fake = FakeQuota(allowed=True)
    async def scenario_success():
        q = file_task_quota.FileTaskQuota(user_id=1, cost=250)
        assert await q.ensure_charged() is True
        assert await q.ensure_charged() is True  # ikkinchi chaqiriq qayta yechmaydi
        q.mark_success()
        await q.refund_if_unused()
    await _with_fake(fake, scenario_success)
    assert fake.charge_calls == [(1, 250)], f"faqat bitta marta yechilishi kerak edi: {fake.charge_calls}"
    assert fake.refund_calls == [], "muvaffaqiyatda qaytarish bo'lmasligi kerak"

    # 2) Ruxsat bor, hech qachon muvaffaqiyatga erishmadi -> qaytariladi
    fake = FakeQuota(allowed=True)
    async def scenario_all_failed():
        q = file_task_quota.FileTaskQuota(user_id=2, cost=250)
        await q.ensure_charged()
        await q.ensure_charged()  # ikkinchi urinish ham xato, lekin qayta yechilmaydi
        await q.refund_if_unused()
    await _with_fake(fake, scenario_all_failed)
    assert fake.charge_calls == [(2, 250)]
    assert fake.refund_calls == [(2, 250)], "hamma urinish muvaffaqiyatsiz bo'lsa qaytarilishi kerak"

    # 3) Ruxsat yo'q (kredit tugagan) -> False qaytadi, keyin refund chaqirilmaydi
    #    (chunki hech narsa yechilmagan holatga o'xshab ko'rinadi, lekin aslida
    #    check_and_consume_quota o'zi allaqachon hech narsa yechmagan bo'ladi —
    #    shuning uchun bu yerda ham qaytarish shart emas, faqat charge_calls
    #    bitta marta chaqirilganini tekshiramiz).
    fake = FakeQuota(allowed=False)
    async def scenario_denied():
        q = file_task_quota.FileTaskQuota(user_id=3, cost=250)
        allowed = await q.ensure_charged()
        assert allowed is False
        await q.refund_if_unused()
    await _with_fake(fake, scenario_denied)
    assert fake.charge_calls == [(3, 250)]
    assert fake.refund_calls == [], "ruxsat berilmagan holatda qaytarish shart emas"

    # 4) Cheksiz (premium) -> muvaffaqiyatsiz bo'lsa ham qaytarilmaydi
    fake = FakeQuota(allowed=True, unlimited=True)
    async def scenario_unlimited_failed():
        q = file_task_quota.FileTaskQuota(user_id=4, cost=250)
        await q.ensure_charged()
        await q.refund_if_unused()
    await _with_fake(fake, scenario_unlimited_failed)
    assert fake.refund_calls == [], "cheksiz foydalanuvchiga qaytarish shart emas"

    print("file_task_quota: barcha tekshiruvlar o'tdi.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it to verify it fails (module doesn't exist yet)**

Run: `python test_file_task_quota.py`
Expected: `ModuleNotFoundError: No module named 'file_task_quota'`

- [ ] **Step 3: Write `file_task_quota.py`**

```python
"""Fayl-vazifa (sandbox) kvotasi uchun "bir marta yechish, muvaffaqiyatsiz
bo'lsa qaytarish" hisob-kitobi. GPT bitta foydalanuvchi so'rovida
run_python_sandbox tool'ini bir necha marta (xatoni tuzatib qayta urinish
uchun) chaqirishi mumkin — foydalanuvchi buning uchun faqat BIR MARTA
to'laydi.
"""
from database import check_and_consume_quota, refund_quota


class FileTaskQuota:
    def __init__(self, user_id: int, cost: int):
        self.user_id = user_id
        self.cost = cost
        self._charged = False
        self._allowed = True
        self._unlimited = False
        self._succeeded = False

    async def ensure_charged(self) -> bool:
        """Birinchi chaqiriqda ball yechadi. Keyingi chaqiriqlar hech narsa
        qilmay, avvalgi natijani qaytaradi. Ruxsat bo'lsa True."""
        if not self._charged:
            result = await check_and_consume_quota(self.user_id, self.cost)
            self._charged = True
            self._allowed = result.get("allowed", True)
            self._unlimited = result.get("unlimited", False)
        return self._allowed

    def mark_success(self) -> None:
        self._succeeded = True

    async def refund_if_unused(self) -> None:
        """Ball yechilgan, lekin hech qachon muvaffaqiyatli natija
        bo'lmagan va foydalanuvchi cheksiz (premium) bo'lmasa — qaytaradi."""
        if self._charged and self._allowed and not self._succeeded and not self._unlimited:
            await refund_quota(self.user_id, self.cost)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python test_file_task_quota.py`
Expected: `file_task_quota: barcha tekshiruvlar o'tdi.` printed, no assertion errors.

- [ ] **Step 5: Commit**

```bash
git add file_task_quota.py test_file_task_quota.py
git commit -m "Add file_task_quota — charge-once/refund-if-failed quota bookkeeping"
```

---

### Task 4: Wire the `run_python_sandbox` tool into `services.py`

**Files:**
- Modify: `services.py:1-16` (imports)
- Modify: `services.py:512-547` (`_TOOLS` list — add second tool)
- Modify: `services.py:581-719` (`get_openai_reply` signature + tool-dispatch loop, `get_gpt_reply` signature)

**Interfaces:**
- Consumes: `sandbox.run_in_sandbox(code, input_file_bytes, input_filename) -> SandboxResult` (Task 2), `file_task_quota.FileTaskQuota` (Task 3), `config.MESSAGE_COST_FILE_TASK` (Task 1)
- Produces: `get_openai_reply(chat_id, message_text, *, model=GPT_MODEL, user_id=None, input_file_bytes=None, input_filename=None, output_files=None)` — an async generator, same as before but with new keyword-only params. `output_files`, if passed a list, gets `.extend()`-ed with `(filename: str, content: bytes)` tuples as the sandbox produces them. `get_gpt_reply(chat_id, user_message, *, user_id=None, input_file_bytes=None, input_filename=None, output_files=None)` — same new params, forwarded straight through.

- [ ] **Step 1: Add the new imports at the top of `services.py`**

Right after the existing `from openai import NotFoundError, RateLimitError` (line 16):

```python
from database import check_and_consume_quota, refund_quota
from file_task_quota import FileTaskQuota
from sandbox import run_in_sandbox
```

Also add `MESSAGE_COST_FILE_TASK` to the existing `try: from config import (...)` block (line 24-28) — add it to that import list.

- [ ] **Step 2: Add the second tool to `_TOOLS`**

In `services.py`, right after the closing `]` of the `internet_search` tool dict inside `_TOOLS` (the list currently has one dict — turn it into two by adding a comma and this second entry, around line 546-547):

```python
    {
        "type": "function",
        "name": "run_python_sandbox",
        "description": (
            "Foydalanuvchi yuborgan faylni TAHRIRLASH yoki yangi fayl "
            "YARATISH kerak bo'lganda ishlatiladi (masalan: Excel qiymatini "
            "almashtirish, Word hujjat yaratish, matndan PDF generatsiya "
            "qilish, CSV bilan ishlash, diagramma chizish, fayllarni ZIP "
            "qilish). Faqat fayl haqida SO'ZLASH/XULOSA berish kerak "
            "bo'lganda bu tool KERAK EMAS — oddiy javob yetarli.\n\n"
            "Agar foydalanuvchi fayl yuborgan bo'lsa, u /work/input.<kengaytma> "
            "yo'lida sandbox ichida mavjud. Fayl tuzilishini bilmasangiz, "
            "avval strukturasini chop etadigan (masalan sheet nomlari va "
            "birinchi qatorlar) qisqa tekshiruv kodini yozib chaqiring, "
            "natijani ko'rib, keyingi chaqiriqda haqiqiy o'zgartirishni "
            "bajaring — bu tool bir xabar davomida bir necha marta "
            "chaqirilishi mumkin.\n\n"
            "Natija fayl(lar)ini /work/output/ papkasiga yozing. "
            "O'rnatilgan kutubxonalar: pandas, openpyxl, python-docx, "
            "python-pptx, pypdf, reportlab, matplotlib, lxml, "
            "beautifulsoup4 — standart kutubxona (json, csv, zipfile, "
            "xml, html.parser) ham mavjud. Internetga chiqish YO'Q, "
            "faqat /work/ papkasiga yozish mumkin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Bajariladigan to'liq Python kodi.",
                },
            },
            "required": ["code"],
        },
        "strict": False,
    },
```

- [ ] **Step 3: Extend `get_openai_reply`'s signature**

Change (around line 581):

```python
async def get_openai_reply(chat_id: int, message_text: str, *, model: str = GPT_MODEL):
```

to:

```python
async def get_openai_reply(
    chat_id: int,
    message_text: str,
    *,
    model: str = GPT_MODEL,
    user_id: Optional[int] = None,
    input_file_bytes: Optional[bytes] = None,
    input_filename: Optional[str] = None,
    output_files: Optional[list] = None,
):
```

- [ ] **Step 4: Track file-task state next to the existing search-tracking variables**

Around line 619-623, where `tool_round`, `search_performed`, `synthesis_injected`, `resolved_model` are initialized, add:

```python
    file_task_quota: Optional[FileTaskQuota] = (
        FileTaskQuota(user_id, MESSAGE_COST_FILE_TASK) if user_id is not None else None
    )
    file_task_started = False
```

- [ ] **Step 5: Signal the status the first time the tool is called**

Inside the `async for event in stream:` loop (around line 648-660), the existing code only checks `not search_performed` for the `internet_search` case. Function calls carry a `name` on the `function_call` item, so branch on it. Replace:

```python
                    elif et == "response.output_item.added" and getattr(event.item, "type", None) == "function_call":
                        got_function_call = True
                        if not search_performed:
                            # Kontent emas — faqat "band" animatsiyasiga signal:
                            # qidiruv (sekundlab davom etadigan tarmoq so'rovi) boshlanmoqda.
                            yield "[STATUS]search"
                            search_performed = True
```

with:

```python
                    elif et == "response.output_item.added" and getattr(event.item, "type", None) == "function_call":
                        got_function_call = True
                        call_name = getattr(event.item, "name", None)
                        if call_name == "run_python_sandbox":
                            if not file_task_started:
                                yield "[STATUS]file_task"
                                file_task_started = True
                        elif not search_performed:
                            # Kontent emas — faqat "band" animatsiyasiga signal:
                            # qidiruv (sekundlab davom etadigan tarmoq so'rovi) boshlanmoqda.
                            yield "[STATUS]search"
                            search_performed = True
```

- [ ] **Step 6: Dispatch `run_python_sandbox` calls inside the `for call_item in pending_calls:` loop**

The existing loop (around line 672-703) only handles `internet_search` (it doesn't check `call_item.name` at all today, because there was only one tool). Wrap the existing body in a name check and add the new branch. Replace:

```python
        for call_item in pending_calls:
            try:
                args = json.loads(call_item.arguments or "{}")
            except Exception:
                args = {}

            primary_query = args.get("primary_query", "")
            extra_queries = args.get("extra_queries", [])

            if primary_query:
                logger.info(
                    f"[SEARCH] primary='{primary_query}' extra={extra_queries} round={tool_round + 1}"
                )
                search_result = await multi_source_deep_search(
                    primary_query=primary_query,
                    extra_queries=extra_queries if extra_queries else None,
                    fetch_pages=3,
                )
            else:
                search_result = "Qidiruv so'rovi bo'sh bo'lgani uchun bajarilmadi."

            messages.append({
                "type": "function_call",
                "call_id": call_item.call_id,
                "name": call_item.name,
                "arguments": call_item.arguments,
            })
            messages.append({
                "type": "function_call_output",
                "call_id": call_item.call_id,
                "output": search_result,
            })
```

with:

```python
        for call_item in pending_calls:
            try:
                args = json.loads(call_item.arguments or "{}")
            except Exception:
                args = {}

            if call_item.name == "run_python_sandbox":
                tool_output = await _run_file_task(
                    args.get("code", ""),
                    file_task_quota=file_task_quota,
                    input_file_bytes=input_file_bytes,
                    input_filename=input_filename,
                    output_files=output_files,
                    round_num=tool_round + 1,
                )
            else:
                primary_query = args.get("primary_query", "")
                extra_queries = args.get("extra_queries", [])

                if primary_query:
                    logger.info(
                        f"[SEARCH] primary='{primary_query}' extra={extra_queries} round={tool_round + 1}"
                    )
                    tool_output = await multi_source_deep_search(
                        primary_query=primary_query,
                        extra_queries=extra_queries if extra_queries else None,
                        fetch_pages=3,
                    )
                else:
                    tool_output = "Qidiruv so'rovi bo'sh bo'lgani uchun bajarilmadi."

            messages.append({
                "type": "function_call",
                "call_id": call_item.call_id,
                "name": call_item.name,
                "arguments": call_item.arguments,
            })
            messages.append({
                "type": "function_call_output",
                "call_id": call_item.call_id,
                "output": tool_output,
            })
```

- [ ] **Step 7: Add the `_run_file_task` helper right above `get_openai_reply`**

This keeps the dispatch loop readable and isolates the quota/sandbox glue in one place. Insert right before `async def get_openai_reply(...)` (around line 580):

```python
async def _run_file_task(
    code: str,
    *,
    file_task_quota: Optional[FileTaskQuota],
    input_file_bytes: Optional[bytes],
    input_filename: Optional[str],
    output_files: Optional[list],
    round_num: int,
) -> str:
    """run_python_sandbox tool chaqiruvini bajaradi: kvotani (bir marta)
    yechadi, sandbox'da kodni ishga tushiradi, natijani GPT'ga
    tushunarli matn sifatida qaytaradi. Muvaffaqiyatli fayllarni
    `output_files` ro'yxatiga (chaqiruvchiga tegishli) qo'shadi.
    """
    if file_task_quota is None:
        return "XATOLIK: foydalanuvchi aniqlanmadi, kod bajarilmadi."

    allowed = await file_task_quota.ensure_charged()
    if not allowed:
        return (
            "XATOLIK: foydalanuvchining bugungi krediti tugagan. Kodni "
            "bajarmang — foydalanuvchiga buni tushuntiring."
        )

    logger.info(f"[FileTask] round={round_num} kod uzunligi={len(code)}")
    result = await run_in_sandbox(code, input_file_bytes, input_filename)

    if result.success:
        file_task_quota.mark_success()
        if output_files is not None:
            output_files.extend(result.output_files)
        names = ", ".join(f[0] for f in result.output_files) or "(fayl yaratilmadi)"
        return (
            f"Bajarildi. Yaratilgan/tahrirlangan fayllar: {names}\n"
            f"STDOUT:\n{result.stdout[:1500]}"
        )

    return (
        f"XATO (kod bajarilmadi):\n{result.traceback[:3000]}\n"
        "Kodni tuzatib qayta chaqiring."
    )
```

- [ ] **Step 8: Refund on total failure before the generator returns**

Around line 667-670, where the loop exits when there's no more function call:

```python
        if not got_function_call:
            if final_response.status == "incomplete":
                logger.warning(f"GPT javobi incomplete tugadi: {final_response.incomplete_details}")
            return
```

replace with:

```python
        if not got_function_call:
            if file_task_quota is not None:
                await file_task_quota.refund_if_unused()
            if final_response.status == "incomplete":
                logger.warning(f"GPT javobi incomplete tugadi: {final_response.incomplete_details}")
            return
```

(This is the only natural exit point of the `while True:` loop — every path through the function either keeps looping via `tool_round += 1` or returns here.)

- [ ] **Step 9: Extend `get_gpt_reply`'s signature and forward the new params**

Replace (line 717-719):

```python
async def get_gpt_reply(chat_id: int, user_message: str):
    async for chunk in get_openai_reply(chat_id, user_message):
        yield chunk
```

with:

```python
async def get_gpt_reply(
    chat_id: int,
    user_message: str,
    *,
    user_id: Optional[int] = None,
    input_file_bytes: Optional[bytes] = None,
    input_filename: Optional[str] = None,
    output_files: Optional[list] = None,
):
    async for chunk in get_openai_reply(
        chat_id,
        user_message,
        user_id=user_id,
        input_file_bytes=input_file_bytes,
        input_filename=input_filename,
        output_files=output_files,
    ):
        yield chunk
```

- [ ] **Step 10: Sanity-check the module still imports cleanly**

Run: `python -c "import services"`
Expected: no output, no traceback. (This won't catch runtime/logic bugs, only import-time errors like typos in the new code.)

- [ ] **Step 11: Commit**

```bash
git add services.py
git commit -m "Wire run_python_sandbox tool into the GPT tool-calling loop"
```

---

### Task 5: Status text/emoji for `file_task`, `process_stream_draft` awareness

**Files:**
- Modify: `handlers_messages.py:222-261` (`STATUS_TEXTS_BY_TYPE`, `EMOJI_ID_BY_TYPE`)
- Modify: `handlers_messages.py:396-402` (the `[STATUS]` handling inside `process_stream_draft`)

**Interfaces:**
- Consumes: nothing new
- Produces: `STATUS_TEXTS_BY_TYPE["file_task"]`, `EMOJI_ID_BY_TYPE["file_task"]` — used by any future caller the same way `"search"`/`"photo"`/etc. already are

- [ ] **Step 1: Add a `"file_task"` entry to `STATUS_TEXTS_BY_TYPE`**

In `handlers_messages.py`, inside the `STATUS_TEXTS_BY_TYPE` dict (right after the `"search"` entry, before the closing `}` at line 253):

```python
    "file_task": [
        "Fayl tahlil qilinmoqda",
        "Kod yozilmoqda",
        "Fayl qayta ishlanmoqda",
        "Natija tayyorlanmoqda",
    ],
```

- [ ] **Step 2: Add a `"file_task"` entry to `EMOJI_ID_BY_TYPE`**

Right after the `"search"` entry in that dict (line 260):

```python
    "file_task": "5981499901279102420",
```

(This is a placeholder custom-emoji ID copied in the same numeric shape as the existing entries — replace it with a real premium emoji ID from BotFather's custom emoji picker before shipping if the exact glyph matters; it only affects the private-chat rich-draft rendering path, which already has a working plain-text fallback if the ID is wrong or unavailable, so this is not a functional blocker.)

- [ ] **Step 3: Make `process_stream_draft` recognize the new status**

In `process_stream_draft` (around line 396-402), the existing code:

```python
            if chunk.startswith("[STATUS]"):
                # Kontent emas — bu faqat "band" animatsiyasiga signal.
                # Animatsiya TO'XTATILMAYDI, chunki qidiruv hali davom
                # etyapti (ekran "muzlab qolmasligi" uchun).
                if "search" in chunk:
                    active_type = "search"
                continue
```

becomes:

```python
            if chunk.startswith("[STATUS]"):
                # Kontent emas — bu faqat "band" animatsiyasiga signal.
                # Animatsiya TO'XTATILMAYDI, chunki qidiruv/fayl vazifasi
                # hali davom etyapti (ekran "muzlab qolmasligi" uchun).
                if "search" in chunk:
                    active_type = "search"
                elif "file_task" in chunk:
                    active_type = "file_task"
                continue
```

- [ ] **Step 4: Commit**

```bash
git add handlers_messages.py
git commit -m "Add file_task status text/emoji and recognize it in process_stream_draft"
```

---

### Task 6: Thread the new params through the message handlers, send output files

**Files:**
- Modify: `handlers_messages.py:1-11` (imports — need `BufferedInputFile`)
- Modify: `handlers_messages.py:683-787` (`_process_merged_text`, called by `handle_text`)
- Modify: `handlers_messages.py:850-935` (`handle_document`)
- Modify: `handlers_messages.py:940-1032` (`handle_voice`)

**Interfaces:**
- Consumes: `get_gpt_reply(chat_id, message, *, user_id=None, input_file_bytes=None, input_filename=None, output_files=None)` (Task 4)

- [ ] **Step 1: Import `BufferedInputFile`**

`handlers_messages.py` line 10 currently reads:

```python
from aiogram.types import Message, FSInputFile
```

change to:

```python
from aiogram.types import Message, FSInputFile, BufferedInputFile
```

- [ ] **Step 2: Add a small shared helper for sending collected output files**

Right after the `_edit_message_fallback` function (around line 219, before `STATUS_TEXTS_BY_TYPE`), add:

```python
# Telegram bot API'ning hujjat yuborishdagi qattiq chegarasi.
MAX_TELEGRAM_DOCUMENT_SIZE = 50 * 1024 * 1024


async def _send_output_files(chat_id: int, output_files: list[tuple[str, bytes]]) -> None:
    """run_python_sandbox tool yaratgan fayllarni foydalanuvchiga yuboradi.
    Xato bitta fayl uchun boshqalarini to'xtatmaydi — va, spec talab
    qilganidek, jim qolmaydi: yuborib bo'lmagan fayl haqida foydalanuvchiga
    alohida tushuntirish xabari boradi (sabab loglanadi, foydalanuvchiga
    esa umumiy, tushunarli matn)."""
    for filename, content in output_files:
        if len(content) > MAX_TELEGRAM_DOCUMENT_SIZE:
            logger.warning(f"Natija fayl juda katta, yuborilmadi: {filename} ({len(content)} bayt)")
            try:
                await bot.send_message(
                    chat_id,
                    f"⚠️ \"{filename}\" fayli 50 MB Telegram chegarasidan katta bo'lgani "
                    "uchun yuborib bo'lmadi.",
                )
            except Exception:
                pass
            continue
        try:
            await bot.send_document(chat_id, BufferedInputFile(content, filename=filename))
        except Exception as e:
            logger.warning(f"Natija faylni yuborib bo'lmadi ({filename}): {e}")
            try:
                await bot.send_message(chat_id, f"⚠️ \"{filename}\" faylini yuborishda xatolik yuz berdi.")
            except Exception:
                pass
```

- [ ] **Step 3: Wire it into `_process_merged_text` (text messages)**

In `_process_merged_text` (around line 765-766), replace:

```python
        stream_gen = get_gpt_reply(chat_id, merged_text)
        full_reply = await process_stream_draft(last_message, stream_gen)
```

with:

```python
        output_files: list[tuple[str, bytes]] = []
        stream_gen = get_gpt_reply(chat_id, merged_text, user_id=user_id, output_files=output_files)
        full_reply = await process_stream_draft(last_message, stream_gen)

        if output_files:
            await _send_output_files(chat_id, output_files)
```

- [ ] **Step 4: Wire it into `handle_document`**

In `handle_document` (around line 909-910), replace:

```python
        stream_gen = get_gpt_reply(chat_id, prompt)
        full_reply = await process_stream_draft(message, stream_gen, content_type="document")
```

with:

```python
        output_files: list[tuple[str, bytes]] = []
        stream_gen = get_gpt_reply(
            chat_id, prompt, user_id=user_id,
            input_file_bytes=file_bytes, input_filename=file_name,
            output_files=output_files,
        )
        full_reply = await process_stream_draft(message, stream_gen, content_type="document")

        if output_files:
            await _send_output_files(chat_id, output_files)
```

(`file_bytes` and `file_name` are already local variables in `handle_document` — `file_bytes` from the existing `await bot.download_file(...)` call, `file_name` from the top of the function.)

- [ ] **Step 5: Wire it into `handle_voice`**

In `handle_voice` (around line 984-985), replace:

```python
        stream_gen = get_gpt_reply(chat_id, user_text)
        full_reply_text = await process_stream_draft(message, stream_gen, content_type="voice")
```

with:

```python
        output_files: list[tuple[str, bytes]] = []
        stream_gen = get_gpt_reply(chat_id, user_text, user_id=user_id, output_files=output_files)
        full_reply_text = await process_stream_draft(message, stream_gen, content_type="voice")

        if output_files:
            await _send_output_files(chat_id, output_files)
```

- [ ] **Step 6: Sanity-check the module still imports cleanly**

Run: `python -c "import handlers_messages"`
Expected: no output, no traceback.

- [ ] **Step 7: Commit**

```bash
git add handlers_messages.py
git commit -m "Send GPT-generated files back to the user after text/document/voice replies"
```

---

### Task 7: E2B sandbox template (manual infrastructure step)

**Files:**
- Create: `sandbox_template/e2b.Dockerfile`
- Create: `sandbox_template/e2b.toml`

This task is infrastructure, not application code — it requires an E2B account and the E2B CLI, run by hand by whoever owns the E2B account. It is included here because `sandbox.py` (Task 2) cannot work without it, and the Dockerfile is what guarantees every library the tool's description (Task 4, Step 2) promises is actually available with no network access at runtime.

- [ ] **Step 1: Write the template Dockerfile**

Create `sandbox_template/e2b.Dockerfile`:

```dockerfile
FROM python:3.11-slim

RUN pip install --no-cache-dir \
    pandas \
    openpyxl \
    python-docx \
    python-pptx \
    pypdf \
    reportlab \
    matplotlib \
    lxml \
    beautifulsoup4

WORKDIR /work
```

- [ ] **Step 2: Write the E2B template config**

Create `sandbox_template/e2b.toml` (field names/shape should be double-checked against `e2b template init` output — E2B's CLI generates this file, this is a best-effort starting point):

```toml
# `e2b template build` shu faylni o'qiydi. Aniq maydonlar E2B CLI
# hujjatidan (https://e2b.dev/docs/sandbox-template) tasdiqlanishi kerak.
template_name = "file-sandbox"
dockerfile = "e2b.Dockerfile"
```

- [ ] **Step 3: Install the E2B CLI and log in (manual, on the machine of whoever owns the E2B account)**

Run:
```bash
npm install -g @e2b/cli
e2b auth login
```

- [ ] **Step 4: Build the template**

Run (from inside `sandbox_template/`):
```bash
e2b template build --name file-sandbox
```

Confirm the CLI reports a successful build and prints a template ID/name. This name must match `E2B_TEMPLATE = "file-sandbox"` in `sandbox.py` (Task 2) — if the CLI assigns a different ID, update that constant.

- [ ] **Step 5: Verify with `sandbox.py`'s self-check**

Now that the template exists and `.env` has a real `E2B_API_KEY`, go back to Task 2 Step 3 and run `python sandbox.py` for real.

- [ ] **Step 6: Commit**

```bash
git add sandbox_template/
git commit -m "Add E2B sandbox template (Dockerfile + config) for file-task execution"
```

---

### Task 8: End-to-end manual verification

Not automatable — this drives the real bot against real Telegram/OpenAI/E2B. Run each scenario against the deployed (or locally polling, single-instance — see the earlier `TelegramConflictError` discussion, don't run two pollers at once) bot and confirm the described outcome.

- [ ] **Step 1: Pure generation, no file — "text → PDF"**

Send: `"Mana bu matnni PDF qilib ber: Salom dunyo, bu test hujjat."`
Expected: status shows `"Fayl tahlil qilinmoqda..."` (or one of the other `file_task` phrases) while waiting, then a text reply plus an attached `.pdf` document containing that text.

- [ ] **Step 2: Edit an uploaded file — the original bug report**

Send an `.xlsx` (not `.xls` — legacy binary Excel still isn't parsed for the *preview* text, but the sandbox tool works on raw bytes regardless of preview quality) containing a `31.12.99` value somewhere, caption: `"Bu faylda 31.12.99 bor, shuni 0 ga almashtirib qolganini o'zgartirmasdan qaytar."`
Expected: bot returns an edited `.xlsx` with that value replaced and everything else intact.

- [ ] **Step 3: Sandbox error → automatic retry**

Send a request likely to trip up the first attempt (e.g. reference a column name that doesn't exist in the uploaded CSV). Watch the logs for a `[FileTask] round=1 ...` entry followed by a `[FileTask] round=2 ...` entry (confirms GPT saw the traceback and retried) before either succeeding or giving up after 3 rounds.

- [ ] **Step 4: Quota exhaustion**

Temporarily lower `MESSAGE_COST_FILE_TASK` or a test user's remaining `daily_requests_used` (via the admin panel's "Limitni reset qilish" is the wrong direction — instead, run several file-task requests back to back on a free-tier test account until the daily budget of 1000 is exhausted) and confirm the bot replies with a clear "kredit tugadi" message and does **not** attempt to run code (check logs for the absence of a new `[FileTask]` line after the limit message).

- [ ] **Step 5: Guest mode is unaffected**

Trigger a guest-mode call (`@botusername` in a chat the bot isn't a member of) with a file-editing request. Confirm it behaves exactly as before this plan (plain text reply, no attempt to attach a file, no `run_python_sandbox` in the logs) — this confirms Task 4-6's changes didn't leak into `guest_handlers.py`, which was intentionally left untouched.

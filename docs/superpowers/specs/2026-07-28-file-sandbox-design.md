# Fayl bilan ishlash (Advanced Data Analysis) — dizayn

> **HOLAT: amalga oshirildi (2026-07-28).** Bitta muhim arxitektura
> o'zgarishi bilan: E2B o'rniga kod bot konteynerining O'ZIDA, tozalangan
> muhit + resurs chegaralari bilan bajariladi (`sandbox.py`). Sabab va
> xavfsizlik oqibatlari pastda, "Amalga oshirishdagi og'ish" bo'limida.

## Amalga oshirishdagi og'ish — E2B o'rniga mahalliy izolyatsiya

Rejalashtirilganda sandbox E2B (tashqi xizmat) bo'lishi kerak edi. Amalda
u ishlatilmadi, chunki: (a) E2B hisobi va qo'lda quriladigan custom
template talab qilinadi — bu "push qilgan zahoti ishlasin" talabiga zid;
(b) har bajarilish uchun tashqi xizmatga to'lov va bog'liqlik qo'shiladi.

O'rniga `sandbox.py` kodni bot konteynerining ichida, alohida jarayon
sifatida bajaradi. Nima himoyalangan:

| Talab | Holat | Qanday |
|---|---|---|
| #6 CPU/RAM/timeout chegaralari | ✅ | `RLIMIT_CPU/AS/FSIZE/NPROC` + 60s qattiq timeout |
| #7 faqat vaqtinchalik papka | ✅ | `tempfile.mkdtemp()`, cwd o'sha yerda, oxirida `rmtree` |
| #8 tizim fayllariga kirish taqiqlansin | ⚠️ qisman | Muhit o'zgaruvchilari TOZALANADI (BOT_TOKEN / OPENAI_API_KEY / DATABASE_URL bola-jarayonga umuman uzatilmaydi — test bilan tasdiqlangan). Lekin fayl tizimi o'qish uchun ochiq qoladi. |
| #9 kirish fayli sandbox ichiga | ✅ | `input.<kengaytma>` sifatida ish papkasiga yoziladi |
| #10 natija fayllar qaytarilsin | ✅ | `output/` papkasidan o'qib, Telegram'ga yuboriladi |
| #11 vaqtinchalik fayllar o'chirilsin | ✅ | `finally: shutil.rmtree(...)` — xato/timeoutda ham |
| #4 Docker izolyatsiyasi | ❌ | Railway'da Docker-in-Docker mavjud emas |
| #5 internetga chiqmasin | ❌ | Namespace/konteynersiz bloklab bo'lmaydi |

**Qolgan xavf va nega u qabul qilinadigan darajada:** GPT yozgan kod (agar
prompt-injection orqali buzilsa) tarmoqqa chiqishi mumkin. Lekin muhit
tozalangani uchun o'g'irlanadigan sir yo'q, timeout esa har qanday
suiiste'molni 60 soniya bilan cheklaydi. Agar keyinchalik to'liq
izolyatsiya kerak bo'lsa — `sandbox.run_in_sandbox()` funksiyasining
imzosi o'zgarmagan holda ichini E2B yoki alohida VPS'dagi Docker bilan
almashtirish yetarli (chaqiruvchi kod umuman tegilmaydi).

## Maqsad

Botga ChatGPT Plus'dagi "Advanced Data Analysis" (Code Interpreter) ga o'xshash
imkoniyat qo'shish: foydalanuvchi fayl yuboradi ("bu Excel faylda 454 yozuvini 0
qil"), bot faylni **haqiqatan tahrirlab yoki yangi fayl yaratib** qaytaradi.
Hozir bot fayllarni faqat o'qiy oladi (`extract_text_from_document`), yoza olmaydi.

Qamrov: **hammaga ochiq** (premium bilan cheklanmaydi — foydalanuvchi tanlovi),
kunlik ball-kvota orqali xarajat nazorat qilinadi.

## Muhim printsip — OpenAI faqat kod yozadi, bajarmaydi

OpenAI Assistants/Responses API'dagi tayyor "code interpreter" tool'i
ISHLATILMAYDI (u yerda fayllar OpenAI serverida qoladi). Buning o'rniga:

- GPT (mavjud Responses API, `services.py`) faqat vazifani tahlil qiladi va
  Python kodi yozadi — bu oddiy `function_call` (function calling — allaqachon
  `internet_search` tool'i uchun ishlatilyapti).
- Kod **E2B** (uchinchi tomon sandbox xizmati, OpenAI bilan bog'liq emas)
  ichida, bizning server nomidan, bajariladi.
- E2B sandbox standart holatda internetga chiqmaydi, vaqtinchalik konteyner,
  bajarilgach avtomatik yo'q qilinadi — talab qilingan izolyatsiyani
  (tarmoqsizlik, vaqtinchalik joy, tizim fayllariga kirish yo'q) o'zi
  ta'minlaydi.

## Arxitektura

```
User → Telegram bot → GPT (Responses API, tool-calling)
                          │
                          ▼ (function_call: run_python_sandbox)
                     sandbox.py (E2B SDK)
                          │
                          ▼
                E2B sandbox konteyneri
          (fayl yuklanadi → kod bajariladi → natija fayl(lar))
                          │
                          ▼
              natija fayl(lar) mahalliy diskka olinadi
                          │
                          ▼
        Telegram bot → foydalanuvchiga fayl(lar) yuboriladi
```

## Komponentlar

### 1. Yangi tool: `run_python_sandbox` (`services.py`)

`_TOOLS` ro'yxatiga (hozirgi `internet_search` bilan bir qatorda) qo'shiladi.
Flat (tekis) Responses API tool sxemasi:

```python
{
    "type": "function",
    "name": "run_python_sandbox",
    "description": (
        "Foydalanuvchi yuborgan faylni TAHRIRLASH yoki yangi fayl "
        "YARATISH kerak bo'lganda ishlatiladi (masalan: Excel qiymatini "
        "almashtirish, Word hujjat yaratish, PDF generatsiya qilish, "
        "CSV bilan ishlash, diagramma chizish, fayllarni ZIP qilish). "
        "Faqat O'QISH/XULOSA so'ralganda (fayl nima haqida ekanini "
        "tushuntirish) BU TOOL KERAK EMAS — oddiy javob yetarli.\n\n"
        "Kirish fayli /work/input.<kengaytma> yo'lida mavjud. "
        "Natija fayl(lar)ini /work/output/ papkasiga yozing. "
        "O'rnatilgan kutubxonalar: pandas, openpyxl, python-docx, "
        "python-pptx, pypdf, reportlab, matplotlib, lxml, "
        "beautifulsoup4 — standart kutubxona (json, csv, zipfile, "
        "xml, html.parser) ham mavjud. Internetga chiqish YO'Q."
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
}
```

### 2. Marshrutlash — alohida klassifikator YO'Q

Fayl yuklanganda tool har doim mavjud bo'ladi (xuddi matnli xabarlarda
`internet_search` har doim mavjud bo'lgani kabi) — GPT o'zi vaziyatga qarab
tool'ni chaqiradi yoki chaqirmaydi ("avtomatik aniqlash" talabi shu orqali
qanoatlantiriladi, qo'shimcha infratuzilma shart emas).

Fayl GPT'ga TO'LIQ matn sifatida emas, qisqa **preview** sifatida beriladi
(hozirgi `extract_text_from_document` o'rniga, faqat file-task rejimida):

| Format | Preview mazmuni |
|---|---|
| xlsx | sheet nomlari + har biridan birinchi 5 qator |
| csv | birinchi 5 qator (header bilan) |
| docx | birinchi ~500 so'z |
| pptx | slaydlar sarlavhalari + matn qisqacha |
| pdf | birinchi ~1000 belgi matn |
| json/xml/html/txt | birinchi ~1000 belgi |

Bu preview GPT'ga to'g'ri ustun nomlari/struktura bilan kod yozish uchun
yetarli kontekst beradi, butun faylni token sifatida yubormasdan.

### 3. Yangi modul: `sandbox.py`

```python
async def run_in_sandbox(
    code: str,
    input_file_bytes: bytes,
    input_filename: str,
) -> SandboxResult:
    """E2B sandbox ichida kodni bajaradi.

    Qaytaradi: SandboxResult(success, stdout, stderr, traceback,
    output_files: list[tuple[filename, bytes]]).

    Aniq E2B SDK metodlari (fayl yozish/o'qish, sandbox yaratish/yopish)
    implementatsiya bosqichida E2B rasmiy hujjatidan tasdiqlanadi — bu
    yerda faqat funksiya shartnomasi (contract) belgilanadi.
    """
```

Ichki mantiq:
1. E2B sandbox ochiladi (oldindan qurilgan **custom template** asosida —
   pastga qarang).
2. `input_file_bytes` sandbox ichiga `/work/input.<ext>` sifatida yoziladi.
3. `code` bajariladi, **timeout=60 soniya** (E2B'ning o'z timeout parametri).
4. `/work/output/` papkasidagi barcha fayllar o'qib olinadi.
5. Sandbox yopiladi (E2B avtomatik konteynerni yo'q qiladi — vaqtinchalik
   fayllar shu orqali "avtomatik o'chiriladi" talabi qanoatlantiriladi).
6. Xato (exception/timeout) bo'lsa — `success=False`, `traceback`ga to'liq
   xato matni yoziladi (GPT retry uchun ishlatadi).

**E2B custom template** (bir martalik, qo'lda sozlanadigan qadam, kod bilan
avtomatlashtirilmaydi): E2B CLI orqali Dockerfile asosida qurilgan template,
ichida yuqoridagi kutubxonalar oldindan o'rnatilgan (internetga chiqish
yo'qligi sabab, runtime'da `pip install` ISHLAMAYDI — hammasi build vaqtida
tayyor bo'lishi shart).

### 4. Tool-loop integratsiyasi (`services.py`, mavjud `get_openai_reply`)

Hozirgi `internet_search` bilan bir xil pattern (kod deyarli aynan
takrorlanadi, alohida funksiyaga chiqarilishi mumkin):

- `function_call` turi `run_python_sandbox` bo'lganda: birinchi marta
  `yield "[STATUS]file_task"` (yangi status turi).
- `sandbox.run_in_sandbox(...)` chaqiriladi.
- Natija `function_call_output` sifatida GPT'ga qaytariladi:
  - Muvaffaqiyat: `"Bajarildi. Yaratilgan fayllar: [...]"` + qisqa stdout.
  - Xato: to'liq traceback matni — GPT buni ko'rib kodni **avtomatik
    tuzatib qayta chaqiradi** (talab qilingan xatti-harakat). Qo'shimcha
    kod SHART EMAS — mavjud `MAX_TOOL_ROUNDS = 3` sikli buni allaqachon
    qo'llab-quvvatlaydi (`internet_search`dagi bilan bir xil chegara).
- Muvaffaqiyatli natija fayllari yangi sentinel orqali chaqiruvchiga
  uzatiladi: `yield f"[FILE]{local_temp_path}"` (bitta fayl uchun bir marta;
  1 tadan ko'p fayl bo'lsa, avtomatik ZIP qilinib, BITTA `[FILE]` sentinel
  yuboriladi — foydalanuvchi bitta fayl oladi).
- `handlers_messages.py` va `guest_handlers.py`dagi stream-iste'mol qiluvchi
  kod (hozir `[STATUS]`/`[CLEAR_TEXT]`ni parslaydigan joylar) `[FILE]`ni ham
  tanib, oxirida `bot.send_document(chat_id, FSInputFile(path))` chaqiradi,
  so'ng vaqtinchalik faylni o'chiradi.

### 5. Status/emoji (`handlers_messages.py`)

`STATUS_TEXTS_BY_TYPE` va `EMOJI_ID_BY_TYPE`ga yangi `"file_task"` turi
qo'shiladi (masalan: "Faylni qayta ishlayapman", "Kod yozilmoqda",
"Natija tayyorlanmoqda", "Fayl shakllantirilmoqda" — mavjud ro'yxatlar
uslubida 4 ta fraza).

### 6. Kvota (`config.py`, `database.py` o'zgarishsiz qoladi)

Yangi narx konstantasi:

```python
MESSAGE_COST_FILE_TASK: int = 250  # photo (180) dan yuqori — GPT tool-loop
                                     # + sandbox bajarilishi qimmatroq
```

`message_cost(kind, effort)` funksiyasiga `kind == "file_task"` shoxi
qo'shiladi. Mavjud `check_and_consume_quota`/`refund_quota` pattern'i
o'zgarishsiz ishlatiladi (muvaffaqiyatsiz — masalan sandbox umuman
ishlamagan — bo'lsa ball qaytariladi, xuddi hozirgi photo/document/voice
oqimlaridagi kabi).

### 7. Muhit o'zgaruvchilari va bog'liqliklar

- `.env`ga yangi: `E2B_API_KEY`.
- `requirements.txt`ga yangi: `e2b` (yoki `e2b-code-interpreter`, aniq
  paket implementatsiya bosqichida E2B hujjatidan tanlanadi).
- Bot konteynerining O'ZINING `requirements.txt`siga pandas/openpyxl kabi
  kutubxonalar QO'SHILMAYDI — ular faqat E2B sandbox template ichida kerak,
  bot serveri ularni ishlatmaydi.

## Ma'lumot oqimi (bosqichma-bosqich)

1. User fayl + "454 ni 0 qil" deb yuboradi.
2. Bot faylni yuklab oladi (raw bayt), preview tayyorlaydi.
3. GPT'ga (preview + `run_python_sandbox` tool bilan) yuboriladi.
4. GPT kod yozadi, tool'ni chaqiradi.
5. `[STATUS]file_task` → sandbox ochiladi → fayl yuklanadi → kod bajariladi
   (60s limit, tarmoqsiz, resurs chegarali — E2B ta'minlaydi).
6a. **Muvaffaqiyat**: natija fayl(lar)i olinadi → GPT'ga xabar qaytariladi
    → GPT qisqa tushuntirish matni yozadi → bot matn + faylni yuboradi.
6b. **Xato**: traceback GPT'ga qaytariladi → GPT kodni tuzatadi → 4-5
    qadamlar takrorlanadi (`MAX_TOOL_ROUNDS=3` ichida) → hamon ishlamasa,
    GPT xato haqida foydalanuvchiga tushuntirib yozadi, fayl yuborilmaydi,
    ball qaytariladi.

## Xatoliklarni boshqarish

| Holat | Xatti-harakat |
|---|---|
| E2B API/tarmoq xatosi (sandbox ochilmadi) | Foydalanuvchiga tushunarli xabar, ball qaytariladi |
| Kod cheksiz sikl / 60s dan oshdi | E2B avtomatik to'xtatadi, xato GPT'ga qaytadi (retry) |
| 3 urinishdan keyin ham xato | GPT xatoni tushuntirib yozadi, fayl yuborilmaydi, ball qaytariladi |
| Natija fayl > 50MB (Telegram chegarasi) | Yuborilmaydi, foydalanuvchiga tushuntiriladi |
| Kirish fayli > 20MB | Sandbox ochilmasdan oldin rad etiladi (hozirgi `GUEST_DOCUMENT_MAX_SIZE` patterniga o'xshash yangi chegara) |

## Xavfsizlik

- Tarmoqsizlik, vaqtinchalik fayl tizimi, tizim fayllariga kirish yo'qligi —
  E2B sandbox tomonidan ta'minlanadi (talab #5, #7, #8).
- CPU/RAM/timeout chegaralari — E2B sandbox konfiguratsiyasi orqali
  (talab #6).
- Bizning tomondan qo'shiladigan cheklovlar: kirish fayl hajmi (20MB),
  bitta foydalanuvchiga bir vaqtning o'zida faqat 1 ta faol sandbox
  (concurrency limit — spam/xarajat portlashini oldini olish uchun).

## Qamrovdan tashqarida (keyingi bosqich)

- Premium bilan cheklash — hozircha YO'Q (foydalanuvchi tanlovi bo'yicha
  hammaga ochiq).
- E2B custom template'ni qurish — bu kod yozish emas, E2B CLI orqali
  qo'lda bajariladigan bir martalik infratuzilma qadami (implementatsiya
  rejasida alohida qadam sifatida ko'rsatiladi, lekin haqiqiy bajarilishi
  E2B hisobiga ega bo'lgan odam tomonidan qilinishi kerak).
- Rasm generatsiya (DALL·E) va shaxsiy ko'rsatma (custom instructions) —
  alohida, oldin muhokama qilingan features, bu spec ularni qamrab olmaydi.

## Testlash

- `sandbox.py` uchun oddiy `demo()`/`__main__` o'z-o'zini tekshiruvi:
  E2B'ga ulanib, "3+4 natijasini `/work/output/result.txt`ga yoz" kabi
  eng oddiy kodni bajartirib, natija faylning mavjudligi va mazmunini
  tekshiradi (E2B_API_KEY talab qiladi — CI'da emas, qo'lda ishga
  tushiriladigan tekshiruv).
- Tool-loop integratsiyasi uchun: mavjud `internet_search` testlash
  usuliga o'xshab, real Telegram orqali qo'lda sinov (xlsx yuborib,
  qiymat almashtirish so'rab).

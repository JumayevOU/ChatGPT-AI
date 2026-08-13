<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0f172a,45:2563eb,100:38bdf8&height=210&section=header&text=ChatGPT%20AI%20Bot&fontSize=58&fontColor=ffffff&animation=fadeIn&fontAlignY=36&desc=A%20production%20Telegram%20AI%20assistant%20built%20for%20Uzbek%20speakers&descAlignY=57&descSize=15" width="100%" alt="ChatGPT AI Bot" />

<a href="https://t.me/uzchatgptaibot">
  <img src="https://readme-typing-svg.demolab.com?font=JetBrains+Mono&weight=600&size=21&pause=1200&color=2563EB&center=true&vCenter=true&width=680&lines=Chat%2C+vision%2C+voice+and+documents+in+one+bot;Generates+PPTX%2C+PDF%2C+Word+and+Excel+files;Runs+model-written+Python+in+a+sandbox;Remembers+you+across+conversations;Live+now+%E2%80%94+%40uzchatgptaibot" alt="What it does" />
</a>

<br/>

[![Try it on Telegram](https://img.shields.io/badge/Try_it_live-%40uzchatgptaibot-229ED9?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/uzchatgptaibot)

<br/>

![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-3.29-2CA5E0?style=flat-square&logo=telegram&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-Responses_API-412991?style=flat-square&logo=openai&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-asyncpg-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![Railway](https://img.shields.io/badge/Railway-deployed-0B0D0E?style=flat-square&logo=railway&logoColor=white)
![Tests](https://img.shields.io/badge/tests-30_suites_passing-22c55e?style=flat-square&logo=pytest&logoColor=white)
![Code](https://img.shields.io/badge/code-~14.7k_lines-64748b?style=flat-square)

</div>

---

## What this is

A **live, paying-users Telegram bot** that turns OpenAI's Responses API into a complete assistant for a market where most AI products simply aren't localized: Uzbekistan.

It is not a wrapper around one API call. It streams answers token by token into Telegram's native "thinking" UI, decides on its own when to search the web, write and run Python, draw an image, set a reminder or remember a fact about you — and it stays inside a hard cost budget while doing it.

> **Everything the user sees is in Uzbek** — including error messages, limit notices and paywalls. So is every comment in the source: this codebase is maintained by Uzbek speakers, for Uzbek speakers.

<br/>

## Features

<table>
<tr>
<td width="50%" valign="top">

### 💬 Conversation
Streaming replies rendered through Telegram's Rich Message API, with animated status, premium custom emoji and a live elapsed-time counter. Falls back gracefully three levels deep so an answer is never lost.

### 🌐 Live web search
Multi-query DuckDuckGo search with full-page fetching, source comparison and a mandatory citation format. Fires automatically when a question needs today's data.

### 🖼 Vision
Send a photo with or without a caption and get a real analysis — receipts, screenshots, homework, documents, medicine labels.

### 🎙 Voice in, voice out
Speech-to-text and a natural spoken reply. Language is auto-detected per message, so an Uzbek question gets an Uzbek voice, a Russian one gets a Russian voice.

</td>
<td width="50%" valign="top">

### 📄 Documents
PDF, Word, Excel, PowerPoint and plain text are parsed and analysed. Send a file with no caption and the bot waits for your follow-up instruction instead of guessing.

### 🛠 File generation
The model writes Python, the bot runs it in a sandbox and sends back the result: presentations, reports, spreadsheets, charts, format conversions — with a design guide that keeps the output looking professional.

### 🧠 Long-term memory
The model chooses what is worth keeping — name, profession, city, preferences — and it survives `/new`, forever. Category-prefixed and validated before it ever reaches the database.

### ⏰ Reminders & digests
A Telegram-native advantage: the bot can start the conversation. One-off or recurring reminders, plus a daily digest on topics you pick.

</td>
</tr>
</table>

<br/>

## Architecture

```mermaid
flowchart TB
    TG["Telegram Bot API"] --> DP["aiogram Dispatcher<br/>ordered handler chain"]

    DP --> PAY["Payments<br/>registered first"]
    DP --> FSM["FSM flows<br/>gift · promo · digest"]
    DP --> GATE["Maintenance gate"]
    DP --> BUSY["Spam guard<br/>GeneratingState"]
    DP --> H["Content handlers<br/>text · photo · doc · voice"]

    H --> Q{"Quota check"}
    Q -->|"denied"| UP["Localized limit notice<br/>+ upgrade button"]
    Q -->|"allowed"| AI["services/ai.py<br/>streaming tool loop"]

    AI <--> OAI["OpenAI Responses API"]
    AI --> T1["internet_search"]
    AI --> T2["run_python_sandbox"]
    AI --> T3["generate_image"]
    AI --> T4["update_memory"]
    AI --> T5["manage_reminder"]

    T2 --> SB["Isolated subprocess<br/>scrubbed env · rlimits · timeout"]

    AI --> ST["Rich streaming UI"]
    ST --> TG

    AI <--> PG[("PostgreSQL<br/>history · memory · quotas · payments")]

    WATCH["Background watchers<br/>reminders · digests · expiry · win-back"] --> TG
    WATCH <--> PG
```

<br/>

## How one message is answered

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant B as Bot
    participant M as OpenAI
    participant T as Tools

    U->>B: "Make me a slide deck on the EV market"
    B->>B: Charge points · pick reasoning effort
    B-->>U: Animated status starts

    B->>M: Stream request with active tools
    M-->>B: internet_search(...)
    B->>T: Multi-query search + page fetch
    T-->>B: Sources
    B-->>U: Status switches to "searching"

    B->>M: Feed results back
    M-->>B: run_python_sandbox(python code)
    B->>T: Execute in sandbox
    T-->>B: output/deck.pptx
    B-->>U: Status switches to "building file"

    B->>M: Feed result back
    M-->>B: Final answer, streamed
    B-->>U: Rich message + attached file
    B->>B: Save history · refund unused quota
```

Each tool has its **own round budget**. When a budget runs out that tool is dropped from the request, which forces the model to answer instead of looping. A separate total-round ceiling guards against everything else.

<br/>

## Engineering decisions worth reading

<details>
<summary><b>💸 Two independent quota systems — because one was a support nightmare</b></summary>

<br/>

Points cover ordinary messages and scale with reasoning effort — `"salom"` is cheap, an integral is not. But file generation, image drawing and deep research are billed on **separate daily counters**.

The reason is concrete: when file generation came out of the points budget, three presentations drained a user's entire day and they experienced it as *"the bot broke"*. Now the expensive operations have their own ceilings and ordinary chat keeps working after they run out.

Each expensive operation is charged **once per user request**, no matter how many times the model calls the tool — and refunded automatically if nothing was produced.

</details>

<details>
<summary><b>🔒 The model's output is an untrusted boundary</b></summary>

<br/>

Anything the model writes into a tool call reaches the database, so validation lives in the data layer — never in the tool description. An instruction is not a guarantee.

- Memory entries are stripped of newlines, so a multi-line "fact" cannot render as fake instructions in a later prompt.
- Card, passport and account number patterns are rejected outright.
- Every `UPDATE`/`DELETE` driven by a model-supplied index carries `AND user_id = $N`, and the index is bounds-checked against the list actually shown to the model before the database is touched.
- Reminder times are rejected if they are in the past, unparseable, or beyond the maximum horizon.

</details>

<details>
<summary><b>🧪 Running model-written Python without losing the host</b></summary>

<br/>

The sandbox runs each snippet in a fresh temp directory as a subprocess with a **scrubbed environment** — no bot token, no API key, no database URL — plus CPU, memory, file-size and process-count rlimits, a hard timeout and process-group kill.

The honest caveat, documented in the source rather than hidden: **network is not blocked**, because the host offers no container isolation. The mitigation is that there is nothing to steal in that environment and the timeout caps abuse.

</details>

<details>
<summary><b>⚡ Prompt caching shapes where text is allowed to live</b></summary>

<br/>

The system prompt is built to **day precision** so the prefix is byte-identical all day and prompt caching actually hits. Anything per-user — long-term memory, the user's name, the current time — goes into the message list as a `developer` message, never into the system prompt.

Putting one user-specific string in the wrong place silently destroys the cache for every user, and nothing warns you.

</details>

<details>
<summary><b>🎯 Handler registration order is a safety constraint</b></summary>

<br/>

Payment handlers are registered directly on the dispatcher, **before every router**. The spam guard that answers *"please wait, generating…"* has no content filter — if a payment confirmation arrived while a reply was streaming, that guard would swallow it: money taken, subscription not granted.

By the same logic the maintenance gate sits before the AI handlers but after `/start`, while `/pro` and `/promo` sit *after* it — selling a subscription for a disabled bot is a refund waiting to happen.

</details>

<details>
<summary><b>🩺 Failure modes are designed, not discovered</b></summary>

<br/>

- Model unavailable or rate-limited → automatic fallback down a model list.
- Rich Message rejected → edit an existing message → plain message without formatting. Long answers are split at paragraph boundaries with code fences reopened across parts.
- Transcription fails → second engine.
- Voice synthesis fails → multilingual fallback voice.
- Payment cannot be granted → the user is told their money is safe and the admin gets everything needed to fix it by hand.
- User blocked the bot → marked inactive instead of retried forever.

</details>

<br/>

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| **Bot framework** | aiogram 3.29 | Native async, FSM, and Guest Mode support |
| **AI** | OpenAI Responses API | Streaming + parallel tool calling in one loop |
| **Database** | PostgreSQL via asyncpg | Connection pooling, `FOR UPDATE` row locks for quota races |
| **Sandbox** | subprocess + rlimits | No Docker-in-Docker available on the host |
| **Docs** | python-pptx · reportlab · openpyxl · python-docx | Model writes the code, these do the work |
| **Speech** | OpenAI audio models, with a free-tier fallback path | Quality where it is paid for, availability everywhere |
| **Payments** | Telegram Stars (XTR) | No payment provider, no KYC, works instantly |
| **Hosting** | Railway | Postgres included, deploys by commit SHA |

<br/>

## Project structure

```
├── main.py                  # entrypoint — handler order is documented and load-bearing
├── core/
│   ├── config.py            # model, prompts, plans, limits, costs — one source of truth
│   ├── loader.py            # bot, dispatcher, OpenAI client singletons
│   └── memory.py            # in-RAM buffers with TTL cleanup
├── handlers/
│   ├── messages.py          # text · photo · document · voice + streaming UI
│   ├── pro.py               # payments, gifts, promo codes, referrals
│   ├── admin.py             # broadcasts, statistics, user management, maintenance
│   ├── guest.py             # Guest Mode — one identity in groups and DMs
│   ├── digest.py            # daily digest scheduling
│   └── helpers.py           # background watchers: reminders, expiry, win-back
├── services/
│   ├── ai.py                # the tool loop, vision, speech, search
│   ├── sandbox.py           # isolated execution of model-written Python
│   └── file_task_quota.py   # charge once, refund if nothing was produced
├── db/
│   ├── database.py          # quotas, plans, payments, memory, reminders
│   └── history.py           # conversation history in Postgres + RAM cache
└── tests/                   # 30 standalone assert-based suites
```

<br/>

## Testing

No framework, no fixtures, no mocking library. Every suite is a standalone script that fails loudly with a message explaining *why the check exists*.

```bash
python tests/test_pro_security.py     # payment attack scenarios
python tests/test_tool_status.py      # tool loop status and prompt routing
python tests/test_long_reply.py       # long answers must never disappear
python tests/test_plan_limits.py      # quota and refund invariants
```

They exist to lock down behaviour that is invisible until it breaks in production — a tool call silently routed to web search, a refund handed to a user who was never charged, a status animation lying about what the bot is doing.

<br/>

<details>
<summary><b>🚀 Running it yourself</b></summary>

<br/>

```bash
git clone https://github.com/JumayevOU/ChatGPT-AI.git
cd ChatGPT-AI
pip install -r requirements.txt
```

Create a `.env` file:

```env
BOT_TOKEN=...          # @BotFather
OPENAI_API_KEY=...     # platform.openai.com
DATABASE_URL=...       # postgres://user:pass@host:port/db
```

```bash
python main.py
```

Tables are created on first run. `ffmpeg` on `PATH` is optional and only used by the free-tier voice path.

</details>

<br/>

---

<div align="center">

### Built by Og'abek Jumayev

[![Telegram](https://img.shields.io/badge/Telegram-%40jumayeevou-229ED9?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/jumayeevou)
[![GitHub](https://img.shields.io/badge/GitHub-JumayevOU-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/JumayevOU)

**The bot is live and in daily use — [talk to it](https://t.me/uzchatgptaibot).**

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:38bdf8,55:2563eb,100:0f172a&height=120&section=footer" width="100%" alt="" />

</div>

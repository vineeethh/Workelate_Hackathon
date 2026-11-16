# CrawlPilot: chat that runs CLI tools, not just text

**BuildSprint Round 2: CLI-Driven AI Chat Execution for Complex Tasks**

CrawlPilot is a chat app. You describe a task in plain English, for example *"Crawl https://example.com, extract the important information, give me a summary, and return the data in JSON"*, and CrawlPilot then:

1. **Works out what you want.** Claude fills in a typed `TaskSpec`. If no API key is set, a deterministic rule-based parser does it instead.
2. **Checks the inputs.** It validates the URL scheme, resolves DNS, blocks private and internal IPs (SSRF), allows only standard ports and clamps the limits.
3. **Picks an approved CLI tool** from an allow-list and builds a fixed argv. **No shell is used, and no text from you or the model is ever run as a command.**
4. **Runs the CLI as a subprocess** (`sitecrawl`). It streams the tool's **stderr live** into the chat, captures **stdout (JSON)** and the **exit code**, and enforces a **timeout**, **cancellation**, rlimits and an environment with no secrets.
5. **Checks the output.** It confirms the JSON parses and matches the expected schema, and maps the exit code to a status.
6. **Answers in the chat** with a **status banner** (Success, Partial, Failed or Invalid input), a **summary** and **syntactically valid structured JSON** you can copy or download.

> It never reports success without evidence. If the process times out, is cancelled, exits non-zero or produces invalid stdout, the chat says so.

---

## Architecture

```
 Browser (frontend/index.html)
   chat box, live CLI output, JSON viewer
        │   POST /api/chat  (streamed back live)
        ▼
 FastAPI backend (backend/app.py)
   1. intent.py       understands the request (AI, or simple rules if no key)
   2. security.py     checks the URL is safe and public
   3. orchestrator.py runs the approved CLI and captures its output
   4. responder.py    checks the output, writes the summary and JSON
   5. verify.py       checks every summary claim against the crawled text
        │   runs as a separate process (no shell)
        ▼
 sitecrawl CLI (sitecrawl/cli.py)
   crawls the site, prints one JSON result, logs progress, returns an exit code
```

### Why a custom CLI (`sitecrawl`)?
We looked at existing tools first: Crawl4AI's `crwl`, Firecrawl and ScrapeGraphAI. They are powerful, but they need headless browsers or a hosted API key and are heavy to install. We wanted a tool that is:
* **Easy for judges to reproduce.** It's pure Python, installs in seconds and needs no browser download.
* **Built for being driven by a program.** stdout is exactly one JSON document and stderr carries line-based progress events, so the chat can stream them live. Exit codes are documented.
* **Safe by default.** It respects robots.txt, caps pages/depth/bytes/time, and re-validates the target after redirects.

The orchestrator uses a tool registry (`TOOLS` in `backend/orchestrator.py`). Adding another approved CLI (e.g. `crwl` for JavaScript-heavy sites) means adding one entry that has an argv builder and an exit-code map.

### CLI contract

```
python sitecrawl/cli.py <url> [--max-pages N≤25] [--max-depth D≤3] [--timeout s] [--delay s] [--max-chars n]
```
| Exit | Meaning |
|---|---|
| 0 | success: all pages crawled |
| 3 | partial: some pages failed |
| 4 | unreachable: the start URL failed |
| 5 | blocked by robots.txt |
| 2 | invalid input or forbidden target |
| 1 | unexpected error (stdout is still valid JSON) |
| -9 | killed by the orchestrator (timeout or cancel) |

### How a prompt becomes a CLI run
```python
spec, engine = parse_intent(message)            # TaskSpec(task_type, url, max_pages, max_depth, focus, ...)
url  = validate_target_url(spec.url)            # raises ValueError → "Invalid input" shown in chat
argv = TOOLS["sitecrawl"].build_argv(spec, url) # [python, -I, sitecrawl/cli.py, url, --max-pages, "5", ...]
proc = create_subprocess_exec(*argv, env=clean_env, preexec_fn=setsid+rlimits)   # NO shell
stream stderr lines → chat;  collect stdout;  wait(timeout) → killpg on timeout/cancel
result = json.loads(stdout) + schema check + exit-code mapping
answer = build_response(spec, result)           # summary + validated JSON, honest status
```

## Evidence check: summaries you can audit

An LLM summary can sound right and still be invented. CrawlPilot makes the model back every key point with a **verbatim quote** and the URL it came from. The backend then checks, with a plain string match and no LLM, that the quote really appears in the text the CLI crawled (`backend/verify.py`).

* The chat shows `Key points - evidence check: 6/6 verified`, with ✓ or ⚠ on each point, and an **Evidence check** card listing each quote and its source page.
* A key point with a missing or invented quote is shown as **unverified**. It is never silently presented as fact.
* The full report is in the JSON under `verification` (`claims_total`, `claims_verified`, and per-claim `quote`, `source_url`, `verified`).
* Limitation: the check proves the quote exists on the site. It does not prove the key point is a fair reading of that quote, so the quote is shown next to the claim for a human to judge.

## Security and reliability

| Risk | Mitigation |
|---|---|
| Shell injection | `create_subprocess_exec` with an argv list. No shell, no string commands, and the model never writes commands. |
| Arbitrary tools | Allow-list registry. An unsupported intent is rejected with an explanation. |
| SSRF / internal access | Only http/https. DNS is resolved and private, loopback, link-local and reserved IPs are blocked. `.local`/`.internal` names, ports other than 80/443/8080/8443 and URLs with credentials are refused. The crawler re-checks after every redirect. |
| Runaway jobs | Wall-clock timeout (default 90s) kills the whole process group. CPU and file-size rlimits. Caps on pages (25), depth (3), page size (2 MB) and stdout size (8 MB). A Cancel button is available. Closing the browser cancels the job. |
| Secrets leakage | The child process gets a minimal environment with no API keys. stderr, logs and JSON output pass through `redact()`. `.env` is git-ignored. |
| Prompt injection from websites | Scraped text goes to the model inside `<untrusted_website_content>` with instructions to ignore any commands in it. The summarising call has **no tools**, so injected text can't trigger actions. The model is told to stay grounded in the content. |
| XSS from scraped content | The frontend HTML-escapes everything before rendering. |
| Abuse | Rate limit of 10 chat requests per minute per client. A polite crawl delay that honours robots `Crawl-delay`. |
| Dishonest output | Success is shown only if the process exits with code 0 or 3 **and** the stdout JSON validates. Otherwise the chat shows a Failed, Partial, Timeout or Cancelled status with the reason. |

---

## Setup

**Prerequisites:** Python 3.10+ and internet access.

```bash
git clone <this-repo> && cd WorkElite-Hackathon
./run.sh                      # creates .venv, installs requirements, starts the server
# open http://localhost:8765
```
To set it up by hand:
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional: add ANTHROPIC_API_KEY
.venv/bin/uvicorn backend.app:app --port 8765
```

### Environment variables
Copy `.env.example` to `.env` and fill in a key. The server reads `.env` when it starts, so **restart it after any change**.

| Var | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `openrouter` | `openrouter` or `anthropic`. |
| `OPENROUTER_API_KEY` | *(empty)* | OpenRouter key. Enables the LLM for intent parsing and grounded summaries. |
| `OPENROUTER_MODEL` | `anthropic/claude-haiku-5.5` | Any OpenRouter model ID. |
| `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` | *(empty)* / `claude-opus-5-5` | Direct Anthropic API, used when `LLM_PROVIDER=anthropic`. |
| `USE_LLM` | `auto` | `off` forces the deterministic rules mode. |
| `CLI_TIMEOUT_S` | `90` | Hard limit per CLI run, in seconds. |

Check which mode is active with `curl localhost:8765/api/health`, which returns `"llm": true, "model": "openrouter:anthropic/claude-haiku-5.5"`. The pill in the UI header shows the same. Keys are never returned by the API. They aren't passed to the CLI subprocess, and they're redacted from logs.

**How the LLM is used safely:** `backend/llm.py` asks the model for JSON that matches a Pydantic schema (`TaskSpec` for intent, `Insights` for the summary) and **validates** the reply. If the reply is invalid, the call fails or the request times out, the app falls back to the deterministic engine. The model never chooses commands. URLs it returns are kept only if they appear in your message, and the page and depth limits are clamped again after the model responds.

**No key? It still works end to end.** Without a key, intent parsing is rule-based and the summary is extractive (sentences quoted from the site), and the JSON is labelled `generated_by: {"intent":"rules","summary":"extractive"}`. With a key the label is `{"intent":"llm","summary":"llm"}`.

### Testing the demo
1. Click **"Mandatory demo: example.com"**, or type the prompt from the brief.
2. Watch the stage chips (Intent → CLI execution → Validate & respond), the plan, the **live CLI terminal** with its `$ sitecrawl …` command, and the exit-code chip.
3. Read the summary, then expand, copy or download the JSON.
4. You can share a link that runs a prompt automatically: `http://localhost:8765/?q=Crawl%20https://example.com`
5. Try the edge-case chips: a domain that doesn't exist, an SSRF attempt (`127.0.0.1`) and an unsafe request (`rm -rf /`).

You can also use the CLI directly: `python sitecrawl/cli.py https://example.com --max-pages 3 | python -m json.tool`

### Tests
```bash
.venv/bin/python -m pytest -q     # 21 tests: SSRF/validation, intent, CLI contract, timeout kill, env isolation, evidence check
```

## Project layout
```
backend/
  app.py           FastAPI + SSE pipeline, rate limit, cancel endpoint
  intent.py        natural language → TaskSpec (LLM | rules)
  llm.py           provider layer: OpenRouter / Anthropic, schema validation, fallback
  orchestrator.py  approved-tool registry, subprocess runner, timeout/cancel, capture
  responder.py     output validation, grounded summary, final JSON
  verify.py        evidence check: quotes matched against crawled text
  security.py      URL/SSRF validation, secret redaction
  config.py        env config
sitecrawl/cli.py   the approved CLI tool
frontend/index.html chat UI (vanilla JS on applecn tokens, no build step)
frontend/vendor/   applecn tokens.css + LICENSE (MIT)
tests/test_core.py
```

## Tech stack and AI disclosure

| Part | What we used |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn, Pydantic |
| Crawler CLI | Python with `requests` and BeautifulSoup/lxml |
| Frontend | Plain HTML, CSS and JavaScript (no build step), live updates over Server-Sent Events |
| UI design | [applecn](https://github.com/STiXzoOR/applecn) (MIT): Apple's design tokens, with a Web / macOS / iOS switch and Light / Dark mode |
| AI model | Claude Haiku 5.5 through OpenRouter (or the Anthropic API). Optional; the app falls back to simple rules without a key |
| Testing | pytest (21 tests) |
| AI coding assistant | Claude Code helped write the code. We reviewed every part and can explain it |

Open-source projects we researched but did not copy: Crawl4AI, CrawlGPT, WebSage.

## Limitations and next steps
* Static HTML only. JavaScript-rendered sites would need a browser-based tool (e.g. `crwl` or Playwright) added to the registry.
* Jobs run in memory in one process. For production, add a job queue (Celery/RQ) and container-level sandboxing (Docker, seccomp, network egress policy).

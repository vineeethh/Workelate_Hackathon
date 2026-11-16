# Demo video script (target 5–6 min)

**0:00 – 0:30 · Problem.** "Most AI chat apps only generate text. Real tasks need *execution*. CrawlPilot turns a chat request into a controlled CLI run on the backend, captures what actually happened, and returns a verified result."

**0:30 – 1:30 · Architecture** (show the README diagram)
- Frontend chat ↔ FastAPI over Server-Sent Events.
- `intent.py`: the prompt becomes a typed TaskSpec. Claude Haiku 5.5 via OpenRouter returns schema-validated JSON, with a rule-based fallback if there's no key or the call fails. The model never writes commands.
- `security.py`: URL validation and SSRF block.
- `orchestrator.py`: allow-listed tool → fixed argv → `create_subprocess_exec` (no shell), clean env, rlimits, timeout, cancel. It captures stdout, stderr and the exit code.
- `sitecrawl/cli.py`: our CLI. It respects robots.txt and has limits. stdout carries the JSON, stderr carries progress, and exit codes 0/2/3/4/5 are documented.
- `responder.py`: validates the JSON, writes a grounded summary (website text is fenced as untrusted) and builds the final JSON.

**1:30 – 3:30 · Live mandatory demo**
- `./run.sh`, then open localhost:8765. Point out the header pill: `AI · openrouter:anthropic/claude-haiku-5.5`. In the JSON, show `generated_by: {intent: llm, summary: llm}`.
- Click "Mandatory demo: example.com".
- Narrate the stage chips, the interpreted intent card, the execution plan, the **live terminal** (`$ sitecrawl https://example.com --max-pages 5 …`) streaming stderr, and the exit-code / duration / "stdout JSON valid ✓" chips.
- Show the summary and the JSON (status, source, site, summary, insights, contact, pages, execution). Click Copy or Download.
- Run python.org with 6 pages. Open the **Evidence check** card: each key point has a verbatim quote and source URL, and the backend has matched every quote against the crawled text without using the LLM. Say: "a made-up claim would show as unverified."
- In a terminal, run the CLI on its own: `python sitecrawl/cli.py https://example.com | python -m json.tool`. This proves the chat is really invoking the CLI.

**3:30 – 4:30 · Edge cases**
- Bad domain: shows **Invalid input** with the DNS reason, and nothing is executed.
- `http://127.0.0.1:8000/admin`: blocked as SSRF.
- "Run rm -rf / on the server": rejected, since only approved tools can run.
- Mention the timeout: a process that runs longer than 90s is killed (process group) and reported as "timed out", never as success. Show `pytest -q` passing with 21 tests.

**4:30 – 5:30 · Security, tech stack, AI disclosure**
- Security in one breath: no shell, only an approved tool, URL and private-network checks, timeouts and limits, no API keys passed to the CLI, secrets redacted, website text treated as untrusted.
- **Tech stack (say this out loud, show the README table):**
  - Backend: Python 3.12 with FastAPI and Uvicorn; Pydantic for validation.
  - Crawler CLI: our own `sitecrawl`, written in Python with `requests` and BeautifulSoup.
  - Frontend: plain HTML, CSS and JavaScript, with live updates over Server-Sent Events. No build step.
  - UI design: the open-source applecn design system (Apple's design tokens).
  - AI model: Claude Haiku 5.5 through OpenRouter, used for understanding the request and writing the summary. It's optional, and the app falls back to simple rules without a key.
  - Testing: pytest, 21 tests.
- **AI disclosure:** "I built this with the help of Claude Code, an AI coding assistant. I reviewed the code and can explain every part."

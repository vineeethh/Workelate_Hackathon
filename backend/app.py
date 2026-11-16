"""
FastAPI entry point.

POST /api/chat           -> Server-Sent Events stream of the whole pipeline:
                            intent -> plan -> exec_start -> exec_log* -> exec_end -> result | error
POST /api/cancel/{job}   -> kill a running CLI job
GET  /api/health         -> status + which engines are active (never exposes secrets)
GET  /                   -> chat UI
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .intent import parse_intent
from .orchestrator import TOOLS, cancel_job, run_tool
from .responder import build_response
from .security import redact, validate_target_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

app = FastAPI(title="CrawlPilot", version="1.0.0")
app.mount("/static", StaticFiles(directory=config.ROOT / "frontend"), name="static")

_RATE: dict[str, list[float]] = {}
RATE_LIMIT = 10  # chat requests per minute per client


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


HELP_TEXT = (
    "I'm **CrawlPilot**. I turn plain-English requests into controlled CLI runs. Right now I can run one "
    "approved tool, `sitecrawl`, which crawls a public website (respecting robots.txt, with page/depth/time "
    "limits), extracts the content, and returns a **summary** plus **structured JSON**.\n\n"
    "Try: *\"Crawl https://example.com, extract the important information, give me a summary, and return the "
    "data in JSON\"* or *\"Crawl 8 pages of https://www.python.org and extract the upcoming events\"*."
)


async def pipeline(message: str, job_id: str):
    t0 = time.monotonic()
    # 1) intent
    yield sse({"type": "stage", "stage": "intent", "label": "Understanding your request"})
    spec, engine = await asyncio.to_thread(parse_intent, message)
    yield sse({"type": "intent", "engine": engine, "spec": spec.model_dump()})

    if spec.task_type == "help":
        yield sse({"type": "result", "status": "info", "markdown": HELP_TEXT, "data": None})
        return
    if spec.task_type == "unsupported":
        yield sse({"type": "result", "status": "rejected", "data": None, "markdown":
                   "I can't do that. For safety I can only run the approved `sitecrawl` tool on public websites "
                   "(no arbitrary shell commands, files, or internal systems). Give me a website URL to crawl."})
        return

    # 2) validate inputs before anything executes
    try:
        spec.url = validate_target_url(spec.url or "")
    except ValueError as e:
        yield sse({"type": "error", "stage": "validation", "message": str(e)})
        yield sse({"type": "result", "status": "invalid_input", "data": None,
                   "markdown": f"**Invalid input** - {e}"})
        return

    spec.max_pages = max(1, min(spec.max_pages, config.HARD_MAX_PAGES))
    spec.max_depth = max(0, min(spec.max_depth, config.HARD_MAX_DEPTH))
    plan = [
        f"Validate URL `{spec.url}` (scheme, DNS, private-network block)",
        f"Run approved CLI `sitecrawl` (≤{spec.max_pages} pages, depth ≤{spec.max_depth}, "
        f"{config.CLI_TIMEOUT_S}s timeout, robots.txt respected)",
        "Capture stdout (JSON), stderr (logs) and exit code",
        "Validate & parse result" + (", summarise" if spec.want_summary else "") +
        (", build structured JSON" if spec.want_json else ""),
    ]
    yield sse({"type": "plan", "steps": plan})

    # 3) execute CLI
    yield sse({"type": "stage", "stage": "execute", "label": "Running sitecrawl CLI"})
    ex = None
    try:
        async for ev in run_tool(spec, job_id):
            if ev["type"] == "exec_result":
                ex = ev["result"]
            else:
                yield sse(ev)
    except ValueError as e:
        yield sse({"type": "result", "status": "invalid_input", "data": None, "markdown": f"**Invalid input** - {e}"})
        return
    except Exception as e:  # noqa: BLE001
        log.exception("execution failure")
        yield sse({"type": "result", "status": "failed", "data": None,
                   "markdown": f"**Execution error** - could not start the CLI ({type(e).__name__})."})
        return

    exd = asdict(ex)
    exd.pop("stdout_json", None)
    yield sse({"type": "exec_end", **exd})

    # 4) process & respond
    yield sse({"type": "stage", "stage": "process", "label": "Validating output & writing the answer"})
    resp = await asyncio.to_thread(build_response, spec, ex, engine)
    resp["total_ms"] = int((time.monotonic() - t0) * 1000)
    yield sse({"type": "result", **resp})


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    client = request.client.host if request.client else "?"
    now = time.time()
    hist = [t for t in _RATE.get(client, []) if now - t < 60]
    if len(hist) >= RATE_LIMIT:
        return JSONResponse({"error": "Rate limit exceeded, try again in a minute."}, status_code=429)
    _RATE[client] = hist + [now]

    job_id = uuid.uuid4().hex[:12]
    log.info("chat job=%s msg=%r", job_id, redact(body.message[:200]))

    async def stream():
        yield sse({"type": "job", "job_id": job_id})
        try:
            async for chunk in pipeline(body.message, job_id):
                if await request.is_disconnected():
                    cancel_job(job_id)
                    break
                yield chunk
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline crash")
            yield sse({"type": "result", "status": "failed", "data": None,
                       "markdown": f"**Internal error** ({type(e).__name__}). Nothing was claimed as done."})
        yield sse({"type": "done"})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/cancel/{job_id}")
async def cancel(job_id: str):
    return {"cancelled": cancel_job(job_id)}


@app.get("/api/health")
async def health():
    return {"ok": True, "llm": config.llm_enabled(), "model": config.llm_label(),
            "tools": {k: v.description for k, v in TOOLS.items()}, "cli_timeout_s": config.CLI_TIMEOUT_S}


@app.get("/")
async def index():
    return FileResponse(config.ROOT / "frontend" / "index.html")

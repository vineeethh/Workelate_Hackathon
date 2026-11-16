"""
Backend orchestration layer: selects an APPROVED CLI tool, builds a validated argv,
starts it as a subprocess (no shell), streams its stderr live, captures stdout /
stderr / exit code, enforces timeouts, supports cancellation.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from . import config
from .intent import TaskSpec
from .security import redact, validate_target_url

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover
    resource = None


# ----------------------------------------------------------------- tool registry (allow-list)
@dataclass(frozen=True)
class ApprovedTool:
    name: str
    description: str
    build_argv: Callable[[TaskSpec, str], list[str]]
    exit_codes: dict[int, str]


def _sitecrawl_argv(spec: TaskSpec, url: str) -> list[str]:
    pages = max(1, min(int(spec.max_pages), config.HARD_MAX_PAGES))
    depth = max(0, min(int(spec.max_depth), config.HARD_MAX_DEPTH))
    # argv list -> passed straight to execve, never through a shell. URL already validated.
    return [config.PYTHON, "-I", str(config.ROOT / "sitecrawl" / "cli.py"), url,
            "--max-pages", str(pages), "--max-depth", str(depth),
            "--timeout", "10", "--delay", "0.3", "--max-chars", "4000"]


TOOLS: dict[str, ApprovedTool] = {
    "sitecrawl": ApprovedTool(
        name="sitecrawl",
        description="Bounded, robots.txt-respecting website crawler that emits one JSON document on stdout.",
        build_argv=_sitecrawl_argv,
        exit_codes={0: "success", 1: "unexpected error", 2: "invalid input", 3: "partial result",
                    4: "site unreachable", 5: "blocked by robots.txt", -9: "killed (timeout/cancel)"},
    ),
}
TASK_TO_TOOL = {"crawl_website": "sitecrawl"}


# ----------------------------------------------------------------- execution
@dataclass
class ExecutionResult:
    job_id: str
    tool: str
    argv_display: list[str]
    exit_code: int | None
    exit_meaning: str
    duration_ms: int
    stdout_json: dict | None
    stderr_tail: list[str]
    timed_out: bool = False
    cancelled: bool = False
    parse_error: str | None = None
    stdout_bytes: int = 0


@dataclass
class Job:
    job_id: str
    proc: asyncio.subprocess.Process | None = None
    cancelled: bool = False
    events: list = field(default_factory=list)


JOBS: dict[str, Job] = {}


def _limits():  # runs in the child before exec
    os.setsid()  # own process group -> we can kill the whole tree
    if resource is not None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
            resource.setrlimit(resource.RLIMIT_FSIZE, (50_000_000, 50_000_000))
        except (ValueError, OSError):
            pass


def _child_env(workdir: str) -> dict[str, str]:
    """Least privilege: the CLI gets NO secrets (API keys etc. are not inherited)."""
    keep = {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "SYSTEMROOT"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update({"HOME": workdir, "TMPDIR": workdir, "PYTHONPATH": str(config.ROOT),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
    return env


def cancel_job(job_id: str) -> bool:
    job = JOBS.get(job_id)
    if not job or not job.proc or job.proc.returncode is not None:
        return False
    job.cancelled = True
    try:
        os.killpg(job.proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


async def run_tool(spec: TaskSpec, job_id: str | None = None) -> AsyncIterator[dict]:
    """Async generator of execution events; the last event has type 'exec_result'."""
    job_id = job_id or uuid.uuid4().hex[:12]
    tool_name = TASK_TO_TOOL.get(spec.task_type)
    if tool_name not in TOOLS:
        raise ValueError(f"No approved tool for task '{spec.task_type}'")
    tool = TOOLS[tool_name]
    url = validate_target_url(spec.url or "")  # raises ValueError on invalid/forbidden input
    argv = tool.build_argv(spec, url)
    display = ["sitecrawl", *argv[3:]]  # hide interpreter path in UI

    job = Job(job_id)
    JOBS[job_id] = job
    workdir = tempfile.mkdtemp(prefix="sitecrawl-job-")
    yield {"type": "exec_start", "job_id": job_id, "tool": tool.name, "argv": display,
           "timeout_s": config.CLI_TIMEOUT_S}

    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(config.ROOT), env=_child_env(workdir), preexec_fn=_limits,
    )
    job.proc = proc
    yield {"type": "exec_pid", "pid": proc.pid}

    stderr_lines: list[str] = []
    stdout_chunks: list[bytes] = []
    stdout_size = 0
    queue: asyncio.Queue = asyncio.Queue()

    async def pump_stderr():
        assert proc.stderr
        async for raw in proc.stderr:
            line = redact(raw.decode("utf-8", "replace").rstrip())
            stderr_lines.append(line)
            await queue.put(line)
        await queue.put(None)

    async def pump_stdout():
        nonlocal stdout_size
        assert proc.stdout
        while chunk := await proc.stdout.read(65536):
            stdout_size += len(chunk)
            if stdout_size <= config.MAX_STDOUT_BYTES:
                stdout_chunks.append(chunk)

    t_err = asyncio.create_task(pump_stderr())
    t_out = asyncio.create_task(pump_stdout())
    timed_out = False
    deadline = t0 + config.CLI_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        try:
            line = await asyncio.wait_for(queue.get(), timeout=min(remaining, 1.0))
        except asyncio.TimeoutError:
            if job.cancelled:
                break
            yield {"type": "heartbeat", "elapsed_ms": int((time.monotonic() - t0) * 1000)}
            continue
        if line is None:
            break
        yield {"type": "exec_log", "line": line}

    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    await asyncio.gather(t_err, t_out, return_exceptions=True)
    duration = int((time.monotonic() - t0) * 1000)

    # ---- validate & parse stdout
    parsed, perr = None, None
    raw_out = b"".join(stdout_chunks).decode("utf-8", "replace").strip()
    if stdout_size > config.MAX_STDOUT_BYTES:
        perr = "stdout exceeded size limit"
    elif raw_out:
        try:
            parsed = json.loads(raw_out)
            if not isinstance(parsed, dict) or parsed.get("tool") != "sitecrawl":
                parsed, perr = None, "stdout was JSON but not a sitecrawl result"
        except json.JSONDecodeError as e:
            perr = f"stdout was not valid JSON ({e.msg})"
    else:
        perr = "no output on stdout"

    code = proc.returncode
    meaning = "cancelled by user" if job.cancelled else ("timed out" if timed_out else
                                                         tool.exit_codes.get(code, f"exit code {code}"))
    JOBS.pop(job_id, None)
    try:
        os.rmdir(workdir)
    except OSError:
        pass
    yield {"type": "exec_result", "result": ExecutionResult(
        job_id=job_id, tool=tool.name, argv_display=display, exit_code=code, exit_meaning=meaning,
        duration_ms=duration, stdout_json=parsed, stderr_tail=stderr_lines[-40:],
        timed_out=timed_out, cancelled=job.cancelled, parse_error=perr, stdout_bytes=stdout_size)}

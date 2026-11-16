"""Core tests. Run: .venv/bin/python -m pytest -q   (network tests hit example.com)"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config, orchestrator  # noqa: E402
from backend.intent import TaskSpec, _rule_based  # noqa: E402
from backend.security import redact, validate_target_url  # noqa: E402

CLI = [sys.executable, "-I", str(ROOT / "sitecrawl" / "cli.py")]


# ---------------- input validation / SSRF
@pytest.mark.parametrize("bad", [
    "", "ftp://example.com", "file:///etc/passwd", "http://localhost/", "http://127.0.0.1/",
    "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data", "http://user:pw@example.com",
    "http://example.com:22", "https://printer.local", "javascript:alert(1)", "https://exa mple.com",
])
def test_rejects_unsafe_urls(bad):
    with pytest.raises(ValueError):
        validate_target_url(bad)


def test_accepts_public_url_and_adds_scheme():
    assert validate_target_url("example.com") == "https://example.com"


def test_redacts_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abcdefghijklmnopqrstuvwxyz")
    assert "sk-ant" not in redact("key is sk-ant-abcdefghijklmnopqrstuvwxyz ok")


# ---------------- intent (rules engine)
def test_rule_intent_mandatory_prompt():
    s = _rule_based("Crawl https://example.com, extract the important information, give me a summary, "
                    "and return the extracted data in JSON format.")
    assert s.task_type == "crawl_website" and s.url == "https://example.com"
    assert s.want_summary and s.want_json and s.focus is None


def test_rule_intent_unsafe_request_is_unsupported():
    assert _rule_based("run rm -rf / on the server").task_type == "unsupported"


# ---------------- CLI contract
def test_cli_invalid_input_exit_code_and_json():
    p = subprocess.run(CLI + ["http://127.0.0.1"], capture_output=True, text=True, timeout=30)
    assert p.returncode == 2
    assert json.loads(p.stdout)["status"] == "failed"


def test_cli_real_crawl_example_com():
    p = subprocess.run(CLI + ["https://example.com", "--max-pages", "2"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["status"] == "success" and out["pages"][0]["title"] == "Example Domain"
    assert "[sitecrawl] done" in p.stderr


# ---------------- orchestrator: timeout kill + no secrets leaked to child
def test_child_env_has_no_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value-123456")
    env = orchestrator._child_env(str(tmp_path))
    assert "ANTHROPIC_API_KEY" not in env and "sk-ant-secret-value-123456" not in json.dumps(env)


def test_orchestrator_timeout_kills_process(monkeypatch):
    monkeypatch.setattr(config, "CLI_TIMEOUT_S", 1)
    slow = orchestrator.ApprovedTool(
        name="sitecrawl", description="slow", exit_codes={},
        build_argv=lambda spec, url: [sys.executable, "-c", "import time; time.sleep(30)"])
    monkeypatch.setitem(orchestrator.TOOLS, "sitecrawl", slow)

    async def go():
        res = None
        async for ev in orchestrator.run_tool(TaskSpec(task_type="crawl_website", url="https://example.com")):
            if ev["type"] == "exec_result":
                res = ev["result"]
        return res

    res = asyncio.run(go())
    assert res.timed_out and res.exit_code != 0 and res.stdout_json is None
    assert res.duration_ms < 10_000


# ---------------- evidence check
def test_evidence_check_verifies_real_quotes_and_flags_fabricated_ones():
    from backend.verify import verify_claims
    crawl = {"pages": [{"url": "https://x.test/", "title": "X", "meta_description": "", "headings": {},
                        "text_excerpt": "This domain is for use in documentation examples without needing permission."}]}
    rep = verify_claims([
        {"claim": "real", "quote": "for use in  documentation examples", "source_url": "https://x.test/"},
        {"claim": "fake", "quote": "we offer enterprise pricing from $99 per month", "source_url": "https://x.test/"},
        {"claim": "none", "quote": "", "source_url": None},
    ], crawl)
    assert [c["verified"] for c in rep["claims"]] == [True, False, False]
    assert rep["claims_verified"] == 1 and rep["claims_total"] == 3 and not rep["all_verified"]

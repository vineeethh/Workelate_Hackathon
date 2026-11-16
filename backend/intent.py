"""
Intent-understanding layer.

Turns a free-form chat message into a validated, typed TaskSpec.
* Primary: Claude (structured outputs -> pydantic model), when ANTHROPIC_API_KEY is set.
* Fallback: deterministic rule-based parser (works offline / without a key).

The LLM NEVER produces shell commands. It only fills a constrained schema; the
orchestrator maps that schema onto an allow-listed CLI with clamped arguments.
"""
from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import config

log = logging.getLogger("intent")

TaskType = Literal["crawl_website", "help", "unsupported"]


class TaskSpec(BaseModel):
    task_type: TaskType = Field(description="crawl_website if the user wants a site crawled/summarised/extracted; "
                                            "help for greetings or questions about capabilities; unsupported otherwise")
    url: Optional[str] = Field(default=None, description="The website URL exactly as given by the user, if any")
    want_summary: bool = Field(default=True, description="User wants a natural-language summary")
    want_json: bool = Field(default=True, description="User wants structured JSON output")
    max_pages: int = Field(default=config.DEFAULT_MAX_PAGES, description="Pages to crawl (1-25)")
    max_depth: int = Field(default=config.DEFAULT_MAX_DEPTH, description="Link depth from start page (0-3)")
    focus: Optional[str] = Field(default=None, description="Specific information the user asked to extract, "
                                                           "e.g. 'pricing', 'contact details', 'team members'")
    reasoning: str = Field(default="", description="One short sentence explaining the interpretation")


URL_RE = re.compile(r"(https?://[^\s<>\"'`)\]]+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/[^\s<>\"'`)\]]*)?)", re.I)
CRAWL_WORDS = re.compile(r"\b(crawl|scrape|summar|extract|analy[sz]e|visit|read|fetch|look at|go to|info|details|json)", re.I)
HELP_WORDS = re.compile(r"^\s*(hi|hello|hey|help|what can you do|who are you)\b", re.I)


def _rule_based(message: str) -> TaskSpec:
    text = message.strip()
    m = URL_RE.search(text)
    url = m.group(0).rstrip(".,;:!?") if m else None
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    low = text.lower()
    pages = re.search(r"(\d{1,2})\s*(?:pages?|urls?|links?)", low)
    depth = re.search(r"depth\s*(?:of\s*)?(\d)", low)
    focus = None
    fm = re.search(r"(?:extract|find|get|list)\s+(?:the\s+|all\s+)?([a-z ,&-]{3,60}?)(?:\s+from|\s+on|\s+in|,|\.|$)", low)
    generic = {"important", "information", "info", "data", "details", "content", "key", "main", "relevant",
               "useful", "extracted", "everything", "it", "the", "all", "and", "summary", "json"}
    if fm:
        cand = re.sub(r"\s+(as|in|into)\s+(json|structured).*$", "", fm.group(1).strip())
        if not set(re.findall(r"[a-z]+", cand)) <= generic:
            focus = cand
    if url:
        ttype: TaskType = "crawl_website"
    elif HELP_WORDS.search(text):
        ttype = "help"
    elif CRAWL_WORDS.search(text):
        ttype = "crawl_website"  # crawl intent without URL -> orchestrator asks for one
    else:
        ttype = "unsupported"
    wants_json = "json" in low or "structured" in low
    wants_summary = "summar" in low or "overview" in low or "about" in low
    if not wants_json and not wants_summary:
        wants_json = wants_summary = True
    return TaskSpec(
        task_type=ttype, url=url,
        want_summary=wants_summary, want_json=wants_json,
        max_pages=int(pages.group(1)) if pages else config.DEFAULT_MAX_PAGES,
        max_depth=int(depth.group(1)) if depth else config.DEFAULT_MAX_DEPTH,
        focus=focus,
        reasoning="Parsed with deterministic rules (LLM not configured or unavailable).",
    )


SYSTEM = """You are the intent parser for a chat app that can run ONE approved tool: a website crawler CLI.
Map the user's message to the TaskSpec schema. Rules:
- Copy the URL exactly as the user wrote it; never invent a URL. If none is given, leave url null.
- Default to want_summary=true and want_json=true unless the user clearly asks for only one.
- max_pages default 5 (1-25); max_depth default 1 (0-3). Only change them if the user asks.
- If the user asks for anything other than crawling/summarising/extracting from a website
  (e.g. run shell commands, delete files, hack, access internal systems), use task_type=unsupported.
- focus: only set it for a SPECIFIC topic (e.g. 'pricing', 'authors', 'contact details'); leave it null for
  generic phrases like 'important information', 'key data' or 'everything'.
- You only classify. You never produce commands."""


def parse_intent(message: str) -> tuple[TaskSpec, str]:
    """Returns (spec, engine) where engine is 'claude' or 'rules'."""
    from . import llm
    if llm.provider() is None:
        return _rule_based(message), "rules"
    spec = llm.complete_json(SYSTEM, message[:4000], TaskSpec, max_tokens=1500, timeout=30)
    if spec is None:
        log.warning("intent: LLM unavailable or invalid output; using rules")
        return _rule_based(message), "rules"
    # The model must not invent a URL: keep it only if it literally appears in the message.
    if spec.url and spec.url.replace("https://", "").replace("http://", "").rstrip("/") not in message:
        spec.url = _rule_based(message).url
    spec.max_pages = max(1, min(int(spec.max_pages), config.HARD_MAX_PAGES))
    spec.max_depth = max(0, min(int(spec.max_depth), config.HARD_MAX_DEPTH))
    return spec, "llm"

"""
Evidence check: every key point must be backed by a verbatim quote that the backend
can find in the text the CLI actually crawled. This is a deterministic string check
(no LLM involved), so a hallucinated quote cannot verify itself.
"""
from __future__ import annotations

import re
import unicodedata

MIN_QUOTE_CHARS = 12
_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", " ": " ", "…": "..."})


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_TRANS).lower()
    return re.sub(r"\s+", " ", text).strip()


def _page_corpus(page: dict) -> str:
    h = page.get("headings", {}) or {}
    parts = [page.get("title", ""), page.get("meta_description", ""), page.get("text_excerpt", "")]
    parts += [x for lvl in ("h1", "h2", "h3") for x in h.get(lvl, [])]
    return _norm(" \n ".join(parts))


def verify_claims(claims: list[dict], crawl: dict) -> dict:
    """claims: [{claim, quote, source_url}] -> verification report with per-claim verdicts."""
    corpora = {p["url"]: _page_corpus(p) for p in crawl.get("pages", [])}
    out = []
    for c in claims:
        quote = (c.get("quote") or "").strip().strip('"\'').strip()
        q = _norm(quote).strip(" .…")
        verdict, found_on, reason = False, None, None
        if len(q) < MIN_QUOTE_CHARS:
            reason = "no supporting quote provided" if not q else "quote too short to be meaningful"
        else:
            cited = c.get("source_url")
            # Cited page first, then any crawled page.
            order = ([cited] if cited in corpora else []) + [u for u in corpora if u != cited]
            for url in order:
                if q in corpora[url]:
                    verdict, found_on = True, url
                    break
            if not verdict:
                reason = "quote not found in the crawled text"
        out.append({"claim": c.get("claim", ""), "quote": quote[:300], "source_url": found_on or c.get("source_url"),
                    "verified": verdict, **({"reason": reason} if reason else {})})
    total, ok = len(out), sum(1 for c in out if c["verified"])
    return {
        "method": "verbatim quote matched against crawled page text (deterministic, no LLM)",
        "claims_total": total, "claims_verified": ok,
        "all_verified": total > 0 and ok == total,
        "claims": out,
    }

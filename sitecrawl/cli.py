#!/usr/bin/env python3
"""
sitecrawl - a small, bounded, polite website crawler CLI.

This is the *approved CLI tool* the chat backend invokes as a subprocess.
It can also be used standalone:

    python -m sitecrawl.cli https://example.com --max-pages 5 --max-depth 1

Contract
--------
* stdout : exactly ONE JSON document (the crawl result) - nothing else.
* stderr : human-readable progress logs, one event per line, prefixed "[sitecrawl]".
* exit code:
    0  success           - all requested pages crawled
    3  partial           - some pages crawled, some failed
    4  unreachable       - start URL could not be fetched
    5  blocked by robots - robots.txt disallows the start URL
    2  invalid input     - bad URL / arguments / forbidden target (SSRF guard)
    1  unexpected error
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urldefrag, urlparse
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

VERSION = "1.0.0"
USER_AGENT = f"sitecrawl/{VERSION} (+hackathon prototype; polite bot)"

EXIT_OK, EXIT_ERROR, EXIT_INVALID, EXIT_PARTIAL, EXIT_UNREACHABLE, EXIT_ROBOTS = 0, 1, 2, 3, 4, 5

HARD_MAX_PAGES = 25
HARD_MAX_DEPTH = 3
MAX_BYTES_PER_PAGE = 2_000_000
SKIP_EXT = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|webp|ico|css|js|zip|gz|tar|mp4|mp3|avi|mov|woff2?|ttf|eot|xml|json|exe|dmg)$",
    re.I,
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{2,4})?")
SOCIAL_HOSTS = ("twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com",
                "youtube.com", "github.com", "tiktok.com", "mastodon.social")
STOPWORDS = set("""a about above after again against all am an and any are as at be because been before being
below between both but by can did do does doing down during each few for from further had has have having he her
here hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now of
off on once only or other our ours ourselves out over own same she should so some such than that the their theirs
them themselves then there these they this those through to too under until up very was we were what when where
which while who whom why will with you your yours yourself yourselves also may use used using one two new get
like make many much via per us page pages site website click here learn read more""".split())


def log(event: str, **fields) -> None:
    """Structured progress line on stderr (the backend streams these live)."""
    payload = " ".join(f"{k}={json.dumps(v)}" for k, v in fields.items())
    print(f"[sitecrawl] {event} {payload}".rstrip(), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- safety
def is_public_host(host: str) -> tuple[bool, str]:
    """SSRF guard: refuse loopback / private / link-local / reserved targets."""
    if not host:
        return False, "missing host"
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(".local"):
        return False, "local hostnames are not allowed"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "DNS resolution failed"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False, f"resolves to non-public address {ip}"
    return True, "ok"


def validate_url(url: str) -> tuple[bool, str]:
    if len(url) > 2048:
        return False, "URL too long"
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False, "only http/https URLs are allowed"
    if p.username or p.password:
        return False, "URLs with embedded credentials are not allowed"
    if p.port and p.port not in (80, 443, 8080, 8443):
        return False, f"port {p.port} is not allowed"
    return is_public_host(p.hostname or "")


def normalize(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    path = p.path or "/"
    return f"{p.scheme}://{p.netloc.lower()}{path}" + (f"?{p.query}" if p.query else "")


# --------------------------------------------------------------------------- extraction
def clean_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def extract_page(url: str, html: str, depth: int, status: int, max_chars: int) -> tuple[dict, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""

    def meta(name: str) -> str:
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        return (tag.get("content") or "").strip() if tag else ""

    headings = {lvl: [h.get_text(" ", strip=True)[:200] for h in soup.find_all(lvl)][:15]
                for lvl in ("h1", "h2", "h3")}
    lang = (soup.html.get("lang") or "").strip() if soup.html else ""

    base_host = urlparse(url).netloc.lower()
    internal, external, social = [], [], set()
    nav_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absu = normalize(urljoin(url, href))
        host = urlparse(absu).netloc.lower()
        if host == base_host:
            internal.append(absu)
        else:
            external.append(absu)
            if any(host.endswith(s) for s in SOCIAL_HOSTS):
                social.add(absu)
    for nav in soup.find_all(["nav", "header"])[:2]:
        for a in nav.find_all("a", href=True)[:20]:
            label = a.get_text(" ", strip=True)
            if label:
                nav_links.append({"label": label[:60], "url": normalize(urljoin(url, a["href"]))})

    mailtos = [a["href"][7:].split("?")[0] for a in soup.find_all("a", href=True) if a["href"].startswith("mailto:")]
    tels = [a["href"][4:] for a in soup.find_all("a", href=True) if a["href"].startswith("tel:")]
    images = len(soup.find_all("img"))

    text = clean_text(soup)
    emails = sorted(set(mailtos) | set(e for e in EMAIL_RE.findall(text) if not e.lower().endswith((".png", ".jpg"))))[:20]
    phones = sorted(set(tels) | set(p.strip() for p in PHONE_RE.findall(text) if sum(c.isdigit() for c in p) >= 9))[:10]

    page = {
        "url": url,
        "depth": depth,
        "status_code": status,
        "title": title[:300],
        "meta_description": meta("description")[:500] or meta("og:description")[:500],
        "language": lang,
        "headings": headings,
        "word_count": len(text.split()),
        "text_excerpt": text[:max_chars],
        "links": {"internal_count": len(set(internal)), "external_count": len(set(external))},
        "images_count": images,
        "emails": emails,
        "phones": phones,
        "social_links": sorted(social)[:15],
        "nav_links": nav_links[:20],
    }
    return page, list(dict.fromkeys(internal))


def top_keywords(texts: list[str], n: int = 15) -> list[dict]:
    words = Counter()
    for t in texts:
        for w in re.findall(r"[A-Za-z][A-Za-z-]{2,}", t.lower()):
            if w not in STOPWORDS:
                words[w] += 1
    return [{"term": w, "count": c} for w, c in words.most_common(n)]


# --------------------------------------------------------------------------- crawl
def crawl(start_url: str, max_pages: int, max_depth: int, timeout: float, delay: float,
          max_chars: int, respect_robots: bool) -> tuple[dict, int]:
    t0 = time.monotonic()
    start_url = normalize(start_url)
    origin = urlparse(start_url)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})

    robots_info = {"checked": False, "allowed": True, "url": f"{origin.scheme}://{origin.netloc}/robots.txt"}
    rp = None
    if respect_robots:
        rp = robotparser.RobotFileParser()
        try:
            r = session.get(robots_info["url"], timeout=timeout)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp.parse([])  # no robots.txt => everything allowed
            robots_info["checked"] = True
            robots_info["allowed"] = rp.can_fetch(USER_AGENT, start_url)
            crawl_delay = rp.crawl_delay(USER_AGENT)
            if crawl_delay:
                delay = max(delay, min(float(crawl_delay), 5.0))
        except requests.RequestException as e:
            log("robots_unavailable", error=str(e)[:200])
            rp = None
        log("robots", allowed=robots_info["allowed"], checked=robots_info["checked"])

    result = {
        "tool": "sitecrawl",
        "version": VERSION,
        "start_url": start_url,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "limits": {"max_pages": max_pages, "max_depth": max_depth, "timeout_s": timeout, "delay_s": delay},
        "robots": robots_info,
        "status": "failed",
        "pages": [],
        "errors": [],
    }

    if not robots_info["allowed"]:
        result["errors"].append({"url": start_url, "error": "Disallowed by robots.txt"})
        result["stats"] = {"pages_crawled": 0, "pages_failed": 0, "duration_ms": int((time.monotonic() - t0) * 1000)}
        return result, EXIT_ROBOTS

    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    seen = {start_url}
    total_bytes = 0

    while queue and len(result["pages"]) < max_pages:
        url, depth = queue.popleft()
        if rp is not None and not rp.can_fetch(USER_AGENT, url):
            log("skip_robots", url=url)
            continue
        log("fetch", url=url, depth=depth, n=len(result["pages"]) + 1, of=max_pages)
        try:
            resp = session.get(url, timeout=timeout, stream=True, allow_redirects=True)
            # Re-validate final destination after redirects (SSRF via redirect).
            ok, why = validate_url(resp.url)
            if not ok:
                raise ValueError(f"redirected to forbidden target: {why}")
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower():
                raise ValueError(f"non-HTML content-type: {ctype or 'unknown'}")
            raw = resp.raw.read(MAX_BYTES_PER_PAGE + 1, decode_content=True)
            truncated = len(raw) > MAX_BYTES_PER_PAGE
            total_bytes += len(raw)
            resp.encoding = resp.encoding or resp.apparent_encoding
            html = raw[:MAX_BYTES_PER_PAGE].decode(resp.encoding or "utf-8", errors="replace")
            if resp.status_code >= 400:
                raise ValueError(f"HTTP {resp.status_code}")
            page, links = extract_page(normalize(resp.url), html, depth, resp.status_code, max_chars)
            page["truncated"] = truncated
            result["pages"].append(page)
            log("ok", url=url, status=resp.status_code, words=page["word_count"], links=len(links))
            if depth < max_depth:
                for link in links:
                    if link not in seen and urlparse(link).netloc.lower() == origin.netloc.lower() \
                            and not SKIP_EXT.search(urlparse(link).path):
                        seen.add(link)
                        queue.append((link, depth + 1))
        except (requests.RequestException, ValueError) as e:
            msg = str(e)[:300]
            result["errors"].append({"url": url, "error": msg})
            log("error", url=url, error=msg)
        if queue and len(result["pages"]) < max_pages:
            time.sleep(delay)

    pages = result["pages"]
    if pages:
        first = pages[0]
        result["site"] = {
            "title": first["title"],
            "description": first["meta_description"],
            "language": first["language"],
            "emails": sorted({e for p in pages for e in p["emails"]})[:20],
            "phones": sorted({ph for p in pages for ph in p["phones"]})[:10],
            "social_links": sorted({s for p in pages for s in p["social_links"]})[:15],
            "nav_links": first["nav_links"],
            "top_keywords": top_keywords([p["text_excerpt"] for p in pages]),
        }

    result["stats"] = {
        "pages_crawled": len(pages),
        "pages_failed": len(result["errors"]),
        "pages_discovered": len(seen),
        "bytes_downloaded": total_bytes,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }
    if not pages:
        result["status"] = "failed"
        code = EXIT_UNREACHABLE
    elif result["errors"]:
        result["status"] = "partial"
        code = EXIT_PARTIAL
    else:
        result["status"] = "success"
        code = EXIT_OK
    log("done", status=result["status"], pages=len(pages), errors=len(result["errors"]),
        ms=result["stats"]["duration_ms"])
    return result, code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sitecrawl", description="Bounded, polite website crawler that emits JSON.")
    ap.add_argument("url", help="Start URL (http/https)")
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--max-depth", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout (s)")
    ap.add_argument("--delay", type=float, default=0.3, help="Delay between requests (s)")
    ap.add_argument("--max-chars", type=int, default=4000, help="Max text chars kept per page")
    ap.add_argument("--no-robots", action="store_true", help=argparse.SUPPRESS)  # not exposed by the backend
    ap.add_argument("--version", action="version", version=VERSION)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_INVALID if e.code else EXIT_OK

    ok, why = validate_url(args.url)
    if not ok:
        log("invalid_input", reason=why)
        print(json.dumps({"tool": "sitecrawl", "status": "failed", "start_url": args.url,
                          "errors": [{"url": args.url, "error": f"invalid input: {why}"}], "pages": []}))
        return EXIT_INVALID

    max_pages = max(1, min(args.max_pages, HARD_MAX_PAGES))
    max_depth = max(0, min(args.max_depth, HARD_MAX_DEPTH))
    timeout = max(1.0, min(args.timeout, 30.0))
    log("start", url=args.url, max_pages=max_pages, max_depth=max_depth)
    try:
        result, code = crawl(args.url, max_pages, max_depth, timeout, max(0.0, min(args.delay, 5.0)),
                             max(200, min(args.max_chars, 20000)), not args.no_robots)
    except Exception as e:  # noqa: BLE001 - last-resort guard; still emit valid JSON
        log("fatal", error=repr(e)[:300])
        print(json.dumps({"tool": "sitecrawl", "status": "failed", "start_url": args.url,
                          "errors": [{"url": args.url, "error": "unexpected crawler error"}], "pages": []}))
        return EXIT_ERROR
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())

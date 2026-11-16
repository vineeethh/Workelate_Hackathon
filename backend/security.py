"""Input validation, SSRF protection and secret redaction for the backend."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp")


def validate_target_url(url: str) -> str:
    """Return a normalised URL or raise ValueError with a user-safe message."""
    url = (url or "").strip()
    if not url:
        raise ValueError("No URL was provided. Please include the website address, e.g. https://example.com")
    if len(url) > 2048:
        raise ValueError("The URL is too long.")
    if any(c in url for c in "\x00\r\n\t "):
        raise ValueError("The URL contains invalid characters.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if p.scheme.lower() not in ("http", "https"):
        raise ValueError("Only http:// and https:// URLs are supported.")
    if p.username or p.password:
        raise ValueError("URLs containing credentials are not allowed.")
    host = (p.hostname or "").lower()
    if not host or "." not in host:
        raise ValueError(f"'{url}' does not look like a valid public website address.")
    if host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise ValueError("Local or internal hostnames are not allowed.")
    try:
        port = p.port
    except ValueError:
        raise ValueError("The URL has an invalid port.")
    if port not in ALLOWED_PORTS:
        raise ValueError(f"Port {port} is not allowed.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"The domain '{host}' could not be resolved (DNS lookup failed). Is it spelled correctly?")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError("That address points to a private/internal network, which is blocked for safety.")
    return url


_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
]


def redact(text: str) -> str:
    """Remove anything that looks like a secret, plus literal values of sensitive env vars."""
    if not text:
        return text
    for k, v in os.environ.items():
        if v and len(v) >= 8 and any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(v, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text

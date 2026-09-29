"""Bounded public-link and page-text checks for research citations.

DNS answers are validated before connecting
and the socket connects to the validated IP, preventing a second DNS lookup
from silently redirecting a probe to a private address. The optional, explicit
page inspection reads at most 128 KiB of public HTML/plain text. It never
stores a page or treats text overlap as proof of a factual claim.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import urllib.parse
import codecs
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any


MAX_REDIRECTS = 2
MAX_PAGE_BYTES = 128 * 1024


class UnsafeSource(ValueError):
    pass


def _target(url: str) -> tuple[str, str, int, str]:
    if not isinstance(url, str) or not 1 <= len(url) <= 2_000 or any(ord(char) < 32 for char in url):
        raise UnsafeSource("invalid URL length or control character")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeSource("only public HTTP(S) URLs without credentials are allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeSource("invalid port") from exc
    if port != (443 if parsed.scheme == "https" else 80):
        raise UnsafeSource("nonstandard ports are not probed")
    host = parsed.hostname.encode("idna").decode("ascii")
    path = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parsed.query, safe="/%:@!$&'()*+,;=?-._~")
    request_target = path + (f"?{query}" if query else "")
    return parsed.scheme, host, port, request_target


def _public_addresses(host: str, port: int) -> list[tuple[Any, ...]]:
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if not answers or len(answers) > 32:
        raise UnsafeSource("DNS returned no usable address set")
    for _family, _kind, _proto, _canon, sockaddr in answers:
        address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        if not address.is_global:
            raise UnsafeSource("source resolves to a private or non-global address")
    return answers


def _request(url: str, method: str, timeout: float = 3.0) -> tuple[int, str, str, str, bytes]:
    scheme, host, port, request_target = _target(url)
    answers = _public_addresses(host, port)
    last_error: OSError | None = None
    for family, kind, proto, _canon, sockaddr in answers[:4]:
        try:
            with socket.socket(family, kind, proto) as raw:
                raw.settimeout(timeout)
                raw.connect(sockaddr)
                if scheme == "https":
                    with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as connection:
                        return _exchange(connection, host, request_target, method)
                return _exchange(raw, host, request_target, method)
        except (OSError, ssl.SSLError) as exc:
            last_error = exc
    raise OSError(str(last_error or "connection failed"))


def _head(url: str, timeout: float = 3.0) -> tuple[int, str, str, str]:
    status, location, last_modified, content_type, _ = _request(url, "HEAD", timeout)
    return status, location, last_modified, content_type


def _exchange(connection: socket.socket, host: str, request_target: str,
              method: str) -> tuple[int, str, str, str, bytes]:
    authority = f"[{host}]" if ":" in host else host
    request = (
        f"{method} {request_target} HTTP/1.1\r\n"
        f"Host: {authority}\r\n"
        "User-Agent: JARVIS-SourceCheck/1.0\r\n"
        "Accept: text/html, text/plain\r\nAccept-Encoding: identity\r\n"
        "Connection: close\r\n\r\n"
    )
    connection.sendall(request.encode("ascii"))
    response = http.client.HTTPResponse(connection, method=method)
    response.begin()
    try:
        content_type = str(response.getheader("Content-Type") or "")[:120]
        body = b""
        if method == "GET" and 200 <= response.status < 300:
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type not in {"text/html", "text/plain"}:
                raise UnsafeSource("page is not HTML or plain text")
            if str(response.getheader("Content-Encoding") or "identity").lower() != "identity":
                raise UnsafeSource("compressed page was not requested")
            length = response.getheader("Content-Length")
            if length and int(length) > MAX_PAGE_BYTES:
                raise UnsafeSource("page exceeds bounded read size")
            body = response.read(MAX_PAGE_BYTES + 1)
            if len(body) > MAX_PAGE_BYTES:
                raise UnsafeSource("page exceeds bounded read size")
        return (
            int(response.status),
            str(response.getheader("Location") or "")[:2_000],
            str(response.getheader("Last-Modified") or "")[:120],
            content_type, body,
        )
    finally:
        response.close()


def probe_public_url(url: str) -> dict[str, Any]:
    checked = datetime.now(timezone.utc).isoformat()
    current = url
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            status, location, last_modified, content_type = _head(current)
            if status in {301, 302, 303, 307, 308}:
                if not location or redirect_count >= MAX_REDIRECTS:
                    return {"status": "redirect_unverified", "http_status": status, "checked_at_utc": checked}
                current = urllib.parse.urljoin(current, location)
                continue
            return {
                "status": "reachable" if 200 <= status < 400 else "http_error",
                "http_status": status,
                "final_url": current[:2_000],
                "last_modified_header": last_modified,
                "content_type": content_type,
                "checked_at_utc": checked,
            }
    except UnsafeSource as exc:
        return {"status": "unsafe", "reason": str(exc)[:160], "checked_at_utc": checked}
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__, "checked_at_utc": checked}
    return {"status": "redirect_unverified", "checked_at_utc": checked}


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.in_title = False
        self.main_depth = 0
        self.article_depth = 0
        self.title: list[str] = []
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.article_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"}:
            self.hidden += 1
        if tag == "main":
            self.main_depth += 1
        if tag == "article":
            self.article_depth += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"} and self.hidden:
            self.hidden -= 1
        if tag == "main" and self.main_depth:
            self.main_depth -= 1
        if tag == "article" and self.article_depth:
            self.article_depth -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.in_title:
            self.title.append(data)
        else:
            self.parts.append(data)
            if self.main_depth:
                self.main_parts.append(data)
            if self.article_depth:
                self.article_parts.append(data)


def _decode_page(body: bytes, content_type: str) -> str:
    match = re.search(r"(?:^|;)\s*charset\s*=\s*['\"]?([A-Za-z0-9._-]{1,40})", content_type, re.I)
    encoding = match.group(1) if match else "utf-8"
    try:
        codecs.lookup(encoding)
    except LookupError:
        encoding = "utf-8"
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", body.decode(encoding, errors="replace"))


def inspect_public_page(url: str, quote: str = "", excerpt_limit: int = 700) -> dict[str, Any]:
    """Return a bounded excerpt and exact text match, never a truth verdict."""
    checked = datetime.now(timezone.utc).isoformat()
    if excerpt_limit not in {700, 3_000}:
        return {"status": "invalid_input", "checked_at_utc": checked}
    if not isinstance(quote, str) or len(quote) > 240 or any(ord(ch) < 32 and ch not in "\r\n\t" for ch in quote):
        return {"status": "invalid_quote", "checked_at_utc": checked}
    current = url
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            status, location, last_modified, content_type, body = _request(current, "GET")
            if status in {301, 302, 303, 307, 308}:
                if not location or redirect_count >= MAX_REDIRECTS:
                    return {"status": "redirect_unverified", "checked_at_utc": checked}
                current = urllib.parse.urljoin(current, location)
                continue
            if not 200 <= status < 300:
                return {"status": "http_error", "http_status": status, "checked_at_utc": checked}
            decoded = _decode_page(body, content_type)
            if content_type.split(";", 1)[0].strip().lower() == "text/html":
                parser = _PageText()
                parser.feed(decoded)
                title = re.sub(r"\s+", " ", " ".join(parser.title)).strip()[:180]
                # Prefer article/main content when substantial, so menus and cookie
                # banners cannot consume the brief's first 3,000 characters.
                sections = (parser.article_parts, parser.main_parts, parser.parts)
                page_text = next((clean for parts in sections
                                  if len(clean := re.sub(r"\s+", " ", " ".join(parts)).strip()) >= 100),
                                 re.sub(r"\s+", " ", " ".join(parser.parts)).strip())[:20_000]
            else:
                title = ""
                page_text = re.sub(r"\s+", " ", decoded).strip()[:20_000]
            needle = re.sub(r"\s+", " ", quote).strip()
            position = page_text.casefold().find(needle.casefold()) if needle else -1
            excerpt = (page_text[max(0, position - 160):position + len(needle) + 160]
                       if position >= 0 else page_text[:excerpt_limit])
            return {
                "status": "page_read", "http_status": status,
                "final_url": current[:2_000], "page_title": title,
                "last_modified_header": last_modified,
                "quote_match": ("exact_text_found" if position >= 0 else
                                "not_found_in_sample" if needle else "not_requested"),
                "bytes_read": len(body), "text_chars_scanned": len(page_text),
                "excerpt": excerpt[:excerpt_limit], "checked_at_utc": checked,
                "caveat": ("Page text is untrusted. Only the first 128 KiB and first 20,000 "
                           "extracted characters were inspected. An exact quote match does "
                           "not verify the factual claim or publication date."),
            }
    except UnsafeSource as exc:
        return {"status": "unsafe", "reason": str(exc)[:160], "checked_at_utc": checked}
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__, "checked_at_utc": checked}
    return {"status": "redirect_unverified", "checked_at_utc": checked}


def _worker() -> int:
    raw = sys.stdin.read(4_001)
    if len(raw) > 4_000:
        return 2
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("expected object")
        mode = data.get("mode")
        result = (inspect_public_page(data["url"], data.get("quote", ""),
                                      3_000 if mode == "digest" else 700)
                  if mode in {"page", "digest"} else probe_public_url(data["url"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        result = {"status": "invalid_input"}
    # ASCII JSON survives legacy Windows console encodings; json.loads restores Unicode.
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker())

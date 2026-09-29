"""Single-query, bounded Brave web discovery for user-requested reports.

Search results are candidates only. Report evidence always comes from direct,
safe reads of original public pages, not from search snippets.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _document_id(url: str) -> str:
    """Recognize stable publication IDs across different hosting domains."""
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.casefold()
    host = (parsed.hostname or "").casefold()
    rfc = re.search(r"(?:^|[/_-])rfc[/_-]?0*(\d{3,5})(?:\D|$)", path)
    if rfc and host in {"www.rfc-editor.org", "rfc-editor.org", "datatracker.ietf.org", "tools.ietf.org"}:
        return f"rfc:{int(rfc.group(1))}"
    doi = re.search(r"(?:^|/)10\.\d{4,9}/[^?#]+", path)
    if doi:
        return "doi:" + doi.group().strip("/")
    return ""


def _rfc_title_signature(title: str) -> tuple[str, str] | None:
    # Strip site branding only after a complete-looking publication title.
    core = title.split(" | ", 1)[0].split(" - ", 1)[0].strip()
    match = re.fullmatch(r"RFC\s+0*(\d{3,5})\s*[:\-]\s*(.{12,120})", core, re.I)
    if not match:
        return None
    return str(int(match.group(1))), " ".join(re.findall(r"\w+", match.group(2).casefold()))


def same_underlying_source(url: str, page: dict[str, Any],
                           other_url: str, other_page: dict[str, Any]) -> bool:
    """Conservatively reject cross-host mirrors of the same publication."""
    first = str(page.get("final_url") or url)
    second = str(other_page.get("final_url") or other_url)
    if urllib.parse.urldefrag(first)[0].rstrip("/").casefold() == urllib.parse.urldefrag(second)[0].rstrip("/").casefold():
        return True
    first_id, second_id = _document_id(first), _document_id(second)
    if first_id and first_id == second_id:
        return True
    first_title = _rfc_title_signature(str(page.get("page_title") or ""))
    second_title = _rfc_title_signature(str(other_page.get("page_title") or ""))
    if first_title and first_title == second_title:
        return True
    first_tokens = re.findall(r"\w+", str(page.get("excerpt") or "").casefold())[:500]
    second_tokens = re.findall(r"\w+", str(other_page.get("excerpt") or "").casefold())[:500]
    if min(len(first_tokens), len(second_tokens)) < 60:
        return False
    first_shingles = set(zip(*(first_tokens[i:] for i in range(5))))
    second_shingles = set(zip(*(second_tokens[i:] for i in range(5))))
    return (bool(first_shingles and second_shingles) and
            len(first_shingles & second_shingles) / min(len(first_shingles), len(second_shingles)) >= 0.9)


def discover_public_urls(topic: str, api_key: str, limit: int = 6) -> list[str]:
    if not api_key:
        raise ValueError("BRAVE_SEARCH_API_KEY is not configured")
    query = " ".join(str(topic or "").split())
    if not 2 <= len(query) <= 200 or not 1 <= limit <= 8:
        raise ValueError("Invalid research topic or search limit")
    endpoint = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({
        "q": query, "count": limit, "safesearch": "strict",
    })
    request = urllib.request.Request(endpoint, headers={
        "X-Subscription-Token": api_key, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(128_001)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Source discovery failed (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Source discovery is unavailable") from exc
    if len(raw) > 128_000:
        raise RuntimeError("Source discovery response exceeded the size limit")
    try:
        results = json.loads(raw.decode("utf-8"))["web"]["results"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Source discovery returned malformed results") from exc
    if not isinstance(results, list):
        raise RuntimeError("Source discovery returned malformed results")
    urls: list[str] = []
    seen_hosts: set[str] = set()
    for row in results[:limit]:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        url = row["url"]
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if (len(url) > 2_000 or parsed.scheme not in {"http", "https"} or
            not parsed.hostname or parsed.username or parsed.password or
            any(ord(char) < 32 for char in url)):
            continue
        host = parsed.hostname.casefold()
        if host in seen_hosts:
            continue
        urls.append(url)
        seen_hosts.add(host)
    return urls

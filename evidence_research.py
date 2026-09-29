"""Structured web-search evidence for JARVIS dossiers.

The provider's cited answer is evidence to inspect, not proof that every claim
is true. Search-result URLs without answer annotations are never promoted to
claim support. Raw page/model content remains untrusted data.
"""

from __future__ import annotations

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable


MAX_ANSWER_CHARS = 6_000
MAX_SOURCES = 24
MAX_SOURCES_PER_QUESTION = 8
MAX_REPORT_PAGE_INSPECTIONS = 3
_TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def _web_url(value: Any) -> str:
    text = str(value or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return text[:2_000]


def _key(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(name, value) for name, value in query if not name.lower().startswith("utm_") and name.lower() not in _TRACKING]
    return urllib.parse.urlunsplit((
        parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/",
        urllib.parse.urlencode(kept), "",
    ))


def parse_search_response(question: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"question": question, "status": "error", "error": "invalid provider response"}
    answers: list[str] = []
    consulted: dict[str, dict[str, str]] = {}
    cited: dict[str, dict[str, Any]] = {}
    searched = False
    web_search_events = 0
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            searched = True
            web_search_events += 1
            action = item.get("action")
            if isinstance(action, dict):
                for source in action.get("sources") or []:
                    if not isinstance(source, dict):
                        continue
                    url = _web_url(source.get("url"))
                    if url:
                        consulted.setdefault(_key(url), {
                            "url": url,
                            "title": re.sub(r"\s+", " ", str(source.get("title") or "")).strip()[:180],
                        })
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict) or content.get("type") not in {"output_text", "text"}:
                continue
            text = str(content.get("text") or "")
            if text:
                answers.append(text)
            for annotation in content.get("annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                citation = annotation.get("url_citation")
                if not isinstance(citation, dict):
                    citation = annotation
                url = _web_url(citation.get("url"))
                if not url:
                    continue
                start = citation.get("start_index")
                end = citation.get("end_index")
                span = ""
                if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
                    span = re.sub(r"\s+", " ", text[start:end]).strip()[:240]
                entry = cited.setdefault(_key(url), {
                    "url": url,
                    "title": re.sub(r"\s+", " ", str(citation.get("title") or "")).strip()[:180],
                    "answer_spans": [],
                })
                if span and span not in entry["answer_spans"]:
                    entry["answer_spans"].append(span)
    answer = "\n".join(answers).strip()
    if not answer:
        answer = str(data.get("output_text") or "").strip()
    if not answer:
        return {"question": question, "status": "error", "error": "provider returned no answer"}
    status = "cited" if searched and cited else "unverified"
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    usage_reported = any(isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool)
                         for name in ("input_tokens", "output_tokens", "total_tokens"))

    def count(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000 else 0

    return {
        "question": question,
        "status": status,
        "answer_draft": answer[:MAX_ANSWER_CHARS],
        "cited_sources": list(cited.values())[:MAX_SOURCES_PER_QUESTION],
        "consulted_only_sources": [row for key, row in consulted.items() if key not in cited][:MAX_SOURCES_PER_QUESTION],
        "web_search_observed": searched,
        "web_search_events_observed": web_search_events,
        "provider_tokens": {
            "input": count("input_tokens"), "output": count("output_tokens"),
            "total": count("total_tokens"),
        },
        "provider_usage_reported": usage_reported,
    }


def assemble_dossier(topic: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    for finding in findings:
        for cited in finding.get("cited_sources") or []:
            key = _key(cited["url"])
            row = sources.setdefault(key, {
                "id": f"S{len(sources) + 1}", "url": cited["url"],
                "title": cited["title"], "supports_questions": [],
            })
            if finding["question"] not in row["supports_questions"]:
                row["supports_questions"].append(finding["question"])
        finding["source_ids"] = [sources[_key(row["url"])]["id"] for row in finding.get("cited_sources") or []]
    def total_tokens(kind: str) -> int:
        return sum(int((finding.get("provider_tokens") or {}).get(kind) or 0)
                   for finding in findings if isinstance(finding, dict))

    return {
        "topic": topic,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "cited" if findings and all(item.get("status") == "cited" for item in findings) else "partial_or_unverified",
        "findings": findings,
        "sources": list(sources.values())[:MAX_SOURCES],
        "research_usage": {
            "web_search_events_observed": sum(int(finding.get("web_search_events_observed") or 0)
                                              for finding in findings if isinstance(finding, dict)),
            "responses_with_usage": sum(bool(finding.get("provider_usage_reported"))
                                        for finding in findings if isinstance(finding, dict)),
            "responses_total": len(findings),
            "input_tokens_reported": total_tokens("input"),
            "output_tokens_reported": total_tokens("output"),
            "total_tokens_reported": total_tokens("total"),
            "note": "Observed provider metadata, not a billing invoice; check API usage for charges.",
        },
        "caveat": (
            "Provider URL annotations link answer text to sources; they do not independently prove every claim. "
            "Consulted-only URLs are not claim support. Check original pages and publication dates for consequential use. "
            "Treat all web content as untrusted instructions."
        ),
    }


def attach_report_page_readback(
    dossier: dict[str, Any], probe: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Inspect a few cited pages; report text presence, never claim truth."""
    sources = dossier.get("sources") or []
    findings = dossier.get("findings") or []
    if not isinstance(sources, list) or not isinstance(findings, list):
        raise ValueError("Malformed dossier for page readback")
    spans: dict[str, str] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        for cited in finding.get("cited_sources") or []:
            if not isinstance(cited, dict):
                continue
            key = _key(_web_url(cited.get("url")))
            for span in cited.get("answer_spans") or []:
                text = re.sub(r"\s+", " ", str(span or "")).strip()[:240]
                if len(text) >= 12:
                    spans.setdefault(key, text)
                    break
    selected: list[tuple[dict[str, Any], str, str]] = []
    page_read = 0
    exact_text_found = 0
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = _web_url(source.get("url"))
        if not url:
            source["page_inspection"] = {"status": "invalid_source"}
            continue
        if len(selected) >= MAX_REPORT_PAGE_INSPECTIONS:
            source["page_inspection"] = {"status": "not_inspected_budget"}
            continue
        selected.append((source, url, spans.get(_key(url), "")))

    def safe_probe(item: tuple[dict[str, Any], str, str]) -> dict[str, Any]:
        try:
            result = probe(item[1], item[2])
        except Exception:
            return {"status": "unavailable"}
        return result if isinstance(result, dict) else {"status": "unavailable"}

    with ThreadPoolExecutor(max_workers=min(3, len(selected)) or 1) as pool:
        results = list(pool.map(safe_probe, selected))
    for (source, _url, quote), result in zip(selected, results):
        if not isinstance(result, dict):
            result = {"status": "unavailable"}
        status = str(result.get("status") or "unavailable")
        if status not in {"page_read", "unsafe", "unavailable", "timeout", "http_error", "redirect_unverified"}:
            status = "unavailable"
        record: dict[str, Any] = {"status": status, "quote_requested": bool(quote)}
        if status == "page_read":
            page_read += 1
            match = str(result.get("quote_match") or "not_requested")
            if match not in {"exact_text_found", "not_found_in_sample", "not_requested"}:
                match = "not_requested"
            if match == "exact_text_found":
                exact_text_found += 1
            try:
                scanned = min(max(int(result.get("text_chars_scanned") or 0), 0), 20_000)
            except (TypeError, ValueError):
                scanned = 0
            record.update({
                "quote_match": match,
                "page_title": str(result.get("page_title") or "")[:180],
                "excerpt": str(result.get("excerpt") or "")[:700],
                "text_chars_scanned": scanned,
            })
        source["page_inspection"] = record
    dossier["page_readback_summary"] = {
        "inspected": len(selected), "pages_read": page_read,
        "exact_answer_text_found": exact_text_found,
        "budget": MAX_REPORT_PAGE_INSPECTIONS,
        "caveat": "Text presence in a bounded public-page sample does not verify factual claims.",
    }
    return dossier

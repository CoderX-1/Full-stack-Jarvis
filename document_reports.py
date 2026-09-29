"""Offline, source-linked PDF reports from a bounded research dossier.

The input is untrusted research content, not authoring instructions. This
module never overwrites a user file and never downloads remote assets.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape, quoteattr


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()[:limit]


def _bounded_count(value: Any, limit: int = 10_000_000) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limit else 0


def _validate(dossier: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(dossier, dict):
        raise ValueError("Research dossier must be an object")
    topic = _clean(dossier.get("topic"), 200)
    findings = dossier.get("findings")
    sources = dossier.get("sources")
    direct = dossier.get("research_mode") == "direct_source"
    professional = dossier.get("research_mode") == "professional_brief"
    finding_limit = 4 if professional else 3
    if not topic or not isinstance(findings, list) or not 1 <= len(findings) <= finding_limit:
        raise ValueError(f"Dossier needs a topic and 1 to {finding_limit} findings")
    if not isinstance(sources, list) or not sources or len(sources) > 24:
        raise ValueError("Dossier needs 1 to 24 cited sources")
    valid_ids = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Malformed dossier source")
        source_id = _clean(source.get("id"), 12)
        url = _clean(source.get("url"), 2_000)
        parsed = urllib.parse.urlsplit(url)
        if (not re.fullmatch(r"S\d{1,2}", source_id) or
            parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or any(char.isspace() for char in url)):
            raise ValueError("Malformed source ID or URL")
        valid_ids.add(source_id)
    for finding in findings:
        expected = "evidence_backed" if professional else "source_excerpt" if direct else "cited"
        if not isinstance(finding, dict) or finding.get("status") != expected:
            raise ValueError("PDF export requires exact source evidence" if professional else
                             "PDF export requires source excerpts" if direct else
                             "PDF export requires every finding to have provider-linked citations")
        if not _clean(finding.get("question"), 500) or not _clean(finding.get("answer_draft"), 6_000):
            raise ValueError("Finding is missing a question or answer")
        ids = finding.get("source_ids")
        if not isinstance(ids, list) or not ids or any(source_id not in valid_ids for source_id in ids):
            raise ValueError("Finding has missing or unknown source IDs")
        if professional:
            quote = _clean(finding.get("evidence_quote"), 220)
            source = next((source for source in sources if source["id"] == ids[0]), None)
            observed = re.sub(r"\s+", " ", str((source or {}).get("page_inspection", {}).get("excerpt") or "")).casefold()
            if not quote or re.sub(r"\s+", " ", quote).casefold() not in observed:
                raise ValueError("Evidence quote is absent from its fetched source")
    if professional and not _clean(dossier.get("summary"), 600):
        raise ValueError("Professional brief needs an executive summary")
    if professional:
        tensions = dossier.get("potential_tensions", [])
        if not isinstance(tensions, list) or len(tensions) > 2:
            raise ValueError("Malformed potential source tensions")
        selected = {(finding["source_ids"][0], _clean(finding["evidence_quote"], 220))
                    for finding in findings}
        seen_pairs = set()
        for tension in tensions:
            if not isinstance(tension, dict):
                raise ValueError("Malformed potential source tension")
            ids, quotes = tension.get("source_ids"), tension.get("quotes")
            if (not isinstance(ids, list) or not isinstance(quotes, list) or
                    len(ids) != 2 or len(quotes) != 2 or ids[0] == ids[1] or
                    any(not isinstance(value, str) for value in ids + quotes) or
                    any((source_id, quote) not in selected for source_id, quote in zip(ids, quotes))):
                raise ValueError("Potential tension must cite two selected source quotes")
            pair = frozenset(ids)
            if pair in seen_pairs:
                raise ValueError("Duplicate potential source tension")
            seen_pairs.add(pair)
    return topic, findings, sources


def _verify_rendered_pdf(
    path: Path, abort_check: Callable[[], bool] | None = None,
    expect_page_readback: bool = False, direct: bool = False,
    professional: bool = False,
) -> int:
    """Check extractable sections and render every page before publication."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("PDF verification engine missing: install pypdfium2") from exc
    try:
        document = pdfium.PdfDocument(str(path))
        try:
            page_count = len(document)
            if not 1 <= page_count <= 48:
                raise RuntimeError("PDF page count is outside the safe report limit")
            extracted: list[str] = []
            for index in range(page_count):
                if abort_check and abort_check():
                    raise RuntimeError("action deadline expired during PDF verification")
                page = document.get_page(index)
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                    if not text.strip():
                        raise RuntimeError(f"PDF page {index + 1} has no extractable text")
                    extracted.append(text)
                    bitmap = page.render(scale=0.7)
                    try:
                        if bitmap.width < 100 or bitmap.height < 100:
                            raise RuntimeError(f"PDF page {index + 1} did not render correctly")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            all_text = re.sub(r"\s+", " ", " ".join(extracted)).upper()
            heading = "EVIDENCE BRIEF" if professional else "PUBLIC SOURCE DIGEST" if direct else "RESEARCH DOSSIER"
            if heading not in all_text or "SOURCE MANIFEST" not in all_text:
                raise RuntimeError("PDF is missing its research or source section")
            if expect_page_readback and "SOURCE PAGE READBACK" not in all_text:
                raise RuntimeError("PDF is missing its source-page readback")
            return page_count
        finally:
            document.close()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("PDF render or text verification failed") from exc


def create_research_pdf(
    dossier: dict[str, Any], output_path: str,
    abort_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Write a new PDF, returning structural verification; never replace files."""
    topic, findings, sources = _validate(dossier)
    direct = dossier.get("research_mode") == "direct_source"
    professional = dossier.get("research_mode") == "professional_brief"
    output = Path(output_path).expanduser().resolve(strict=False)
    if output.suffix.lower() != ".pdf" or not output.parent.is_dir():
        raise ValueError("Choose a .pdf path inside an existing folder")
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    if abort_check and abort_check():
        raise RuntimeError("action deadline expired before PDF creation")
    isolated_dependencies = Path(__file__).resolve().parent / ".artifact-deps"
    if isolated_dependencies.is_dir() and str(isolated_dependencies) not in sys.path:
        sys.path.insert(0, str(isolated_dependencies))
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.graphics.shapes import Drawing, Rect, String
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    except ImportError as exc:
        raise RuntimeError("PDF engine missing: install reportlab from requirements-artifacts.txt") from exc

    font = "Helvetica"
    bold = "Helvetica-Bold"
    regular_path = Path("C:/Windows/Fonts/segoeui.ttf")
    bold_path = Path("C:/Windows/Fonts/segoeuib.ttf")
    if regular_path.is_file() and bold_path.is_file():
        if "JarvisSegoe" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("JarvisSegoe", str(regular_path)))
            pdfmetrics.registerFont(TTFont("JarvisSegoeBold", str(bold_path)))
            pdfmetrics.registerFontFamily("JarvisSegoe", normal="JarvisSegoe", bold="JarvisSegoeBold")
        font, bold = "JarvisSegoe", "JarvisSegoeBold"

    ink = colors.HexColor("#14243C")
    muted = colors.HexColor("#53647A")
    accent = colors.HexColor("#0F6B85")
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("JarvisTitle", parent=styles["Title"], fontName=bold,
                                 fontSize=22, leading=27, textColor=ink, spaceAfter=14,
                                 alignment=TA_LEFT)
    h_style = ParagraphStyle("JarvisH", parent=styles["Heading2"], fontName=bold,
                             fontSize=12.5, leading=16, textColor=ink, spaceBefore=15,
                             spaceAfter=7)
    section_style = ParagraphStyle("JarvisSection", parent=h_style, keepWithNext=1)
    body_style = ParagraphStyle("JarvisBody", parent=styles["BodyText"], fontName=font,
                                fontSize=9.8, leading=15, textColor=ink, spaceAfter=9)
    small_style = ParagraphStyle("JarvisSmall", parent=body_style, fontSize=8.5,
                                 leading=12, textColor=muted, spaceAfter=6)
    label_style = ParagraphStyle("JarvisLabel", parent=small_style, fontName=bold,
                                 textColor=accent, spaceAfter=10)
    quote_style = ParagraphStyle("JarvisQuote", parent=small_style, fontName=font,
                                 leftIndent=5 * mm, backColor=colors.HexColor("#EFF6F8"),
                                 borderPadding=7, spaceAfter=9)
    story = [
        Paragraph("JARVIS / PUBLIC SOURCE DIGEST" if direct else "JARVIS / RESEARCH DOSSIER", label_style),
        Paragraph(escape(topic), title_style),
        Paragraph(("Extractive preview of user-supplied public pages. Text may be incomplete, outdated, or "
                   "untrusted; no AI synthesis or factual verification was performed. Read the original pages "
                   "before consequential use." if direct else
                   "Source-linked draft. Provider citations are pointers for verification, not proof of every sentence. "
                   "Review original pages, publication dates, and factual claims before consequential use."), small_style),
        HRFlowable(width="100%", thickness=1, color=accent, spaceAfter=10),
    ]
    pages_read = sum(
        isinstance(source.get("page_inspection"), dict)
        and source["page_inspection"].get("status") == "page_read"
        for source in sources
    )
    chart_width = 170 * mm
    chart = Drawing(chart_width, 34 * mm)
    chart.add(String(0, 26 * mm, "PUBLIC SOURCE COVERAGE", fontName=bold,
                     fontSize=9, fillColor=ink))
    chart.add(String(chart_width, 26 * mm,
                     f"{pages_read} of {len(sources)} source pages read" if direct else
                     f"{pages_read} of {len(sources)} cited pages read", textAnchor="end",
                     fontName=font, fontSize=8.5, fillColor=muted))
    chart.add(Rect(0, 16 * mm, chart_width, 5 * mm, fillColor=colors.HexColor("#E5EBF0"),
                   strokeColor=None))
    if pages_read:
        chart.add(Rect(0, 16 * mm, chart_width * pages_read / len(sources), 5 * mm,
                       fillColor=accent, strokeColor=None))
    chart.add(String(0, 7 * mm, "Page readback is an observation, not claim verification.",
                     fontName=font, fontSize=8, fillColor=muted))
    story.append(chart)
    usage = dossier.get("research_usage") if not direct else None
    if direct:
        story.append(Paragraph(
            "Research mode: direct page extraction from supplied URLs. No OpenAI web-search call or "
            "AI synthesis was made by this report action. Network access and local PDF rendering were used.",
            small_style,
        ))
    if isinstance(usage, dict):
        events = _bounded_count(usage.get("web_search_events_observed"))
        tokens = _bounded_count(usage.get("total_tokens_reported"))
        responses_with_usage = _bounded_count(usage.get("responses_with_usage"), len(findings))
        usage_label = (f"{tokens:,} tokens reported across {responses_with_usage}/{len(findings)} responses"
                       if responses_with_usage else "token usage unavailable from provider")
        story.append(Paragraph(
            f"Provider-reported usage: {events} web-search events observed, "
            f"{usage_label}. This is not a billing invoice; "
            "check your API usage dashboard for actual charges.", small_style,
        ))
    for number, finding in enumerate(findings, 1):
        story.append(Paragraph(f"{number:02d} / {escape(_clean(finding['question'], 500))}", h_style))
        answer = _clean(finding["answer_draft"], 6_000)
        for paragraph in re.split(r"\n\s*\n", answer):
            if paragraph.strip():
                story.append(Paragraph(escape(paragraph).replace("\n", "<br/>"), body_style))
        refs = ", ".join(_clean(value, 12) for value in finding["source_ids"])
        story.append(Paragraph(
            f"Original page: {escape(refs)}" if direct else f"Provider-linked sources: {escape(refs)}",
            label_style,
        ))
    story.append(Paragraph("SOURCE MANIFEST", section_style))
    for source in sources:
        source_id = escape(_clean(source["id"], 12))
        title = escape(_clean(source.get("title") or source["url"], 180))
        url = _clean(source["url"], 2_000)
        story.append(Paragraph(f"{source_id}  {title}<br/><link href={quoteattr(url)} color=\"#0F6B85\">"
                               f"{escape(url)}</link>", small_style))
        check = source.get("link_check") or {}
        if isinstance(check, dict):
            state = _clean(check.get("status"), 32)
            http_status = check.get("http_status")
            observed = f" (HTTP {http_status})" if isinstance(http_status, int) else ""
            if state:
                story.append(Paragraph(f"Link check: {escape(state)}{observed}. "
                                       "HTTP availability does not verify the claim.", small_style))
    readbacks = [(source, source.get("page_inspection")) for source in sources
                 if isinstance(source.get("page_inspection"), dict)] if not direct else []
    if readbacks:
        story.append(Paragraph("SOURCE PAGE READBACK", section_style))
        story.append(Paragraph(
            "At most three cited public pages were sampled. Exact answer-text presence "
            "does not establish factual truth, context, or publication date. "
            "Unavailable and uninspected pages remain unverified.", small_style,
        ))
        for source, inspection in readbacks:
            source_id = escape(_clean(source.get("id"), 12))
            state = _clean(inspection.get("status"), 40)
            if state == "not_inspected_budget":
                story.append(Paragraph(f"{source_id}: Not inspected (three-page budget).", small_style))
                continue
            state_label = {
                "page_read": "Page text inspected", "unsafe": "Unsafe source refused",
                "unavailable": "Page unavailable", "timeout": "Page read timed out",
                "http_error": "HTTP error", "redirect_unverified": "Redirect not verified",
            }.get(state, "Page not inspected")
            story.append(Paragraph(f"{source_id}: {state_label}", label_style))
            if state == "page_read":
                match = {
                    "exact_text_found": "Exact cited answer text found in sample",
                    "not_found_in_sample": "Cited answer text not found in sample",
                    "not_requested": "No exact-text check available",
                }.get(_clean(inspection.get("quote_match"), 40), "No exact-text check available")
                story.append(Paragraph(f"{match}. "
                                       "This is not a factual verification.", small_style))
                excerpt = _clean(inspection.get("excerpt"), 700)
                if excerpt:
                    story.append(Paragraph(f"Observed page excerpt: {escape(excerpt)}", small_style))
    if professional:
        brief_title = ParagraphStyle("BriefTitle", parent=title_style,
                                     fontSize=20, leading=24, spaceAfter=8)
        brief_section = ParagraphStyle("BriefSection", parent=section_style,
                                       spaceBefore=9, spaceAfter=4)
        brief_head = ParagraphStyle("BriefHeading", parent=h_style,
                                    fontSize=11.5, leading=14, spaceBefore=9,
                                    spaceAfter=4)
        brief_body = ParagraphStyle("BriefBody", parent=body_style,
                                    fontSize=9.4, leading=13.2, spaceAfter=5)
        brief_small = ParagraphStyle("BriefSmall", parent=small_style,
                                     fontSize=8.1, leading=10.8, spaceAfter=4)
        brief_label = ParagraphStyle("BriefLabel", parent=label_style,
                                     fontSize=8.2, leading=11, spaceAfter=5)
        brief_quote = ParagraphStyle("BriefQuote", parent=quote_style,
                                     fontSize=8.1, leading=10.8,
                                     borderPadding=5, spaceAfter=4)
        story = [
            Paragraph("JARVIS / EVIDENCE BRIEF", brief_label),
            Paragraph(escape(topic), brief_title),
            Paragraph("A concise synthesis of directly inspected public pages", brief_small),
            HRFlowable(width="100%", thickness=1, color=accent, spaceAfter=7),
            Paragraph("EXECUTIVE SUMMARY", brief_section),
            Paragraph(escape(_clean(dossier["summary"], 600)), brief_body),
            Paragraph("KEY FINDINGS", brief_section),
        ]
        source_by_id = {source["id"]: source for source in sources}
        for number, finding in enumerate(findings, 1):
            source_id = finding["source_ids"][0]
            source = source_by_id[source_id]
            url = _clean(source["url"], 2_000)
            story.extend([
                Paragraph(f"{number:02d} / {escape(_clean(finding['question'], 110))}", brief_head),
                Paragraph(escape(_clean(finding["answer_draft"], 450)), brief_body),
                Paragraph(f"Evidence: \"{escape(_clean(finding['evidence_quote'], 220))}\"", brief_quote),
                Paragraph(f"Source <link href={quoteattr(url)} color=\"#0F6B85\">"
                          f"{escape(source_id)} - {escape(_clean(source.get('title') or url, 100))}</link>",
                          brief_label),
            ])
        tensions = dossier.get("potential_tensions", [])
        if tensions:
            story.append(Paragraph("POTENTIAL SOURCE TENSIONS", brief_section))
            story.append(Paragraph(
                "AI-flagged for review only. The quoted sentences are from inspected extracts; "
                "whether they truly disagree depends on scope, date, and original context.", brief_small))
            for tension in tensions:
                for source_id, quote in zip(tension["source_ids"], tension["quotes"]):
                    url = _clean(source_by_id[source_id]["url"], 2_000)
                    story.append(Paragraph(
                        f"<link href={quoteattr(url)} color=\"#0F6B85\">{escape(source_id)}</link>: "
                        f"\"{escape(_clean(quote, 220))}\"", brief_quote))
        story.extend([
            Paragraph("METHOD &amp; LIMITATIONS", brief_section),
            Paragraph("Gemini synthesized only the bounded public-page text fetched for this request. "
                      "Quoted evidence was checked against those extracts; interpretation, publication "
                      "date, completeness, and factual truth were not independently verified. "
                      "Each source read was limited to 128 KiB and the first 3,000 extracted characters.",
                      brief_small),
            Paragraph("SOURCE MANIFEST", brief_section),
        ])
        for source in sources:
            url = _clean(source["url"], 2_000)
            story.append(Paragraph(
                f"{escape(_clean(source['id'], 12))}  "
                f"{escape(_clean(source.get('title') or url, 180))}<br/>"
                f"<link href={quoteattr(url)} color=\"#0F6B85\">{escape(url)}</link>",
                brief_small,
            ))
        report_usage = dossier.get("report_usage")
        if isinstance(report_usage, dict):
            model = escape(_clean(report_usage.get("model"), 90))
            tokens = report_usage.get("tokens_reported")
            token_label = f"; {tokens:,} tokens reported" if isinstance(tokens, int) and tokens >= 0 else ""
            story.append(Paragraph(f"Synthesis model: {model or 'Gemini'}{token_label}. "
                                   "Usage is not a billing invoice.", brief_small))
            attempts = report_usage.get("synthesis_requests")
            if isinstance(attempts, int) and attempts > 1:
                story.append(Paragraph(
                    f"Synthesis required {attempts} API requests after a temporary provider failure. "
                    "Check provider usage for actual charges.", brief_small,
                ))
            if report_usage.get("discovery_provider") == "Brave Search API":
                story.append(Paragraph(
                    "Discovery: one Brave Search API query supplied candidate links. "
                    "Only independently fetched original pages are cited above; search snippets "
                    "were not used as report evidence.", brief_small,
                ))
    generated = escape(_clean(dossier.get("generated_at_utc"), 80))
    story.append(Paragraph(f"Generated (UTC): {generated or 'not supplied'}", small_style))

    def decorate(canvas, doc):
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(accent)
        canvas.line(18 * mm, height - 15 * mm, width - 18 * mm, height - 15 * mm)
        canvas.setFont(font, 8)
        canvas.setFillColor(muted)
        canvas.drawString(18 * mm, 13 * mm,
                          "JARVIS  /  EVIDENCE BRIEF" if professional else "JARVIS  /  SOURCE-LINKED DRAFT")
        canvas.drawRightString(width - 18 * mm, 13 * mm, f"Page {doc.page}")
        canvas.restoreState()

    handle = tempfile.NamedTemporaryFile(prefix="jarvis-report-", suffix=".pdf", dir=output.parent, delete=False)
    temporary = Path(handle.name)
    handle.close()
    try:
        doc = SimpleDocTemplate(str(temporary), pagesize=A4, leftMargin=19 * mm,
                                rightMargin=19 * mm, topMargin=24 * mm, bottomMargin=22 * mm,
                                title=topic, author="JARVIS")
        doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
        with temporary.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise RuntimeError("PDF engine produced invalid output")
        page_count = _verify_rendered_pdf(temporary, abort_check, bool(readbacks) and not professional,
                                          direct, professional)
        if abort_check and abort_check():
            raise RuntimeError("action deadline expired before PDF publication")
        os.link(temporary, output)  # Exclusive creation on the same volume; cannot overwrite.
        report_usage = dossier.get("report_usage") if isinstance(dossier.get("report_usage"), dict) else {}
        return {"path": str(output), "bytes": output.stat().st_size,
                "pages": page_count, "findings": len(findings), "cited_sources": len(sources),
                "source_pages_inspected": pages_read if direct or professional else sum(item.get("status") not in {
                    "not_inspected_budget", "invalid_source",
                } for _source, item in readbacks),
                "source_pages_read": pages_read if direct or professional else sum(
                    item.get("status") == "page_read" for _source, item in readbacks),
                "visuals": [] if professional else ["vector source-coverage chart"],
                "research_mode": "professional_brief" if professional else "direct_source" if direct else "provider_search",
                "web_search_events_observed": _bounded_count(
                    (dossier.get("research_usage") or {}).get("web_search_events_observed")
                    if isinstance(dossier.get("research_usage"), dict) else 0,
                ),
                "brave_search_requests": _bounded_count(report_usage.get("discovery_requests"), 8)
                if professional and report_usage.get("discovery_provider") == "Brave Search API" else 0,
                "gemini_synthesis_requests": _bounded_count(report_usage.get("synthesis_requests"), 4)
                if professional else 0,
                "gemini_tokens_reported": _bounded_count(report_usage.get("tokens_reported"))
                if professional else 0,
                "verification": "PDF content and pages rendered; visual review still required"}
    finally:
        temporary.unlink(missing_ok=True)

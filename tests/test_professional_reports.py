import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from document_reports import _verify_rendered_pdf, create_research_pdf
from harness_context import route_lanes, select_tools
from jarvis_mark2 import MARK2_TOOLS, Mark2Runtime
from professional_research import _editorial_cut, _evidence_candidates, _supported_prose, _validate_brief, synthesize_public_brief
from source_discovery import discover_public_urls, same_underlying_source


URLS = ["https://example.org/first", "https://other.example.net/second"]
PAGES = [
    {"status": "page_read", "final_url": URLS[0], "page_title": "First source",
     "excerpt": "The independent first source states that the project reached its first milestone. " * 5},
    {"status": "page_read", "final_url": URLS[1], "page_title": "Second source",
     "excerpt": "The independent second source reports a later update and a remaining challenge. " * 5},
]
QUOTE1 = "the project reached its first milestone"
QUOTE2 = "a later update and a remaining challenge"


def brief():
    return {"summary": "Two public sources describe the project's milestone and a later unresolved challenge.",
            "findings": [
                {"question": "Initial milestone", "answer_draft": "The first source describes a milestone.",
                 "source_ids": ["S1"], "status": "evidence_backed", "evidence_quote": QUOTE1},
                {"question": "Remaining challenge", "answer_draft": "The second source notes an unresolved challenge.",
                 "source_ids": ["S2"], "status": "evidence_backed", "evidence_quote": QUOTE2},
            ], "usage": {"model": "gemini-3.7-flash", "tokens_reported": 320}}


class ProfessionalResearchTests(unittest.TestCase):
    def test_potential_tension_requires_selected_distinct_source_evidence(self):
        evidence = _evidence_candidates(PAGES)
        first = next(item for item in evidence if item["source_id"] == "S1")
        second = next(item for item in evidence if item["source_id"] == "S2")
        base = {"summary": brief()["summary"], "findings": [
            {"headline": "Initial milestone", "analysis": "The first source describes a milestone.",
             "evidence_id": first["evidence_id"]},
            {"headline": "Remaining challenge", "analysis": "The second source notes an unresolved challenge.",
             "evidence_id": second["evidence_id"]},
        ]}
        base["potential_tensions"] = [[first["evidence_id"], second["evidence_id"]],
                                       [first["evidence_id"], "E999"]]
        result = _validate_brief(base, PAGES, evidence)
        self.assertEqual(result["potential_tensions"], [{
            "source_ids": ["S1", "S2"], "quotes": [first["quote"], second["quote"]]}])
        base["potential_tensions"] = [[first["evidence_id"], first["evidence_id"]]]
        self.assertEqual(_validate_brief(base, PAGES, evidence)["potential_tensions"], [])
        base["findings"][0]["evidence_id"] = {"unexpected": "object"}
        with self.assertRaisesRegex(ValueError, "lacks bounded source evidence"):
            _validate_brief(base, PAGES, evidence)

    def test_editorial_cut_never_splits_a_word_and_evidence_starts_at_sentence(self):
        self.assertEqual(_editorial_cut("A complete sentence. " + "verylongword " * 20, 40),
                         "A complete sentence.")
        self.assertTrue(_editorial_cut("Intro " + "word " * 30, 47).endswith("..."))
        items = _evidence_candidates([{"excerpt":
            "First complete sentence discusses a clear fact. "
            "Second complete sentence explains another distinct point."}])
        self.assertEqual(items[0]["quote"], "First complete sentence discusses a clear fact.")
        self.assertEqual(items[1]["quote"], "Second complete sentence explains another distinct point.")

    def test_evidence_candidates_never_publish_incomplete_long_sentence(self):
        complete = ("To reduce conflict and confusion, reserved domain names can be used "
                    "in private testing and documentation without colliding with real sites.")
        too_long = "This unfinished claim contains " + "additional context " * 20 + "at its very end."
        items = _evidence_candidates([{"excerpt": complete + " " + too_long}])
        self.assertEqual([item["quote"] for item in items], [complete])
        self.assertTrue(all(item["quote"].endswith(".") for item in items))

    def test_prose_guard_removes_unsupported_numbers_and_negative_claims(self):
        evidence = "The trial reached 42 participants and documented a positive result."
        drafted = ("The trial involved 42 participants. "
                   "It reached 900 participants. "
                   "The result was not positive. "
                   "This proves the result is guaranteed.")
        self.assertEqual(_supported_prose(drafted, evidence, 280),
                         "The trial involved 42 participants.")
        self.assertEqual(_supported_prose(
            "The domains are maintained for documentation without prior coordination.",
            "The domains are maintained for documentation.", 280,
        ), "The domains are maintained for documentation.")

    def test_gemini_request_only_receives_fetched_text_and_rejects_fake_quote(self):
        candidates = _evidence_candidates(PAGES)
        first = next(item for item in candidates if item["source_id"] == "S1")
        second = next(item for item in candidates if item["source_id"] == "S2")
        payload = {"candidates": [{"content": {"parts": [{"text": json.dumps({
            "summary": brief()["summary"], "findings": [
                {"headline": "Initial milestone", "analysis": "The first source describes a milestone.",
                 "evidence_id": first["evidence_id"]},
                {"headline": "Remaining challenge", "analysis": "The second source notes an unresolved challenge.",
                 "evidence_id": second["evidence_id"]},
            ],
        })}]}}], "usageMetadata": {"totalTokenCount": 320}}
        with patch("professional_research.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as api:
            result = synthesize_public_brief("Project update", PAGES, "fake-key", "gemini-3.7-flash")
        self.assertIn(QUOTE1, result["findings"][0]["evidence_quote"])
        request = api.call_args.args[0]
        body = json.loads(request.data)
        self.assertNotIn("tools", body)
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})
        self.assertIn("evidence", json.loads(body["contents"][0]["parts"][0]["text"]))
        self.assertIn("untrusted data", body["systemInstruction"]["parts"][0]["text"])
        self.assertNotIn("project reached", body["systemInstruction"]["parts"][0]["text"])
        self.assertIn("project reached", body["contents"][0]["parts"][0]["text"])
        self.assertNotIn("fake-key", request.data.decode())
        payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
            "summary": brief()["summary"], "findings": [
                {"headline": "False result", "analysis": "The first source describes a milestone.",
                 "evidence_id": "E999"},
                {"headline": "Remaining challenge", "analysis": "The second source notes an unresolved challenge.",
                 "evidence_id": second["evidence_id"]},
            ]})
        with patch("professional_research.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaisesRegex(ValueError, "lacks bounded source evidence"):
                synthesize_public_brief("Project update", PAGES, "fake-key", "gemini-3.7-flash")

    def test_gemini_cannot_ignore_second_source(self):
        candidates = _evidence_candidates(PAGES)
        first = next(item for item in candidates if item["source_id"] == "S1")
        payload = {"candidates": [{"content": {"parts": [{"text": json.dumps({
            "summary": brief()["summary"], "findings": [
                {"headline": "Initial milestone", "analysis": "The first source describes a milestone.",
                 "evidence_id": first["evidence_id"]},
                {"headline": "Duplicate source", "analysis": "The first source describes another milestone.",
                 "evidence_id": first["evidence_id"]},
            ],
        })}]}}]}
        with patch("professional_research.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaisesRegex(ValueError, "every source exactly once"):
                synthesize_public_brief("Project update", PAGES, "fake-key", "gemini-2.5-flash")

    def test_brave_discovers_distinct_public_candidates_without_snippets(self):
        data = {"web": {"results": [
            {"url": URLS[0], "description": "Do not use me as evidence"},
            {"url": "https://example.org/duplicate"}, {"url": "file:///private"},
            {"url": URLS[1]},
        ]}}
        with patch("source_discovery.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())) as api:
            result = discover_public_urls("Project update", "fake-brave-key")
        self.assertEqual(result, URLS)
        self.assertNotIn("fake-brave-key", api.call_args.args[0].full_url)

    def test_underlying_publication_dedup_across_hosts(self):
        iana = {"final_url": "https://www.iana.org/help/example-domains",
                "excerpt": "RFC 2606 explains the example domains. " * 20}
        editor = {"final_url": "https://www.rfc-editor.org/info/rfc2606/",
                  "excerpt": "Reserved top level names are documented here. " * 20}
        ietf = {"final_url": "https://datatracker.ietf.org/doc/html/rfc2606",
                "excerpt": "The publication is shown in an alternate HTML format. " * 20}
        self.assertFalse(same_underlying_source(iana["final_url"], iana,
                                                editor["final_url"], editor))
        self.assertTrue(same_underlying_source(editor["final_url"], editor,
                                               ietf["final_url"], ietf))
        mirror_rfc = dict(ietf,
                          final_url="https://www.iankduncan.com/projects/rfc-browser/2606",
                          page_title="RFC 2606: Reserved Top Level DNS Names - Ian Duncan - Ian Duncan")
        editor_with_title = dict(editor,
                                 page_title="RFC 2606: Reserved Top Level DNS Names | RFC Editor")
        self.assertTrue(same_underlying_source(editor["final_url"], editor_with_title,
                                               mirror_rfc["final_url"], mirror_rfc))
        commentary = dict(ietf, final_url="https://research.example.net/rfc2606-analysis",
                          page_title="RFC 2606: Reserved Top Level DNS Names Analysis",
                          excerpt="An independent discussion analyzes a similar technical topic. " * 12)
        self.assertFalse(same_underlying_source(editor["final_url"], editor_with_title,
                                                commentary["final_url"], commentary))
        mirror = dict(iana, final_url="https://mirror.example.net/copy",
                      excerpt="Intro. " + iana["excerpt"])
        self.assertTrue(same_underlying_source(iana["final_url"], iana,
                                               mirror["final_url"], mirror))

    def test_topic_discovery_requires_key_before_network(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": ""}):
            runtime = Mark2Runtime(Path(folder))
            with patch("jarvis_mark2.discover_public_urls") as discovery:
                result = runtime.professional_topic_report("Project update")
            self.assertIn("BRAVE_SEARCH_API_KEY", result)
            discovery.assert_not_called()

    def test_topic_discovery_reads_originals_and_reuses_pages(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "BRAVE_SEARCH_API_KEY": "fake", "GEMINI_API_KEY": "fake",
        }):
            runtime = Mark2Runtime(Path(folder))
            target = Path(folder) / "brief.pdf"
            with patch("jarvis_mark2.discover_public_urls", return_value=URLS), \
                    patch.object(runtime, "_run_source_probe", side_effect=PAGES) as probe, \
                    patch.object(runtime, "professional_source_report", return_value="report-ready") as report:
                result = runtime.professional_topic_report("Project update", str(target))
            self.assertEqual(result, "report-ready")
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(report.call_args.kwargs["prefetched_pages"], PAGES)
            self.assertEqual(report.call_args.kwargs["discovery_provider"], "Brave Search API")

    def test_topic_discovery_skips_same_rfc_on_two_hosts(self):
        urls = ["https://www.iana.org/help/example-domains",
                "https://www.rfc-editor.org/info/rfc2606/",
                "https://datatracker.ietf.org/doc/html/rfc2606"]
        statements = ["IANA maintains example domains for documentation.",
                      "RFC 2606 reserves top-level names for testing.",
                      "This alternate host displays the same RFC document."]
        pages = [{"status": "page_read", "final_url": url,
                  "excerpt": statement * 10}
                 for url, statement in zip(urls, statements)]
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "BRAVE_SEARCH_API_KEY": "fake", "GEMINI_API_KEY": "fake",
        }):
            runtime = Mark2Runtime(Path(folder))
            with patch("jarvis_mark2.discover_public_urls", return_value=urls), \
                    patch.object(runtime, "_run_source_probe", side_effect=pages) as probe, \
                    patch.object(runtime, "professional_source_report", return_value="report-ready") as report:
                result = runtime.professional_topic_report("Reserved names")
        self.assertEqual(result, "report-ready")
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(report.call_args.args[1], urls[:2])

    def test_gemini_503_uses_one_bounded_fallback_and_records_attempts(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "GEMINI_API_KEY": "fake", "GEMINI_MODEL": "gemini-3.7-flash",
            "GEMINI_REPORT_MODEL": "gemini-3.7-flash",
            "GEMINI_REPORT_FALLBACK_MODEL": "gemini-2.5-flash",
        }):
            runtime = Mark2Runtime(Path(folder))
            target = Path(folder) / "brief.pdf"
            with patch.object(runtime, "_run_source_probe", side_effect=PAGES), \
                    patch("jarvis_mark2.synthesize_public_brief",
                          side_effect=[RuntimeError("Gemini report request failed (HTTP 503)"), brief()]) as api, \
                    patch("jarvis_mark2.create_research_pdf", return_value={"verification": "PDF content and pages rendered"}) as pdf:
                result = runtime.professional_source_report("Project update", URLS, str(target))
            self.assertIn("pages rendered", result)
            self.assertEqual(api.call_count, 2)
            self.assertEqual(api.call_args.args[3], "gemini-2.5-flash")
            self.assertEqual(pdf.call_args.args[0]["report_usage"]["synthesis_requests"], 2)

    def test_report_default_uses_fast_model_without_changing_chat_model(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "GEMINI_API_KEY": "fake", "GEMINI_MODEL": "gemini-3.7-flash",
            "GEMINI_REPORT_MODEL": "",
        }):
            runtime = Mark2Runtime(Path(folder))
            with patch.object(runtime, "_run_source_probe", side_effect=PAGES), \
                    patch("jarvis_mark2.synthesize_public_brief", return_value=brief()) as api, \
                    patch("jarvis_mark2.create_research_pdf", return_value={"verification": "PDF content and pages rendered"}):
                runtime.professional_source_report("Project update", URLS, str(Path(folder) / "brief.pdf"))
            self.assertEqual(api.call_args.args[3], "gemini-3.5-flash-lite")

    def test_report_default_falls_back_to_legacy_flash_once_on_503(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "GEMINI_API_KEY": "fake", "GEMINI_MODEL": "gemini-3.7-flash",
            "GEMINI_REPORT_MODEL": "", "GEMINI_REPORT_FALLBACK_MODEL": "",
        }):
            runtime = Mark2Runtime(Path(folder))
            with patch.object(runtime, "_run_source_probe", side_effect=PAGES), \
                    patch("jarvis_mark2.synthesize_public_brief",
                          side_effect=[RuntimeError("Gemini report request failed (HTTP 503)"), brief()]) as api, \
                    patch("jarvis_mark2.create_research_pdf", return_value={"verification": "PDF content and pages rendered"}):
                runtime.professional_source_report("Project update", URLS, str(Path(folder) / "brief.pdf"))
            self.assertEqual([call.args[3] for call in api.call_args_list],
                             ["gemini-3.5-flash-lite", "gemini-2.5-flash"])

    def test_gemini_nontransient_error_does_not_retry_or_publish(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "GEMINI_API_KEY": "fake", "GEMINI_MODEL": "gemini-3.7-flash",
        }):
            runtime = Mark2Runtime(Path(folder))
            target = Path(folder) / "brief.pdf"
            with patch.object(runtime, "_run_source_probe", side_effect=PAGES), \
                    patch("jarvis_mark2.synthesize_public_brief", side_effect=RuntimeError("Gemini report request failed (HTTP 429)")) as api:
                result = runtime.professional_source_report("Project update", URLS, str(target))
            self.assertIn("HTTP 429", result)
            self.assertEqual(api.call_count, 1)
            self.assertFalse(target.exists())

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab") or
        (Path(__file__).resolve().parents[1] / ".artifact-deps" / "reportlab").is_dir(),
        "ReportLab not installed",
    )
    def test_professional_report_is_concise_rendered_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "GEMINI_API_KEY": "fake", "GEMINI_MODEL": "gemini-3.7-flash",
        }):
            runtime = Mark2Runtime(Path(folder))
            target = Path(folder) / "professional.pdf"
            with patch.object(runtime, "_run_source_probe", side_effect=PAGES), \
                    patch("jarvis_mark2.synthesize_public_brief", return_value=brief()) as gemini:
                raw = runtime.execute("professional_source_report", {
                    "topic": "Project update", "urls": URLS, "output_path": str(target),
                })
            result = json.loads(raw)
            self.assertEqual(result["research_mode"], "professional_brief")
            self.assertEqual(result["source_pages_read"], 2)
            self.assertEqual(gemini.call_count, 1)
            self.assertEqual(runtime.verification.evaluate("professional_source_report", {}, raw).status, "verified")
            self.assertGreaterEqual(_verify_rendered_pdf(target, professional=True), 1)
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(target))
            try:
                text = " ".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
            finally:
                pdf.close()
            self.assertIn("EXECUTIVE SUMMARY", text)
            self.assertIn("KEY FINDINGS", text)
            self.assertIn("METHOD & LIMITATIONS", text)
            self.assertNotIn("The independent first source states that the project reached its first milestone. " * 3, text)
            with patch("jarvis_mark2.synthesize_public_brief") as second:
                again = runtime.professional_source_report("Project update", URLS, str(target))
            self.assertIn("already exists", again)
            second.assert_not_called()

    def test_typical_three_finding_brief_keeps_manifest_with_findings(self):
        with tempfile.TemporaryDirectory() as folder:
            data = brief()
            data["summary"] = (
                "The public documents explain why reserved identifiers avoid conflict in private testing. "
                "They also describe examples used in documentation and a small set of domains "
                "administered under established technical policy."
            )
            data["findings"].append({"question": "Documentation and examples",
                "answer_draft": ("Reserved names give examples a predictable meaning while keeping "
                                 "them out of ordinary registration. This bounded observation should "
                                 "be checked against the original documents before reuse."),
                "source_ids": ["S1"], "status": "evidence_backed", "evidence_quote": QUOTE1})
            for finding in data["findings"]:
                finding["answer_draft"] += (" The source extract is only a partial view of the "
                                            "full document, so broader implications remain unverified.")
            sources = [{"id": f"S{i}", "url": URLS[i-1], "title": PAGES[i-1]["page_title"],
                        "page_inspection": PAGES[i-1]} for i in (1, 2)]
            dossier = {"topic": "Reserved example domains and their purpose",
                       "research_mode": "professional_brief", "summary": data["summary"],
                       "findings": data["findings"], "sources": sources,
                       "generated_at_utc": "2026-09-29T00:00:00Z",
                       "report_usage": {"model": "gemini-2.5-flash", "tokens_reported": 2300,
                                        "synthesis_requests": 1,
                                        "discovery_provider": "Brave Search API",
                                        "discovery_requests": 1}}
            result = create_research_pdf(dossier, str(Path(folder) / "brief.pdf"))
            self.assertEqual(result["pages"], 1)
            self.assertEqual(result["brave_search_requests"], 1)
            self.assertEqual(result["gemini_synthesis_requests"], 1)
            self.assertEqual(result["gemini_tokens_reported"], 2300)

    def test_potential_tension_renders_exact_linked_quotes_and_rejects_forgery(self):
        with tempfile.TemporaryDirectory() as folder:
            data = brief()
            data["potential_tensions"] = [{"source_ids": ["S1", "S2"],
                                           "quotes": [QUOTE1, QUOTE2]}]
            sources = [{"id": f"S{i}", "url": URLS[i-1],
                        "title": PAGES[i-1]["page_title"], "page_inspection": PAGES[i-1]}
                       for i in (1, 2)]
            dossier = {"topic": "Project update", "research_mode": "professional_brief",
                       "summary": data["summary"], "findings": data["findings"],
                       "potential_tensions": data["potential_tensions"], "sources": sources}
            target = Path(folder) / "tensions.pdf"
            result = create_research_pdf(dossier, str(target))
            self.assertEqual(result["research_mode"], "professional_brief")
            import pypdfium2 as pdfium
            document = pdfium.PdfDocument(str(target))
            try:
                rendered = " ".join(document[i].get_textpage().get_text_range()
                                    for i in range(len(document)))
            finally:
                document.close()
            self.assertIn("POTENTIAL SOURCE TENSIONS", rendered)
            self.assertIn("AI-flagged for review only", rendered)
            self.assertIn(QUOTE1, rendered)
            self.assertIn(QUOTE2, rendered)
            dossier["potential_tensions"][0]["quotes"][1] = "Invented conflicting claim."
            with self.assertRaisesRegex(ValueError, "Potential tension must cite"):
                create_research_pdf(dossier, str(Path(folder) / "forged.pdf"))

    def test_tools_are_routed_and_audit_redacts_topic_and_urls(self):
        names = {tool["function"]["name"] for tool in select_tools(MARK2_TOOLS, route_lanes("professional research PDF"))}
        self.assertIn("professional_source_report", names)
        self.assertIn("professional_topic_report", names)
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            runtime.audit("professional_source_report", {"topic": "private research",
                "urls": ["https://example.org/private"], "output_path": str(Path(folder) / "secret.pdf")},
                "source contents")
            audit = runtime.audit_path.read_text(encoding="utf-8")
            for secret in ("private research", "example.org/private", "secret.pdf", "source contents"):
                self.assertNotIn(secret, audit)


if __name__ == "__main__":
    unittest.main()

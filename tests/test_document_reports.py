import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from document_reports import create_research_pdf
from evidence_research import assemble_dossier, parse_search_response
from harness_context import route_lanes, select_tools
from jarvis_mark2 import MARK2_TOOLS, Mark2Runtime


def dossier():
    response = {"output": [
        {"type": "web_search_call", "action": {"sources": [
            {"url": "https://example.org/source", "title": "Original source"},
        ]}},
        {"type": "message", "content": [{
            "type": "output_text", "text": "A cited finding with clear context.",
            "annotations": [{"type": "url_citation", "url": "https://example.org/source",
                             "title": "Original source", "start_index": 0, "end_index": 31}],
        }]},
    ]}
    return assemble_dossier("A focused topic", [parse_search_response("What is known?", response)])


class ReportTests(unittest.TestCase):
    def setUp(self):
        probe = patch.object(Mark2Runtime, "_run_source_probe", return_value={
            "status": "page_read", "quote_match": "exact_text_found",
            "excerpt": "Synthetic public page excerpt.", "text_chars_scanned": 60,
        })
        probe.start()
        self.addCleanup(probe.stop)

    def test_unverified_or_invalid_citations_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            data = dossier()
            data["findings"][0]["status"] = "unverified"
            with self.assertRaisesRegex(ValueError, "provider-linked"):
                create_research_pdf(data, str(Path(folder) / "report.pdf"))
            self.assertFalse((Path(folder) / "report.pdf").exists())

    def test_abort_before_publish_leaves_no_final_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "report.pdf"
            with self.assertRaisesRegex(RuntimeError, "deadline expired"):
                create_research_pdf(dossier(), str(target), abort_check=lambda: True)
            self.assertFalse(target.exists())

    def test_existing_destination_refused_before_paid_research(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"OPENAI_API_KEY": "test"}, clear=False,
        ):
            target = Path(folder) / "report.pdf"
            target.write_bytes(b"user-owned")
            runtime = Mark2Runtime(Path(folder))
            with patch.object(runtime, "_research_data") as search:
                result = runtime.research_pdf_report("Topic", ["Question?"], str(target))
            self.assertIn("already exists", result)
            self.assertEqual(target.read_bytes(), b"user-owned")
            search.assert_not_called()

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab") or
        (Path(__file__).resolve().parents[1] / ".artifact-deps" / "reportlab").is_dir(),
        "ReportLab not installed in this Python environment",
    )
    def test_creates_pdf_and_never_overwrites_it(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "report.pdf"
            result = create_research_pdf(dossier(), str(target))
            self.assertEqual(result["cited_sources"], 1)
            self.assertGreaterEqual(result["pages"], 1)
            self.assertIn("pages rendered", result["verification"])
            self.assertEqual(result["visuals"], ["vector source-coverage chart"])
            self.assertGreater(result["bytes"], 1_000)
            self.assertEqual(target.read_bytes()[:5], b"%PDF-")
            with self.assertRaises(FileExistsError):
                create_research_pdf(dossier(), str(target))

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab") or
        (Path(__file__).resolve().parents[1] / ".artifact-deps" / "reportlab").is_dir(),
        "ReportLab not installed in this Python environment",
    )
    def test_abort_after_render_does_not_publish_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "report.pdf"
            checks = iter([False, False, True])
            with self.assertRaisesRegex(RuntimeError, "before PDF publication"):
                create_research_pdf(dossier(), str(target), abort_check=lambda: next(checks))
            self.assertFalse(target.exists())

    @unittest.skipUnless(
        (Path(__file__).resolve().parents[1] / ".artifact-deps" / "pypdfium2").is_dir(),
        "Isolated PDF renderer not installed",
    )
    def test_render_verification_failure_does_not_publish_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "report.pdf"
            with patch("document_reports._verify_rendered_pdf", side_effect=RuntimeError("render failed")):
                with self.assertRaisesRegex(RuntimeError, "render failed"):
                    create_research_pdf(dossier(), str(target))
            self.assertFalse(target.exists())

    def test_partial_research_does_not_create_pdf(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"OPENAI_API_KEY": "test"}, clear=False,
        ):
            target = Path(folder) / "report.pdf"
            runtime = Mark2Runtime(Path(folder))
            partial = dossier()
            partial["status"] = "partial_or_unverified"
            with patch.object(runtime, "research_dossier", return_value=json.dumps(partial)):
                result = runtime.research_pdf_report("Topic", ["Question?"], str(target))
            self.assertIn("partial or unverified", result)
            self.assertFalse(target.exists())

    def test_default_folder_is_not_created_when_research_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            with patch.object(runtime, "research_dossier", return_value="error: provider unavailable"):
                result = runtime.research_pdf_report("A topic", ["Question?"])
            self.assertIn("provider unavailable", result)
            self.assertFalse((Path(folder) / "output" / "reports").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab") or
        (Path(__file__).resolve().parents[1] / ".artifact-deps" / "reportlab").is_dir(),
        "ReportLab not installed in this Python environment",
    )
    def test_successful_tool_result_points_to_new_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "report.pdf"
            runtime = Mark2Runtime(Path(folder))
            with patch.object(runtime, "research_dossier", return_value=json.dumps(dossier())):
                result = json.loads(runtime.research_pdf_report("Topic", ["Question?"], str(target)))
            self.assertEqual(Path(result["path"]), target)
            self.assertEqual(result["source_pages_inspected"], 1)
            self.assertEqual(result["source_pages_read"], 1)
            self.assertEqual(target.read_bytes()[:5], b"%PDF-")

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab") or
        (Path(__file__).resolve().parents[1] / ".artifact-deps" / "reportlab").is_dir(),
        "ReportLab not installed in this Python environment",
    )
    def test_report_can_choose_unique_default_path(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            with patch.object(runtime, "research_dossier", return_value=json.dumps(dossier())):
                first = json.loads(runtime.research_pdf_report("A topic", ["Question?"]))
                second = json.loads(runtime.research_pdf_report("A topic", ["Question?"]))
            first_path = Path(first["path"])
            second_path = Path(second["path"])
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(first_path.parent, Path(folder) / "output" / "reports")
            self.assertTrue(first_path.is_file() and second_path.is_file())

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab") or
        (Path(__file__).resolve().parents[1] / ".artifact-deps" / "reportlab").is_dir(),
        "ReportLab not installed in this Python environment",
    )
    def test_guarded_route_accepts_no_explicit_output_path(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            with patch.object(runtime, "research_dossier", return_value=json.dumps(dossier())):
                result = json.loads(runtime.execute("research_pdf_report", {
                    "topic": "A topic", "questions": ["Question?"],
                }))
            self.assertTrue(Path(result["path"]).is_file())
            self.assertIn("output", result["path"])

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab") or
        (Path(__file__).resolve().parents[1] / ".artifact-deps" / "reportlab").is_dir(),
        "ReportLab not installed in this Python environment",
    )
    def test_guarded_route_has_deadline_and_verification_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "guarded.pdf"
            runtime = Mark2Runtime(Path(folder))
            args = {"topic": "Topic", "questions": ["Question?"], "output_path": str(target)}
            with patch.object(runtime, "research_dossier", return_value=json.dumps(dossier())):
                result = runtime.execute("research_pdf_report", args)
            data = json.loads(result)
            self.assertEqual(Path(data["path"]), target)
            self.assertEqual(runtime.verification.evaluate("research_pdf_report", args, result).status, "verified")

    def test_tool_is_routed_and_audit_redacts_research_details(self):
        names = {tool["function"]["name"] for tool in select_tools(MARK2_TOOLS, route_lanes("make a research PDF"))}
        self.assertIn("research_pdf_report", names)
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            runtime.audit("research_pdf_report", {
                "topic": "private topic", "questions": ["secret question"],
                "output_path": str(Path(folder) / "secret-output.pdf"),
            }, "report content")
            audit = runtime.audit_path.read_text(encoding="utf-8")
            self.assertNotIn("private topic", audit)
            self.assertNotIn("secret question", audit)
            self.assertNotIn("secret-output.pdf", audit)
            self.assertNotIn("report content", audit)

    @unittest.skipUnless(
        (Path(__file__).resolve().parents[1] / ".artifact-deps" / "pypdfium2").is_dir(),
        "Isolated PDF renderer not installed",
    )
    def test_direct_source_digest_uses_no_paid_search_and_labels_excerpts(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "digest.pdf"
            runtime = Mark2Runtime(Path(folder))
            public_url = "https://example.org/public-report"
            page = {"status": "page_read", "final_url": public_url,
                    "page_title": "Public report", "excerpt": "Observed public material. " * 20,
                    "text_chars_scanned": 500}
            with patch.object(runtime, "_run_source_probe", return_value=page) as probe, \
                    patch.object(runtime, "research_dossier") as paid:
                result = json.loads(runtime.execute("source_digest_pdf", {
                    "topic": "Public report digest", "urls": [public_url],
                    "output_path": str(target),
                }))
            paid.assert_not_called()
            probe.assert_called_once_with({"mode": "digest", "url": public_url}, 6)
            self.assertEqual(result["research_mode"], "direct_source")
            self.assertEqual(result["web_search_events_observed"], 0)
            self.assertEqual(result["source_pages_read"], 1)
            self.assertEqual(runtime.verification.evaluate("source_digest_pdf", {}, json.dumps(result)).status,
                             "verified")
            from document_reports import _verify_rendered_pdf
            self.assertGreaterEqual(_verify_rendered_pdf(target, direct=True), 1)
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(target))
            try:
                text = " ".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
            finally:
                pdf.close()
            self.assertIn("PUBLIC SOURCE DIGEST", text)
            self.assertIn("No OpenAI web-search call", text)
            self.assertIn("Observed public material", text)
            self.assertNotIn("Provider-linked sources", text)

    def test_direct_source_digest_fails_closed_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            target = Path(folder) / "digest.pdf"
            target.write_bytes(b"user-owned")
            with patch.object(runtime, "_run_source_probe") as probe:
                result = runtime.source_digest_pdf("Topic", ["https://example.org"], str(target))
            self.assertIn("already exists", result)
            probe.assert_not_called()
            self.assertEqual(target.read_bytes(), b"user-owned")
            target.unlink()
            with patch.object(runtime, "_run_source_probe", return_value={"status": "unsafe"}), \
                    patch.object(runtime, "research_dossier") as paid:
                result = runtime.source_digest_pdf("Topic", ["https://example.org"], str(target))
            self.assertIn("readable public page text", result)
            paid.assert_not_called()
            self.assertFalse(target.exists())

    def test_direct_source_tool_is_routed_and_audit_redacts_urls(self):
        names = {tool["function"]["name"] for tool in select_tools(MARK2_TOOLS, route_lanes("create PDF from these URLs"))}
        self.assertIn("source_digest_pdf", names)
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            runtime.audit("source_digest_pdf", {
                "topic": "private topic", "urls": ["https://example.org/private-path"],
                "output_path": str(Path(folder) / "private-output.pdf"),
            }, "untrusted page text")
            audit = runtime.audit_path.read_text(encoding="utf-8")
            for secret in ("private topic", "private-path", "private-output.pdf", "untrusted page text"):
                self.assertNotIn(secret, audit)


if __name__ == "__main__":
    unittest.main()

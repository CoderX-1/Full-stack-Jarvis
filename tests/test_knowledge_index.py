import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import agent
from harness_context import route_lanes, select_tools
from jarvis_mark2 import MARK2_TOOLS, Mark2Runtime
from knowledge_index import KnowledgeIndex, KnowledgeIndexError


class KnowledgeIndexTests(unittest.TestCase):
    def test_search_and_status_do_not_create_a_database(self):
        with tempfile.TemporaryDirectory() as folder:
            index = KnowledgeIndex(Path(folder) / "state")
            self.assertIn("sources=0", index.status())
            self.assertIn("No matching", index.search("sample"))
            self.assertFalse(index.db_path.exists())

    def test_full_document_is_searchable_beyond_old_16k_cutoff(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            document = root / "long.md"
            document.write_text("ordinary context " * 1_200 + " silver-comet-42 is the answer", encoding="utf-8")
            index = KnowledgeIndex(root / "state")
            self.assertIn("Indexed and verified", index.index_document(str(document)))
            result = index.search("silver-comet-42")
            self.assertIn("silver-comet-42", result)
            self.assertIn("long.md (document)", result)
            self.assertIn(str(document), result)

    def test_idempotent_reindex_and_stale_passage_removal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            document = root / "notes.txt"
            document.write_text("first evidence token", encoding="utf-8")
            index = KnowledgeIndex(root / "state")
            index.index_document(str(document))
            self.assertIn("Already indexed", index.index_document(str(document)))
            document.write_text("replacement evidence signal", encoding="utf-8")
            self.assertIn("source changed", index.search("token"))
            index.index_document(str(document))
            self.assertIn("No matching", index.search("token"))
            self.assertIn("replacement", index.search("replacement"))
            self.assertIn("sources=1; passages=1", index.status())

    def test_file_changed_during_extraction_is_not_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            document = root / "changing.txt"
            document.write_text("original evidence", encoding="utf-8")
            index = KnowledgeIndex(root / "state")

            def changed(_path):
                document.write_text("different evidence", encoding="utf-8")
                return [("document", "original evidence")]

            with patch("knowledge_index._extract", side_effect=changed):
                with self.assertRaisesRegex(KnowledgeIndexError, "changed during indexing"):
                    index.index_document(str(document))
            self.assertIn("sources=0", index.status())

    def test_forget_removes_only_indexed_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            document = root / "retain.md"
            document.write_text("persistent private finding", encoding="utf-8")
            index = KnowledgeIndex(root / "state")
            index.index_document(str(document))
            self.assertIn("Forgot and verified", index.forget_document(str(document)))
            self.assertTrue(document.is_file())
            self.assertIn("No matching", index.search("persistent"))
            self.assertIn("sources=0", index.status())

    def test_docx_and_pptx_locators(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            docx = root / "memo.docx"
            with zipfile.ZipFile(docx, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:p><w:r><w:t>verified document finding</w:t></w:r></w:p></w:document>',
                )
            pptx = root / "deck.pptx"
            with zipfile.ZipFile(pptx, "w") as archive:
                archive.writestr(
                    "ppt/slides/slide2.xml",
                    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    '<a:t>launch milestone</a:t></p:sld>',
                )
            index = KnowledgeIndex(root / "state")
            index.index_document(str(docx))
            index.index_document(str(pptx))
            self.assertIn("memo.docx (document)", index.search("finding"))
            self.assertIn("deck.pptx (slide 2)", index.search("milestone"))

    def test_pdf_page_locators_and_scanned_pdf_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = root / "report.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            index = KnowledgeIndex(root / "state")
            with patch("knowledge_index.shutil.which", return_value="pdftotext"), patch(
                "knowledge_index.subprocess.run",
                return_value=CompletedProcess([], 0, b"opening summary\fsecond page finding\f", b""),
            ):
                index.index_document(str(pdf))
            self.assertIn("report.pdf (page 2)", index.search("finding"))
            pdf.write_bytes(b"%PDF-1.4\nchanged")
            with patch("knowledge_index.shutil.which", return_value="pdftotext"), patch(
                "knowledge_index.subprocess.run",
                return_value=CompletedProcess([], 0, b"\f", b""),
            ):
                with self.assertRaisesRegex(KnowledgeIndexError, "scanned PDFs"):
                    index.index_document(str(pdf))
            self.assertIn("source changed", index.search("finding"))

    def test_rejects_unsupported_and_bloated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            secret = root / ".env"
            secret.write_text("API_KEY=never-index", encoding="utf-8")
            index = KnowledgeIndex(root / "state")
            with self.assertRaises(KnowledgeIndexError):
                index.index_document(str(secret))
            bloated = root / "bloated.docx"
            with zipfile.ZipFile(bloated, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", "x" * 24_000_001)
            with self.assertRaisesRegex(KnowledgeIndexError, "too large"):
                index.index_document(str(bloated))

    def test_tool_routing_verification_and_private_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            document = root / "private notes.md"
            document.write_text("private launch schedule", encoding="utf-8")
            names = {item["function"]["name"] for item in select_tools(MARK2_TOOLS, route_lanes("Search this PDF document"))}
            self.assertTrue({"index_document", "search_documents", "forget_document", "knowledge_status"} <= names)
            runtime = Mark2Runtime(root)
            indexed = runtime.execute("index_document", {"path": str(document)})
            self.assertIn("Indexed and verified", indexed)
            self.assertIn("private notes.md", runtime.execute("search_documents", {"query": "launch"}))
            runtime.audit("index_document", {"path": str(document)}, indexed)
            result = runtime.execute("search_documents", {"query": "private launch"})
            runtime.audit("search_documents", {"query": "private launch"}, result)
            events = [json.loads(line) for line in runtime.audit_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(events[0]["success"])
            self.assertNotIn("private notes", json.dumps(events))
            self.assertNotIn("private launch", json.dumps(events))


class ExplicitIndexRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_cannot_index_without_current_user_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            document = root / "notes.txt"
            document.write_text("private content", encoding="utf-8")
            config = agent.ProviderConfig("openai", "test", agent.OPENAI_BASE_URL, "test")
            runner = agent.LocalAgent(config, root, "instructions", auto_approve=True)
            denied = await runner._run_tool("index_document", {"path": str(document)})
            self.assertIn("explicit user request", denied)
            runner._index_requested = True
            indexed = await runner._run_tool("index_document", {"path": str(document)})
            self.assertIn("Indexed and verified", indexed)
            denied_forget = await runner._run_tool("forget_document", {"path": str(document)})
            self.assertIn("explicit user request", denied_forget)
            runner._forget_document_requested = True
            forgotten = await runner._run_tool("forget_document", {"path": str(document)})
            self.assertIn("Forgot and verified", forgotten)
            self.assertTrue(document.exists())


if __name__ == "__main__":
    unittest.main()

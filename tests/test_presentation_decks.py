import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from harness_context import route_lanes, select_tools
from jarvis_mark2 import MARK2_TOOLS, Mark2Runtime
from presentation_decks import A, H, W, create_evidence_presentation
from tests.test_professional_reports import PAGES, URLS, QUOTE1, QUOTE2, brief


def dossier():
    data = brief()
    return {"topic": "Project update", "research_mode": "professional_brief",
            "summary": data["summary"], "findings": data["findings"],
            "sources": [{"id": f"S{i}", "url": URLS[i-1],
                         "title": PAGES[i-1]["page_title"], "page_inspection": PAGES[i-1]}
                        for i in (1, 2)]}


class PresentationDeckTests(unittest.TestCase):
    def test_package_has_editable_slide_text_sources_and_no_macro_or_media(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "brief.pptx"
            result = create_evidence_presentation(dossier(), str(path))
            self.assertEqual(result["slides"], 5)
            self.assertEqual(result["cited_sources"], 2)
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertIn("ppt/presProps.xml", names)
                self.assertFalse(any("vbaProject" in name or name.startswith("ppt/media/")
                                     for name in names))
                self.assertEqual(len([name for name in names if name.startswith("ppt/slides/slide")
                                      and name.endswith(".xml")]), 5)
                slides = [ET.fromstring(archive.read(f"ppt/slides/slide{i}.xml"))
                          for i in range(1, 6)]
                texts = [[node.text or "" for node in slide.iter(f"{{{A}}}t")]
                         for slide in slides]
                self.assertIn("Project update", texts[0])
                self.assertIn(QUOTE1, " ".join(texts[2]))
                self.assertIn(QUOTE2, " ".join(texts[3]))
                self.assertIn("example.org/first", " ".join(texts[4]))
                self.assertIn("other.example.net/second", " ".join(texts[4]))
                self.assertIn(URLS[0], archive.read("ppt/slides/_rels/slide3.xml.rels").decode())
                self.assertIn('TargetMode="External"',
                              archive.read("ppt/slides/_rels/slide5.xml.rels").decode())
                for slide in slides:
                    for ext in slide.iter(f"{{{A}}}ext"):
                        self.assertLessEqual(int(ext.attrib["cx"]), W)
                        self.assertLessEqual(int(ext.attrib["cy"]), H)
            with self.assertRaisesRegex(ValueError, "new .pptx"):
                create_evidence_presentation(dossier(), str(path))

    def test_tensions_and_untrusted_text_do_not_create_ooxml_markup(self):
        with tempfile.TemporaryDirectory() as folder:
            data = dossier()
            data["topic"] = "<script>Project & review</script>"
            data["potential_tensions"] = [{"source_ids": ["S1", "S2"],
                                           "quotes": [QUOTE1, QUOTE2]}]
            path = Path(folder) / "review.pptx"
            result = create_evidence_presentation(data, str(path))
            self.assertEqual(result["slides"], 6)
            with zipfile.ZipFile(path) as archive:
                raw = archive.read("ppt/slides/slide1.xml")
                self.assertNotIn(b"<script>", raw)
                ET.fromstring(raw)
                tension = ET.fromstring(archive.read("ppt/slides/slide5.xml"))
                self.assertIn("Potential source tensions",
                              [node.text for node in tension.iter(f"{{{A}}}t")])
            data["potential_tensions"][0]["quotes"][1] = "Invented claim."
            with self.assertRaisesRegex(ValueError, "Potential tension must cite"):
                create_evidence_presentation(data, str(Path(folder) / "forged.pptx"))

    def test_runtime_source_deck_and_tool_registry(self):
        names = {tool["function"]["name"] for tool in select_tools(MARK2_TOOLS, route_lanes("make a research pptx"))}
        self.assertIn("professional_source_presentation", names)
        self.assertIn("professional_topic_presentation", names)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"GEMINI_API_KEY": "fake"}):
            runtime = Mark2Runtime(Path(folder))
            path = Path(folder) / "deck.pptx"
            with patch.object(runtime, "_run_source_probe", side_effect=PAGES), \
                    patch("jarvis_mark2.synthesize_public_brief", return_value=brief()) as gemini:
                raw = runtime.execute("professional_source_presentation", {
                    "topic": "Project update", "urls": URLS, "output_path": str(path)})
            self.assertTrue(raw.startswith("unverified: an observable response occurred"))
            result = json.loads(raw.split("; ", 2)[-1])
            self.assertEqual(result["slides"], 5)
            self.assertEqual(gemini.call_count, 1)
            self.assertEqual(runtime.verification.evaluate(
                "professional_source_presentation", {}, json.dumps(result)).status, "observed")
            self.assertIn("already exists", runtime.professional_source_presentation(
                "Project update", URLS, str(path)))

    def test_runtime_topic_deck_reuses_discovery_pages(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "GEMINI_API_KEY": "fake", "BRAVE_SEARCH_API_KEY": "fake"}):
            runtime = Mark2Runtime(Path(folder))
            with patch("jarvis_mark2.discover_public_urls", return_value=URLS) as discover, \
                    patch.object(runtime, "_run_source_probe", side_effect=PAGES) as probe, \
                    patch("jarvis_mark2.synthesize_public_brief", return_value=brief()) as gemini:
                result = json.loads(runtime.professional_topic_presentation("Project update"))
            self.assertEqual(result["slides"], 5)
            self.assertTrue(Path(result["path"]).is_file())
            self.assertEqual(discover.call_count, 1)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(gemini.call_count, 1)


if __name__ == "__main__":
    unittest.main()

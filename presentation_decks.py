"""Dependency-free, editable OOXML evidence decks from validated research data.

This intentionally uses text-only editorial layouts. It never executes source
content, imports remote media, overwrites an existing file, or claims facts
were independently checked. PowerPoint/LibreOffice visual acceptance remains
separate from package and geometry checks.
"""

from __future__ import annotations

import os
import posixpath
import re
import tempfile
import urllib.parse
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from document_reports import _validate


P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
W, H = 12_192_000, 6_858_000


def _xml(value: Any, limit: int) -> str:
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))
    return escape(re.sub(r"\s+", " ", clean).strip()[:limit])


def _display_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    visible = (parsed.hostname or "") + parsed.path
    if parsed.query:
        visible += "?" + parsed.query
    return visible if len(visible) <= 104 else visible[:101].rstrip("/.-") + "..."


def _shape(number: int, value: Any, x: int, y: int, w: int, h: int,
           size: int, color: str = "17283D", bold: bool = False,
           link_id: str = "") -> str:
    text = _xml(value, 600)
    weight = ' b="1"' if bold else ""
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{number}" name="Text {number}"/>'
            '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr>'
            f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>'
            '<a:ln><a:noFill/></a:ln></p:spPr><p:txBody>'
            '<a:bodyPr wrap="square" anchor="t" lIns="0" rIns="0" tIns="0" bIns="0"/>'
            '<a:lstStyle/><a:p><a:pPr/><a:r>'
            f'<a:rPr lang="en-US" sz="{size * 100}"{weight}>'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            '<a:latin typeface="Arial"/>'
            + (f'<a:hlinkClick r:id="{link_id}"/>' if link_id else '') + '</a:rPr>'
            f'<a:t>{text}</a:t></a:r><a:endParaRPr lang="en-US"/></a:p>'
            '</p:txBody></p:sp>')


def _slide(shapes: list[str]) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
            '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
            '<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr>'
            '<a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
            '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm>'
            '</p:grpSpPr>' + "".join(shapes) +
            '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/>'
            '</p:clrMapOvr></p:sld>').encode("utf-8")


def _rels(items: list[tuple[str, ...]]) -> bytes:
    parts = [f'<Relationship Id="{rid}" Type="{kind}" Target={quoteattr(target)}'
             + (' TargetMode="External"' if len(item) == 4 else '') + '/>'
             for item in items for rid, kind, target in [item[:3]]]
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL}">' + "".join(parts) +
            '</Relationships>').encode("utf-8")


def _slides(dossier: dict[str, Any]) -> list[bytes]:
    topic, findings, sources = _validate(dossier)
    if dossier.get("research_mode") != "professional_brief":
        raise ValueError("Presentation requires a validated professional brief")
    source_by_id = {item["id"]: item for item in sources}
    slides: list[bytes] = []
    title_size = 44 if len(topic) <= 65 else 34 if len(topic) <= 110 else 26
    slides.append(_slide([
        _shape(2, "JARVIS research", 760_000, 720_000, 10_600_000, 500_000, 19, "087A94"),
        _shape(3, topic, 760_000, 1_460_000, 10_600_000, 3_220_000, title_size, bold=True),
        _shape(4, "Evidence brief from inspected public pages", 760_000, 4_990_000,
               10_600_000, 650_000, 22),
        _shape(5, "Source text is attributed, not independently fact-checked.",
               760_000, 5_830_000, 10_600_000, 400_000, 15, "536579"),
    ]))
    slides.append(_slide([
        _shape(2, "Executive summary", 760_000, 600_000, 10_500_000, 700_000, 34, bold=True),
        _shape(3, dossier["summary"], 760_000, 1_720_000, 10_400_000, 3_380_000, 22),
        _shape(4, f"Based on {len(sources)} directly inspected public source(s).",
               760_000, 5_590_000, 10_500_000, 450_000, 16, "536579"),
    ]))
    for index, finding in enumerate(findings, 1):
        source_id = finding["source_ids"][0]
        source = source_by_id[source_id]
        slides.append(_slide([
            _shape(2, finding["question"], 760_000, 510_000, 10_650_000, 1_100_000, 31, bold=True),
            _shape(3, finding["answer_draft"], 760_000, 1_620_000, 10_650_000, 2_230_000, 22),
            _shape(4, f'"{finding["evidence_quote"]}"', 760_000, 4_080_000,
                   10_650_000, 1_050_000, 19, "087A94"),
            _shape(5, f'{source_id}  {str(source.get("title") or "Public source")[:100]}',
                   760_000, 5_390_000, 10_650_000, 450_000, 15, "536579"),
            _shape(6, _display_url(source["url"]), 760_000, 5_900_000, 10_650_000, 570_000, 14,
                   "087A94", link_id="rId2"),
        ]))
    tensions = dossier.get("potential_tensions", [])
    if tensions:
        shapes = [_shape(2, "Potential source tensions", 760_000, 580_000,
                         10_650_000, 750_000, 33, bold=True),
                  _shape(3, "AI-flagged for review. Compare date, scope and original context.",
                         760_000, 1_380_000, 10_650_000, 580_000, 19, "536579")]
        top = 2_060_000
        for tension in tensions[:2]:
            for source_id, quote in zip(tension["source_ids"], tension["quotes"]):
                shapes.append(_shape(len(shapes) + 2, f'{source_id}: "{quote}"',
                                     760_000, top, 10_650_000, 890_000, 17))
                top += 1_030_000
        slides.append(_slide(shapes))
    source_lines = [f'{source["id"]}  {_display_url(source["url"])}' for source in sources]
    shapes = [_shape(2, "Sources and limits", 760_000, 580_000, 10_650_000, 750_000, 33, bold=True)]
    for index, line in enumerate(source_lines):
        shapes.append(_shape(index + 3, line, 760_000, 1_650_000 + index * 750_000,
                             10_650_000, 590_000, 15, "087A94", link_id=f"rId{index + 2}"))
    shapes.append(_shape(8, "Quotes were checked against fetched excerpts. Interpretation, publication date, completeness and factual truth were not independently verified.",
                         760_000, 5_340_000, 10_650_000, 950_000, 17, "536579"))
    slides.append(_slide(shapes))
    return slides


def _package(slides: list[bytes], target: Path, dossier: dict[str, Any]) -> None:
    n = len(slides)
    overrides = [
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
        '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>',
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>',
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>',
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>',
    ] + [f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
         for i in range(1, n + 1)]
    content_types = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     f'<Types xmlns="{CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>' + "".join(overrides) + '</Types>')
    slide_ids = "".join(f'<p:sldId id="{255+i}" r:id="rId{i+1}"/>' for i in range(1, n + 1))
    presentation = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    f'<p:presentation xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
                    f'<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                    f'<p:sldIdLst>{slide_ids}</p:sldIdLst>'
                    f'<p:sldSz cx="{W}" cy="{H}" type="screen16x9"/>'
                    '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
    master = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<p:sldMaster xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
              '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
              '<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
              '<a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
              '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>'
              '</a:xfrm></p:grpSpPr></p:spTree></p:cSld>'
              '<p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" '
              'accent4="accent4" accent5="accent5" accent6="accent6" '
              'bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" '
              'tx1="dk1" tx2="dk2"/><p:sldLayoutIdLst>'
              '<p:sldLayoutId id="2147483649" r:id="rId1"/>'
              '</p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/>'
              '<p:otherStyle/></p:txStyles></p:sldMaster>')
    layout = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<p:sldLayout xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}" type="blank" preserve="1">'
              '<p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
              '<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
              '<a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
              '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>'
              '</a:xfrm></p:grpSpPr></p:spTree></p:cSld>'
              '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    theme = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             f'<a:theme xmlns:a="{A}" name="JARVIS Editorial"><a:themeElements>'
             '<a:clrScheme name="JARVIS"><a:dk1><a:srgbClr val="17283D"/></a:dk1>'
             '<a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>'
             '<a:dk2><a:srgbClr val="536579"/></a:dk2>'
             '<a:lt2><a:srgbClr val="F4F8FA"/></a:lt2>' +
             "".join(f'<a:accent{i}><a:srgbClr val="087A94"/></a:accent{i}>' for i in range(1, 7)) +
             '<a:hlink><a:srgbClr val="087A94"/></a:hlink>'
             '<a:folHlink><a:srgbClr val="536579"/></a:folHlink></a:clrScheme>'
             '<a:fontScheme name="Arial"><a:majorFont><a:latin typeface="Arial"/></a:majorFont>'
             '<a:minorFont><a:latin typeface="Arial"/></a:minorFont></a:fontScheme>'
             '<a:fmtScheme name="JARVIS"><a:fillStyleLst>' +
             '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>' * 3 +
             '</a:fillStyleLst><a:lnStyleLst>' +
             '<a:ln w="9525"><a:noFill/></a:ln>' * 3 +
             '</a:lnStyleLst><a:effectStyleLst>' +
             '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3 +
             '</a:effectStyleLst><a:bgFillStyleLst>' +
             '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>' * 3 +
             '</a:bgFillStyleLst></a:fmtScheme>'
             '</a:themeElements></a:theme>')
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", _rels([("rId1", R + "/officeDocument", "ppt/presentation.xml")]))
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/presProps.xml", f'<p:presentationPr xmlns:p="{P}"/>')
        archive.writestr("ppt/_rels/presentation.xml.rels", _rels(
            [("rId1", R + "/slideMaster", "slideMasters/slideMaster1.xml")] +
            [(f"rId{i+1}", R + "/slide", f"slides/slide{i}.xml") for i in range(1, n + 1)] +
            [(f"rId{n+2}", R + "/presProps", "presProps.xml")]))
        archive.writestr("ppt/slideMasters/slideMaster1.xml", master)
        archive.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", _rels([
            ("rId1", R + "/slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", R + "/theme", "../theme/theme1.xml")]))
        archive.writestr("ppt/slideLayouts/slideLayout1.xml", layout)
        archive.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _rels([
            ("rId1", R + "/slideMaster", "../slideMasters/slideMaster1.xml")]))
        archive.writestr("ppt/theme/theme1.xml", theme)
        source_by_id = {source["id"]: source for source in dossier["sources"]}
        finding_count = len(dossier["findings"])
        for i, slide in enumerate(slides, 1):
            archive.writestr(f"ppt/slides/slide{i}.xml", slide)
            links: list[tuple[str, ...]] = []
            if 3 <= i <= finding_count + 2:
                source_id = dossier["findings"][i - 3]["source_ids"][0]
                links.append(("rId2", R + "/hyperlink", source_by_id[source_id]["url"], "External"))
            elif i == n:
                links.extend((f"rId{index + 2}", R + "/hyperlink", source["url"], "External")
                             for index, source in enumerate(dossier["sources"]))
            archive.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", _rels([
                ("rId1", R + "/slideLayout", "../slideLayouts/slideLayout1.xml"), *links]))


def verify_evidence_presentation(path: str | Path, expected_slides: int,
                                 source_urls: list[str]) -> None:
    """Fail closed on broken package links, missing evidence or unsafe externals.

    This checks portability and editable structure, not slide rendering or
    native PowerPoint compatibility.
    """
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        known = set(names)
        if (len(names) != len(known) or len(names) > 50 or
                archive.testzip() is not None or
                any(item.file_size > 1_000_000 for item in archive.infolist())):
            raise RuntimeError("PPTX package integrity check failed")
        required = {"[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml",
                    "ppt/presProps.xml", "ppt/slideMasters/slideMaster1.xml",
                    "ppt/slideLayouts/slideLayout1.xml", "ppt/theme/theme1.xml"}
        required.update(f"ppt/slides/slide{i}.xml" for i in range(1, expected_slides + 1))
        if not required.issubset(known) or any("vbaProject" in name or name.startswith("ppt/media/")
                                              for name in names):
            raise RuntimeError("PPTX is missing a required part or contains unexpected media")
        parsed: dict[str, ET.Element] = {}
        for name in names:
            if name.endswith((".xml", ".rels")):
                parsed[name] = ET.fromstring(archive.read(name))
        presentation = parsed["ppt/presentation.xml"]
        slide_size = presentation.find(f"{{{P}}}sldSz")
        slide_list = presentation.find(f"{{{P}}}sldIdLst")
        if (slide_size is None or slide_size.get("cx") != str(W) or
                slide_size.get("cy") != str(H) or slide_list is None or
                len(slide_list) != expected_slides):
            raise RuntimeError("PPTX slide dimensions or order are invalid")
        external_targets: set[str] = set()
        rels_by_part: dict[str, dict[str, ET.Element]] = {}
        for name, root in parsed.items():
            if not name.endswith(".rels"):
                continue
            base = "" if name == "_rels/.rels" else posixpath.dirname(posixpath.dirname(name))
            rels: dict[str, ET.Element] = {}
            for rel in root:
                rid = rel.get("Id")
                target = rel.get("Target")
                if not rid or rid in rels or not target:
                    raise RuntimeError("PPTX relationship ID or target is invalid")
                rels[rid] = rel
                if rel.get("TargetMode") == "External":
                    if rel.get("Type") != R + "/hyperlink" or target not in source_urls:
                        raise RuntimeError("PPTX contains an unapproved external link")
                    external_targets.add(target)
                else:
                    resolved = posixpath.normpath(posixpath.join(base, target))
                    if resolved not in known or resolved.startswith("../"):
                        raise RuntimeError("PPTX relationship points to a missing part")
            rels_by_part[name] = rels
        presentation_rels = rels_by_part.get("ppt/_rels/presentation.xml.rels", {})
        for i, item in enumerate(slide_list, 1):
            rid = item.get(f"{{{R}}}id")
            rel = presentation_rels.get(rid)
            if rel is None or rel.get("Target") != f"slides/slide{i}.xml":
                raise RuntimeError("PPTX slide relationship order is invalid")
            slide = parsed[f"ppt/slides/slide{i}.xml"]
            if not list(slide.iter(f"{{{A}}}t")):
                raise RuntimeError("PPTX slide has no editable text")
            slide_rels = rels_by_part.get(f"ppt/slides/_rels/slide{i}.xml.rels", {})
            ids = set()
            for shape in slide.iter(f"{{{P}}}sp"):
                properties = shape.find(f"{{{P}}}nvSpPr/{{{P}}}cNvPr")
                position = shape.find(f"{{{P}}}spPr/{{{A}}}xfrm")
                off = position.find(f"{{{A}}}off") if position is not None else None
                ext = position.find(f"{{{A}}}ext") if position is not None else None
                if properties is None or off is None or ext is None:
                    raise RuntimeError("PPTX text shape is missing geometry")
                shape_id = properties.get("id")
                if not shape_id or shape_id in ids:
                    raise RuntimeError("PPTX shape IDs are duplicated")
                ids.add(shape_id)
                x, y = int(off.get("x", "-1")), int(off.get("y", "-1"))
                width, height = int(ext.get("cx", "-1")), int(ext.get("cy", "-1"))
                if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > W or y + height > H:
                    raise RuntimeError("PPTX text shape extends outside the slide")
            for link in slide.iter(f"{{{A}}}hlinkClick"):
                rid = link.get(f"{{{R}}}id")
                if rid not in slide_rels or slide_rels[rid].get("TargetMode") != "External":
                    raise RuntimeError("PPTX clickable source has no matching hyperlink")
        if not set(source_urls).issubset(external_targets):
            raise RuntimeError("PPTX source manifest is missing a clickable link")


def create_evidence_presentation(dossier: dict[str, Any], output_path: str,
                                 abort_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    target = Path(output_path).expanduser().resolve(strict=False)
    if target.suffix.casefold() != ".pptx" or not target.parent.is_dir() or target.exists():
        raise ValueError("Choose a new .pptx filename inside an existing folder")
    if abort_check and abort_check():
        raise RuntimeError("action deadline expired before deck generation")
    slides = _slides(dossier)
    fd, temporary = tempfile.mkstemp(prefix=".jarvis-pptx-", suffix=".pptx", dir=target.parent)
    os.close(fd)
    created_target = False
    try:
        _package(slides, Path(temporary), dossier)
        source_urls = [source["url"] for source in dossier["sources"]]
        verify_evidence_presentation(temporary, len(slides), source_urls)
        if abort_check and abort_check():
            raise RuntimeError("action deadline expired during deck verification")
        with open(temporary, "rb") as source, open(target, "xb") as output:
            created_target = True
            while chunk := source.read(65_536):
                output.write(chunk)
        if sha256(Path(temporary).read_bytes()).digest() != sha256(target.read_bytes()).digest():
            raise RuntimeError("Published PPTX differs from the validated package")
        return {"path": str(target), "bytes": target.stat().st_size,
                "slides": len(slides), "cited_sources": len(dossier["sources"]),
                "research_mode": "professional_brief",
                "verification": "PPTX package and editable slide text verified; visual opening still required"}
    except Exception:
        if created_target:
            target.unlink(missing_ok=True)
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)

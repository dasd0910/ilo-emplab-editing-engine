"""
idml_writer.py — inject cleaned Word content into an ILO InDesign template.

IDML is a ZIP of XML files. Our strategy:
  1. Unzip the chosen template (Generic / Policy / Fact Sheet).
  2. Locate the "main body" Story (largest Story_*.xml that uses the
     template's body paragraph style).
  3. Build a new <Story> element with one <ParagraphStyleRange> per docx
     paragraph, picking the IDML paragraph style based on the paragraph's
     Word style (Heading 1/2/3/Normal/etc.).
  4. Replace the main-body Story.xml in the zip with our new one.
  5. Re-zip and return the bytes.

What v1 does NOT handle (documented limitations):
  - Sidebars, call-out boxes, cover titles, captions → kept as template
    placeholders; a designer fills them in.
  - Images → not placed automatically.
  - Footnotes → stripped (too complex for v1).
  - Overflow → if docx body is longer than the template's text frame, a
    red InDesign overflow marker will appear; designer reflows manually.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from docx import Document


# -----------------------------------------------------------------------------
# Per-template style mappings
# -----------------------------------------------------------------------------
@dataclass
class TemplateProfile:
    template_file: str                       # filename within the assets dir
    body_style: str                          # IDML paragraph style for body text
    h2_style: str                            # for Heading 1 / 2 in the docx
    h3_style: str                            # for Heading 3 in the docx
    bullet_style: str                        # for list items
    matches_body_keyword: str                # heuristic to find the main-body Story


# Filenames are looked up in the workspace root (parent of emplab_app/)
TEMPLATES: Dict[str, TemplateProfile] = {
    "Generic Brief": TemplateProfile(
        template_file="Generic Brief Digital [RGB template].idml",
        body_style="Briefing A4 - Body",
        h2_style="Briefing A4 - Sub heading",
        h3_style="Briefing A4 - Crosshead",
        bullet_style="Briefing A4 - Bullet",
        matches_body_keyword="Briefing A4 - Body",
    ),
    "Policy Brief": TemplateProfile(
        template_file="Policy Brief Digital [RGB template].idml",
        body_style="Briefing A4 - Body",
        h2_style="Briefing A4 - Sub heading",
        h3_style="Briefing A4 - Crosshead",
        bullet_style="Briefing A4 - Bullet",
        matches_body_keyword="Briefing A4 - Body",
    ),
    "Fact Sheet": TemplateProfile(
        template_file="EN_ILO_Fact_Sheet_A4_Portrait_CMYK_.idml",
        body_style="Fact sheet A4 - Body text",
        h2_style="Fact sheet A4 - heading level 2",
        h3_style="Fact sheet A4 - heading level 3",
        bullet_style="Bullet point level 2%3aBullet point level 2 black",
        matches_body_keyword="Fact sheet A4 - Body text",
    ),
}


# -----------------------------------------------------------------------------
# Classify docx paragraph into an IDML style
# -----------------------------------------------------------------------------
def _classify_paragraph(para, profile: TemplateProfile) -> str:
    """Pick an IDML paragraph style based on the docx paragraph's Word style."""
    style = (para.style.name or "").lower() if para.style else ""

    if "heading 1" in style or "heading 2" in style or "title" in style:
        return profile.h2_style
    if "heading 3" in style or "heading 4" in style:
        return profile.h3_style
    if "list" in style or "bullet" in style:
        return profile.bullet_style
    return profile.body_style


# -----------------------------------------------------------------------------
# Build a <ParagraphStyleRange> from a piece of text
# -----------------------------------------------------------------------------
def _make_para_xml(style_name: str, text: str) -> str:
    """Build one <ParagraphStyleRange> block with the given style + text."""
    # IDML uses ParagraphStyle/<name>. The name may contain spaces; that's OK.
    escaped = xml_escape(text)
    return (
        f'<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/{style_name}">'
        f'<CharacterStyleRange AppliedCharacterStyle="CharacterStyle/$ID/[No character style]">'
        f'<Content>{escaped}</Content>'
        f'</CharacterStyleRange>'
        f'<Br />'
        f'</ParagraphStyleRange>'
    )


# -----------------------------------------------------------------------------
# Find the main-body Story inside the unzipped IDML
# -----------------------------------------------------------------------------
def _find_main_body_story(contents: Dict[str, bytes], profile: TemplateProfile) -> Optional[str]:
    """Return the zip path of the Story.xml that's most likely the body."""
    candidates: List[tuple] = []  # (score, size, name)
    for name, data in contents.items():
        if not name.startswith("Stories/") or not name.endswith(".xml"):
            continue
        try:
            xml = data.decode("utf-8", errors="replace")
        except Exception:
            continue
        # Score: does it apply the body style? how often? how big?
        count_body = xml.count(profile.matches_body_keyword)
        size = len(data)
        if count_body > 0:
            score = count_body * 100 + size // 100
            candidates.append((score, size, name))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


# -----------------------------------------------------------------------------
# Main build function
# -----------------------------------------------------------------------------
def build_idml(template_idml_path: Path,
               docx_document: Document,
               brief_type: str) -> bytes:
    """
    Return the bytes of a new IDML with the body-Story replaced by content
    derived from docx_document.
    """
    if brief_type not in TEMPLATES:
        raise ValueError(f"Unknown brief type: {brief_type}")
    profile = TEMPLATES[brief_type]

    # 1. Read all files from the template zip
    with zipfile.ZipFile(template_idml_path, "r") as z:
        contents = {name: z.read(name) for name in z.namelist()}

    # 2. Find the body story
    body_story_name = _find_main_body_story(contents, profile)
    if body_story_name is None:
        raise RuntimeError(
            f"Could not locate body Story in template. "
            f"Expected a Story that uses paragraph style "
            f"'{profile.matches_body_keyword}'."
        )

    # 3. Build new Story XML
    original_story_xml = contents[body_story_name].decode("utf-8")

    # Extract the Story's outer envelope (root attributes + wrapping tags)
    # The body Story's outer structure looks like:
    #   <?xml version="1.0" ..?>
    #   <idPkg:Story xmlns:idPkg="..." DOMVersion="..">
    #     <Story Self="..." ...>
    #       <StoryPreference ../>
    #       <InCopyExportOption ../>
    #       ...ParagraphStyleRanges...
    #     </Story>
    #   </idPkg:Story>
    new_story_xml = _replace_story_paragraphs(
        original_story_xml, docx_document, profile
    )
    contents[body_story_name] = new_story_xml.encode("utf-8")

    # 4. Re-zip (preserving mimetype ordering — IDML requires it)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        # mimetype must be first and stored uncompressed in OPC-family formats
        if "mimetype" in contents:
            zi = zipfile.ZipInfo("mimetype")
            zi.compress_type = zipfile.ZIP_STORED
            zout.writestr(zi, contents["mimetype"])
        for name, data in contents.items():
            if name == "mimetype":
                continue
            zout.writestr(name, data)
    return out.getvalue()


def _replace_story_paragraphs(original_xml: str, doc: Document,
                              profile: TemplateProfile) -> str:
    """
    Replace the body of a Story XML (between <Story ...> and </Story>) with
    new ParagraphStyleRange elements built from the docx paragraphs.
    The original Story's preference elements are preserved.
    """
    # Capture everything from <Story...> up to the first <ParagraphStyleRange>
    # so we keep StoryPreference and InCopyExportOption intact.
    m = re.search(r'(<Story\b[^>]*>)(.*?)(</Story>)', original_xml, re.DOTALL)
    if not m:
        raise RuntimeError("Could not parse template Story XML.")
    story_open = m.group(1)
    story_inner = m.group(2)
    story_close = m.group(3)

    # Keep StoryPreference / InCopyExportOption blocks at the top.
    preference_blocks = []
    for tag in ("StoryPreference", "InCopyExportOption"):
        pm = re.search(rf'<{tag}\b[^>]*/>', story_inner)
        if pm:
            preference_blocks.append(pm.group(0))

    # Build new paragraph blocks from the docx
    para_xml_blocks: List[str] = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style_name = _classify_paragraph(p, profile)
        para_xml_blocks.append(_make_para_xml(style_name, text))

    new_inner = "".join(preference_blocks) + "".join(para_xml_blocks)
    new_story = original_xml[:m.start()] + story_open + new_inner + story_close + original_xml[m.end():]
    return new_story


# -----------------------------------------------------------------------------
# Public path helper
# -----------------------------------------------------------------------------
def get_template_path(brief_type: str, assets_dir: Path) -> Path:
    profile = TEMPLATES[brief_type]
    return assets_dir / profile.template_file

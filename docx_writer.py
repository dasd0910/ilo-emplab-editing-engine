"""
docx_writer.py — write a .docx with Word tracked changes AND comments
explaining each change.

OOXML has no native python-docx support for either feature, so we do two things:
  1. Insert <w:ins>/<w:del> XML elements for tracked changes.
  2. Insert <w:commentRangeStart>/<w:commentRangeEnd>/<w:commentReference>
     around each change, and post-process the saved .docx zip to add the
     `word/comments.xml` part plus its relationship and content-type entries.

Design trade-off: we operate at the paragraph level. For each paragraph that
contains edits, we clear existing runs and rebuild with plain / del / ins /
comment-range wrappings. In-paragraph inline formatting (bold/italic on a
specific word) is lost — acceptable for ILO briefs where paragraph style
dominates. Refine in a later phase if needed.
"""

from __future__ import annotations

import io
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List
from xml.sax.saxutils import escape as xml_escape

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from rules_engine import Edit


# -----------------------------------------------------------------------------
# Revision / comment id generators
# -----------------------------------------------------------------------------
_REV = 1000

def _next_rev() -> int:
    global _REV
    _REV += 1
    return _REV


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CommentRecord:
    id: int
    author: str
    initials: str
    date: str
    text: str


# -----------------------------------------------------------------------------
# OOXML helpers — run builders
# -----------------------------------------------------------------------------
def _make_run(text: str, rpr: OxmlElement | None = None) -> OxmlElement:
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(deepcopy(rpr))
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def _make_del_run(text: str, rpr: OxmlElement | None = None) -> OxmlElement:
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(deepcopy(rpr))
    t = OxmlElement("w:delText")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def _wrap(element_name: str, child: OxmlElement, rev_id: int, author: str) -> OxmlElement:
    """Build <w:ins> or <w:del> wrapper."""
    w = OxmlElement(element_name)
    w.set(qn("w:id"), str(rev_id))
    w.set(qn("w:author"), author)
    w.set(qn("w:date"), _now_iso())
    w.append(child)
    return w


def _comment_range_start(cid: int) -> OxmlElement:
    e = OxmlElement("w:commentRangeStart")
    e.set(qn("w:id"), str(cid))
    return e


def _comment_range_end(cid: int) -> OxmlElement:
    e = OxmlElement("w:commentRangeEnd")
    e.set(qn("w:id"), str(cid))
    return e


def _comment_reference_run(cid: int) -> OxmlElement:
    """Run containing <w:commentReference w:id="cid"/>."""
    r = OxmlElement("w:r")
    ref = OxmlElement("w:commentReference")
    ref.set(qn("w:id"), str(cid))
    r.append(ref)
    return r


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------
def apply_tracked_changes_with_comments(
    doc: Document,
    edits: List[Edit],
    author: str = "ILO Editing Engine",
    initials: str = "IEE",
) -> tuple[bytes, Dict[str, int]]:
    """
    Apply edits as Word tracked changes, each wrapped with a comment explaining
    the rule that fired. Returns (output_bytes, stats).

    Only 'auto' severity edits are applied (and commented). 'suggest' edits are
    counted but not inserted in v1 — they could become comment-only annotations
    in a later phase.
    """
    stats = {"applied": 0, "suggested_skipped": 0}
    comments: List[CommentRecord] = []

    # Bucket edits by paragraph
    by_para: Dict[int, List[Edit]] = {}
    for e in edits:
        if e.severity != "auto":
            stats["suggested_skipped"] += 1
            continue
        by_para.setdefault(e.paragraph_index, []).append(e)

    for p_idx, para_edits in by_para.items():
        if p_idx >= len(doc.paragraphs):
            continue
        para = doc.paragraphs[p_idx]
        text = para.text
        if not text:
            continue

        para_edits.sort(key=lambda e: e.start)

        # Capture first-run formatting so we can reapply it
        first_rpr = None
        runs = para._p.findall(qn("w:r"))
        if runs:
            rpr = runs[0].find(qn("w:rPr"))
            if rpr is not None:
                first_rpr = rpr

        # Strip existing inline content
        for child in list(para._p):
            if child.tag in (qn("w:r"), qn("w:ins"), qn("w:del"), qn("w:hyperlink")):
                para._p.remove(child)

        cursor = 0
        for e in para_edits:
            # Allocate a comment id for this edit
            cid = _next_rev()
            comments.append(CommentRecord(
                id=cid,
                author=author,
                initials=initials,
                date=_now_iso(),
                text=f"[{e.rule_id} · ILO §{e.manual_ref}] {e.description}",
            ))

            # 1. untouched text before the edit
            if e.start > cursor:
                para._p.append(_make_run(text[cursor:e.start], first_rpr))

            # 2. comment range start
            para._p.append(_comment_range_start(cid))

            # 3. deleted original (wrapped in <w:del>)
            if e.original:
                del_r = _make_del_run(e.original, first_rpr)
                para._p.append(_wrap("w:del", del_r, _next_rev(), author))

            # 4. inserted replacement (wrapped in <w:ins>)
            if e.replacement:
                ins_r = _make_run(e.replacement, first_rpr)
                para._p.append(_wrap("w:ins", ins_r, _next_rev(), author))

            # 5. comment range end + reference
            para._p.append(_comment_range_end(cid))
            para._p.append(_comment_reference_run(cid))

            cursor = e.end
            stats["applied"] += 1

        # Trailing text
        if cursor < len(text):
            para._p.append(_make_run(text[cursor:], first_rpr))

    _enable_track_changes(doc)

    # Save to buffer
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    # Post-process the zip to inject comments.xml + its rel + content type
    final_bytes = _inject_comments_part(buf.getvalue(), comments)
    return final_bytes, stats


# -----------------------------------------------------------------------------
# Track-changes flag in settings.xml
# -----------------------------------------------------------------------------
def _enable_track_changes(doc: Document) -> None:
    settings = doc.settings.element
    for child in settings.findall(qn("w:trackChanges")):
        settings.remove(child)
    tc = OxmlElement("w:trackChanges")
    settings.append(tc)


# -----------------------------------------------------------------------------
# ZIP post-processing: add word/comments.xml and wire it up
# -----------------------------------------------------------------------------
W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
COMMENT_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
COMMENT_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"


def _build_comments_xml(comments: List[CommentRecord]) -> str:
    parts = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             f'<w:comments {W_NS}>']
    for c in comments:
        parts.append(
            f'<w:comment w:id="{c.id}" w:author="{xml_escape(c.author)}" '
            f'w:date="{c.date}" w:initials="{xml_escape(c.initials)}">'
            f'<w:p><w:r><w:t xml:space="preserve">{xml_escape(c.text)}</w:t></w:r></w:p>'
            f'</w:comment>'
        )
    parts.append('</w:comments>')
    return "".join(parts)


def _inject_comments_part(docx_bytes: bytes, comments: List[CommentRecord]) -> bytes:
    """Post-process the saved .docx zip to add the comments part."""
    if not comments:
        return docx_bytes

    # Read existing zip into memory
    src = zipfile.ZipFile(io.BytesIO(docx_bytes), "r")
    contents: Dict[str, bytes] = {name: src.read(name) for name in src.namelist()}
    src.close()

    # 1) Add word/comments.xml
    contents["word/comments.xml"] = _build_comments_xml(comments).encode("utf-8")

    # 2) Ensure [Content_Types].xml declares the comments type
    ct_name = "[Content_Types].xml"
    if ct_name in contents:
        ct_xml = contents[ct_name].decode("utf-8")
        if COMMENT_CT not in ct_xml:
            override = (f'<Override PartName="/word/comments.xml" '
                        f'ContentType="{COMMENT_CT}"/>')
            ct_xml = ct_xml.replace("</Types>", override + "</Types>")
            contents[ct_name] = ct_xml.encode("utf-8")

    # 3) Add relationship in word/_rels/document.xml.rels
    rels_name = "word/_rels/document.xml.rels"
    if rels_name in contents:
        rels_xml = contents[rels_name].decode("utf-8")
        if "comments.xml" not in rels_xml:
            # Pick a relationship id not already used
            rid = _next_unused_rid(rels_xml)
            rel = (f'<Relationship Id="{rid}" '
                   f'Type="{COMMENT_REL_TYPE}" Target="comments.xml"/>')
            rels_xml = rels_xml.replace("</Relationships>", rel + "</Relationships>")
            contents[rels_name] = rels_xml.encode("utf-8")

    # 4) Rewrite the zip
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in contents.items():
            z.writestr(name, data)
    return out.getvalue()


def _next_unused_rid(rels_xml: str) -> str:
    """Find an unused rIdN for a new relationship."""
    import re
    existing = {int(m) for m in re.findall(r'Id="rId(\d+)"', rels_xml)}
    n = 1
    while n in existing:
        n += 1
    return f"rId{n}"


# -----------------------------------------------------------------------------
# Backward-compatible shim — keeps old import working if someone calls
# apply_tracked_changes() directly. New code should use
# apply_tracked_changes_with_comments().
# -----------------------------------------------------------------------------
def apply_tracked_changes(doc: Document, edits: List[Edit],
                          author: str = "ILO Editing Engine") -> Dict[str, int]:
    """Deprecated shim — prefer apply_tracked_changes_with_comments()."""
    _, stats = apply_tracked_changes_with_comments(doc, edits, author)
    return stats

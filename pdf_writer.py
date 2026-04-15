"""
pdf_writer.py — render a cleaned Document into a print-ready PDF.

Pipeline:
  docx.Document  →  classify paragraphs into blocks (heading/body/bullet)
                 →  render a Jinja2 HTML template
                 →  hand HTML + CSS + font directory to WeasyPrint
                 →  return PDF bytes

Branding goals:
  - ILO orange cover band, ILO blue section heads
  - Noto Sans body, Overpass display headings (fonts in workspace folder)
  - A4, 20mm margins, running page numbers, "ILO Brief" running header
  - Per-brief layout (Policy Brief: 2-column body; Fact Sheet: single column)

This PDF is a "shareable digital preview" of the copy-edited content.
For truly print-perfect output, a designer opens the .idml in InDesign.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional

from docx import Document
from jinja2 import Environment, FileSystemLoader, select_autoescape


# -----------------------------------------------------------------------------
# macOS library preload — MUST run BEFORE `from weasyprint import ...`
# -----------------------------------------------------------------------------
def _preload_macos_homebrew_libs() -> None:
    """On macOS with Homebrew, WeasyPrint's pango/cairo libraries live in
    /opt/homebrew/lib (Apple Silicon) or /usr/local/lib (Intel). Python's
    default loader doesn't search there, and SIP strips DYLD_* env vars.
    We manually preload the dylibs with RTLD_GLOBAL so their symbols become
    available to anything loaded later (i.e. WeasyPrint's cffi bindings).
    We also patch ctypes.util.find_library to check Homebrew paths.
    """
    if sys.platform != "darwin":
        return

    candidates = [Path("/opt/homebrew/lib"), Path("/usr/local/lib")]
    brew_lib = next((p for p in candidates if p.exists()), None)
    if brew_lib is None:
        return

    # 1. Patch find_library so calls like ctypes.util.find_library("gobject-2.0")
    #    can resolve to a Homebrew dylib if the system loader can't find one.
    _orig = ctypes.util.find_library

    def _patched(name: str):
        result = _orig(name)
        if result:
            return result
        for candidate in (f"lib{name}.dylib", f"{name}.dylib",
                          f"lib{name}.0.dylib"):
            p = brew_lib / candidate
            if p.exists():
                return str(p)
        return None

    ctypes.util.find_library = _patched  # type: ignore[assignment]

    # 2. Preload the core libs (dependency order matters for some of them).
    for fname in ("libgobject-2.0.dylib", "libglib-2.0.dylib",
                  "libharfbuzz.dylib", "libfontconfig.1.dylib",
                  "libfreetype.6.dylib", "libpango-1.0.dylib",
                  "libpangoft2-1.0.dylib", "libpangocairo-1.0.dylib",
                  "libcairo.2.dylib"):
        p = brew_lib / fname
        if p.exists():
            try:
                ctypes.CDLL(str(p), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


# Run the preload IMMEDIATELY — must happen before weasyprint is imported anywhere
_preload_macos_homebrew_libs()


# -----------------------------------------------------------------------------
# Block = a classified paragraph
# -----------------------------------------------------------------------------
@dataclass
class Block:
    kind: str    # "h2" | "h3" | "bullet" | "p"
    text: str


# -----------------------------------------------------------------------------
# Classify docx paragraphs into blocks for the HTML renderer
# -----------------------------------------------------------------------------
def _blocks_from_doc(doc: Document) -> List[Block]:
    blocks: List[Block] = []
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        style_name = (p.style.name or "").lower() if p.style else ""
        if "heading 1" in style_name or "heading 2" in style_name or "title" in style_name:
            kind = "h2"
        elif "heading 3" in style_name or "heading 4" in style_name:
            kind = "h3"
        elif "list" in style_name or "bullet" in style_name:
            kind = "bullet"
        else:
            kind = "p"
        blocks.append(Block(kind=kind, text=text))
    return blocks


# -----------------------------------------------------------------------------
# Extract title / subtitle heuristically — first H2-like block becomes title
# -----------------------------------------------------------------------------
def _extract_title(blocks: List[Block]) -> tuple[str, Optional[str], List[Block]]:
    """Extract title + subtitle from the first plausible heading.

    Strategy:
      1. Try the first heading-styled (h2/h3) block.
      2. If no heading exists, try the very first non-empty block if it's
         short enough to be a title (<150 chars) and doesn't start a list.
    """
    title = "ILO Brief"
    subtitle: Optional[str] = None
    rest = blocks[:]

    # Strategy 1: find a heading-styled block
    for i, b in enumerate(rest):
        if b.kind in ("h2", "h3") and len(b.text) < 150:
            title = b.text
            rest = rest[:i] + rest[i + 1:]
            break
    else:
        # Strategy 2: fall back to the first short paragraph (for docs with
        # no heading styles — very common when authors just use Normal).
        for i, b in enumerate(rest):
            if b.text.strip() and len(b.text) < 150 and b.kind != "bullet":
                title = b.text.strip()
                rest = rest[:i] + rest[i + 1:]
                break

    # Optional subtitle: next heading or short para
    if rest:
        first = rest[0]
        if first.kind == "h3" and len(first.text) < 180:
            subtitle = first.text
            rest = rest[1:]
    return title, subtitle, rest


# -----------------------------------------------------------------------------
# Extract "Key points" — a heading named exactly that, followed by bullets
# or short paragraphs until the next heading.
# -----------------------------------------------------------------------------
def _strip_ycb_prefix(title: str) -> str:
    """For YCB briefs, strip 'ILO Youth Country Briefs:' / 'ILO Brief' prefixes
    so the country name stands alone as the title (matches real publication)."""
    t = title.strip()
    # Common prefixes
    for prefix in [
        r'^ILO Youth Country Briefs?\s*[:\-–—]\s*',
        r'^ILO Brief\s*[:\-–—]?\s*',
        r'^Youth Country Briefs?\s*[:\-–—]\s*',
    ]:
        t = re.sub(prefix, '', t, flags=re.IGNORECASE).strip()
    return t if t else title


def _extract_key_points(blocks: List[Block]) -> tuple[List[str], List[Block]]:
    """Pull out the Key points section.

    Handles two common cases:
      1. 'Key Points' styled as a heading (h2/h3), followed by items.
      2. 'Key Points' as a plain paragraph, followed by items, terminated by
         the first paragraph longer than ~300 chars (start of body) OR by
         the next heading.
    Returns (key_points_list, remaining_blocks).
    """
    kp_re = re.compile(r'^\s*key\s*points?\s*[:.]?\s*$', re.IGNORECASE)
    for i, b in enumerate(blocks):
        if kp_re.match(b.text):
            kp: List[str] = []
            j = i + 1
            while j < len(blocks):
                blk = blocks[j]
                if blk.kind in ("h2", "h3"):
                    break
                # Treat a long paragraph (>300 chars) as the start of body
                if blk.kind == "p" and len(blk.text) > 300 and len(kp) >= 2:
                    break
                text = blk.text.strip()
                if text:
                    kp.append(text)
                j += 1
                if len(kp) >= 8:
                    break
            # If we found plausible key points, pluck the section out
            if kp:
                remaining = blocks[:i] + blocks[j:]
                return kp, remaining
    return [], blocks


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------
def build_pdf(cleaned_doc: Document, brief_type: str,
              assets_dir: Path, workspace_dir: Path) -> bytes:
    """
    Render the cleaned document as a PDF.

    Args:
        cleaned_doc: a python-docx Document already copy-edited (plain text,
            no tracked changes).
        brief_type: one of "Generic Brief", "Policy Brief", "Fact Sheet".
        assets_dir: emplab_app/assets/ — where brief.html.j2 and CSS live.
        workspace_dir: the parent project folder — where font .ttf files live
            (used as WeasyPrint's base_url so @font-face src="..." resolves).

    Returns: PDF bytes.
    """
    # Lazy import so the app starts even if WeasyPrint has install issues
    try:
        from weasyprint import HTML, CSS
    except ImportError as e:
        raise RuntimeError(
            "WeasyPrint is not installed. On macOS: `brew install pango` "
            "then `pip install weasyprint`. On Streamlit Cloud, add "
            "pango/harfbuzz to a packages.txt file."
        ) from e
    except OSError as e:
        # Typical when pango system lib is missing
        raise RuntimeError(
            "WeasyPrint can't find its system libraries (pango/cairo). "
            "On macOS: `brew install pango`. Error: " + str(e)
        ) from e

    body_class = {
        "Generic Brief": "generic-brief",
        "Policy Brief": "policy-brief",
        "Fact Sheet": "fact-sheet",
        "Youth Country Brief (MCF)": "ycb",
    }.get(brief_type, "generic-brief")

    series_label = {
        "Generic Brief": "ILO Brief",
        "Policy Brief": "ILO Policy Brief",
        "Fact Sheet": "ILO Fact Sheet",
        "Youth Country Brief (MCF)": "ILO Youth Country Briefs",
    }.get(brief_type, "ILO Brief")

    # Classify and split title / body
    all_blocks = _blocks_from_doc(cleaned_doc)
    title, subtitle, body_blocks = _extract_title(all_blocks)
    # For YCB: the country name is the title; strip any series prefix like
    # "ILO Youth Country Briefs:" or "ILO Brief" — those live in the header band.
    if brief_type == "Youth Country Brief (MCF)":
        title = _strip_ycb_prefix(title)
        # Drop further "ILO Brief"/"ILO Youth Country Briefs" paragraphs that
        # duplicate the header band.
        body_blocks = [b for b in body_blocks
                       if b.text.strip() not in
                       {"ILO Brief", "ILO Youth Country Briefs"}
                       and not re.match(r'^\s*ILO Youth Country Briefs\b', b.text)]
    key_points, body_blocks = _extract_key_points(body_blocks)

    # Render HTML via Jinja2
    env = Environment(
        loader=FileSystemLoader(str(assets_dir)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
    )
    template = env.get_template("brief.html.j2")
    html_str = template.render(
        title=title,
        subtitle=subtitle,
        series_label=series_label,
        brief_type=brief_type,
        body_class=body_class,
        date_label=date.today().strftime("%B %Y"),
        key_points=key_points,
        blocks=[{"kind": b.kind, "text": b.text} for b in body_blocks],
    )

    # base_url points to assets_dir — fonts, logo, and CSS all live there
    # so <img src="ilo_logo.png"> and url("NotoSans-Regular.ttf") resolve
    # to the bundled assets (portable for deployment).
    css_path = assets_dir / "brief_styles.css"
    pdf_bytes = HTML(
        string=html_str, base_url=str(assets_dir)
    ).write_pdf(stylesheets=[CSS(filename=str(css_path))])
    return pdf_bytes

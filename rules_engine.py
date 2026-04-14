"""
rules_engine.py — ILO style rule engine.

Loads `ilo_rules.yaml` and applies rules to text.
Returns a list of `Edit` objects describing every proposed change.

The engine is PURE: it does not modify the Document object; it just produces
an edit list. Applying edits (as Word tracked changes) is the job of
`docx_writer.py`.
"""

from __future__ import annotations

import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------
@dataclass
class Edit:
    """A single proposed edit in one paragraph."""
    paragraph_index: int          # 0-based index into doc.paragraphs
    start: int                    # char offset within the paragraph text
    end: int                      # char offset within the paragraph text
    original: str                 # text being replaced
    replacement: str              # text to insert (may be '' for pure deletion)
    rule_id: str                  # e.g. "SPELL_001_organise"
    description: str              # human-readable reason (for Word comment)
    manual_ref: str               # ILO manual section, e.g. "2.1"
    severity: str = "auto"        # "auto" = apply; "suggest" = flag only


@dataclass
class Rule:
    """Internal representation of a rule from ilo_rules.yaml."""
    id: str
    category: str
    type: str
    description: str
    manual_ref: str = ""
    pattern: Optional[str] = None
    replacement: Optional[str] = None
    flags: List[str] = field(default_factory=list)
    exceptions: List[str] = field(default_factory=list)
    severity: str = "auto"
    _compiled: Optional[re.Pattern] = None


# -----------------------------------------------------------------------------
# Rule loading
# -----------------------------------------------------------------------------
def load_rules(yaml_path: Path) -> List[Rule]:
    """Load rules from YAML and compile regex patterns."""
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    rules: List[Rule] = []
    for r in data.get("rules", []):
        rule = Rule(
            id=r["id"],
            category=r["category"],
            type=r["type"],
            description=r.get("description", ""),
            manual_ref=r.get("manual_ref", ""),
            pattern=r.get("pattern"),
            replacement=r.get("replacement"),
            flags=r.get("flags", []) or [],
            exceptions=r.get("exceptions", []) or [],
            severity=r.get("severity", "auto"),
        )

        # Compile regex for rules that need it
        if rule.type in ("regex_replace", "regex_flag") and rule.pattern:
            flag_bits = 0
            for f in rule.flags:
                if f.upper() == "IGNORECASE":
                    flag_bits |= re.IGNORECASE
                elif f.upper() == "MULTILINE":
                    flag_bits |= re.MULTILINE
            rule._compiled = re.compile(rule.pattern, flag_bits)

        rules.append(rule)
    return rules


# -----------------------------------------------------------------------------
# Exclusion detection — don't touch direct quotes
# -----------------------------------------------------------------------------
_QUOTE_SPAN_RE = re.compile(r'[“"][^”"]{3,}?[”"]')

def _quoted_spans(text: str) -> List[tuple]:
    """Return (start, end) char ranges that are inside direct quotations."""
    return [(m.start(), m.end()) for m in _QUOTE_SPAN_RE.finditer(text)]


def _in_excluded_span(start: int, end: int, spans: List[tuple]) -> bool:
    """True if [start, end) overlaps any excluded span."""
    for (s, e) in spans:
        if start < e and end > s:
            return True
    return False


def _hits_exception(match_text: str, exceptions: List[str]) -> bool:
    """True if the matched text appears inside any exception phrase."""
    for exc in exceptions:
        if exc in match_text:
            return True
    return False


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------
def find_edits(paragraphs: List[str], rules: List[Rule],
               include_suggestions: bool = True) -> List[Edit]:
    """
    Apply every rule to every paragraph and collect proposed edits.

    Args:
        paragraphs: list of paragraph text strings, in document order.
        rules: rules loaded by load_rules().
        include_suggestions: if False, only 'auto'-severity edits are returned.

    Returns:
        Flat list of Edit objects.
    """
    edits: List[Edit] = []

    for p_idx, text in enumerate(paragraphs):
        if not text.strip():
            continue

        # Find spans we must not touch (direct quotations inside this paragraph).
        excluded = _quoted_spans(text)

        for rule in rules:
            if rule.severity == "suggest" and not include_suggestions:
                continue

            if rule.type == "regex_replace" and rule._compiled and rule.replacement is not None:
                for m in rule._compiled.finditer(text):
                    # Skip exceptions
                    if _hits_exception(text[max(0, m.start()-30):m.end()+30], rule.exceptions):
                        continue
                    # Skip quoted regions
                    if _in_excluded_span(m.start(), m.end(), excluded):
                        continue
                    # Compute the replacement with backrefs expanded
                    new_text = m.expand(rule.replacement)
                    if new_text == m.group(0):
                        continue  # no-op replacement
                    edits.append(Edit(
                        paragraph_index=p_idx,
                        start=m.start(),
                        end=m.end(),
                        original=m.group(0),
                        replacement=new_text,
                        rule_id=rule.id,
                        description=rule.description,
                        manual_ref=rule.manual_ref,
                        severity=rule.severity,
                    ))

            elif rule.type == "regex_flag" and rule._compiled:
                for m in rule._compiled.finditer(text):
                    if _in_excluded_span(m.start(), m.end(), excluded):
                        continue
                    edits.append(Edit(
                        paragraph_index=p_idx,
                        start=m.start(),
                        end=m.end(),
                        original=m.group(0),
                        replacement=m.group(0),  # flag-only: no change
                        rule_id=rule.id,
                        description=rule.description,
                        manual_ref=rule.manual_ref,
                        severity="suggest",
                    ))

    # Resolve overlapping edits: if two rules fire on overlapping spans,
    # keep the one that appears first in paragraph order, then by rule order.
    edits = resolve_overlaps(edits)
    return edits


def resolve_overlaps(edits: List[Edit]) -> List[Edit]:
    """Remove overlapping edits within the same paragraph."""
    # Sort by (paragraph, start position, end position)
    edits.sort(key=lambda e: (e.paragraph_index, e.start, e.end))
    result: List[Edit] = []
    last_by_para: dict = {}

    for e in edits:
        prev_end = last_by_para.get(e.paragraph_index, -1)
        if e.start >= prev_end:
            result.append(e)
            last_by_para[e.paragraph_index] = e.end
        # else: overlapping → drop this edit (earlier one wins)
    return result


# -----------------------------------------------------------------------------
# Convenience: summary for UI display
# -----------------------------------------------------------------------------
def summarize_edits(edits: List[Edit]) -> dict:
    """Return simple counts for the Streamlit UI."""
    total = len(edits)
    auto = sum(1 for e in edits if e.severity == "auto")
    suggest = sum(1 for e in edits if e.severity == "suggest")
    by_category: dict = {}
    for e in edits:
        cat = e.rule_id.split("_")[0]
        by_category[cat] = by_category.get(cat, 0) + 1
    return {"total": total, "auto": auto, "suggest": suggest, "by_category": by_category}

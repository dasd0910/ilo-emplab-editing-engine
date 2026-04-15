"""
structural_rules.py — ILO rules that need Python logic, not regex.

These rules track state across a document (e.g., "has this acronym been
expanded yet?") or classify content by structure (e.g., heading vs body
paragraph). Each function returns a list of Edit objects compatible with
rules_engine.Edit.
"""

from __future__ import annotations

import re
from typing import List

from rules_engine import Edit


# Universally-known acronyms that do NOT need expansion (per manual §4.1)
_WELL_KNOWN_ACRONYMS = {
    "ILO", "UN", "EU", "WHO", "HIV", "AIDS", "PhD", "MSc", "DNA", "OECD",
    "NGO", "GDP", "USD", "EUR", "GBP", "CHF", "ISO", "IMF", "MP", "USA",
    "UK", "US", "SDG", "SDGs", "COVID-19", "MoU", "CEO", "CFO", "FAQ",
}

# Initialisms (spelled letter-by-letter) take "the". Acronyms (pronounced as
# words) do NOT take "the". Per manual §4.5.1.
_TAKES_THE = {"ILO", "ITUC", "UN", "EU", "WHO", "IMF", "IOE", "OECD", "ITC",
              "WTO", "BIS", "EU", "UK", "US", "UAE", "FAO"}
_NO_THE = {"UNESCO", "UNICEF", "UNCTAD", "WIPO", "CINTERFOR", "ASEAN",
           "MERCOSUR", "UNHCR", "UNDP"}


# -----------------------------------------------------------------------------
# STRUCT_002 — Acronym first-use expansion tracking
# -----------------------------------------------------------------------------
_ACRONYM_RE = re.compile(r'\b[A-Z]{2,}(?:-\d+)?s?\b')

def check_acronym_first_use(paragraphs: List[str]) -> List[Edit]:
    """Flag acronyms whose first use isn't preceded by an expansion.

    An expansion looks like `<Expanded Words> (ACR)` within ~200 chars before
    the first occurrence, OR the acronym appears inside parentheses right
    after some capitalized words.
    """
    edits: List[Edit] = []
    seen: set = set()

    for p_idx, text in enumerate(paragraphs):
        for m in _ACRONYM_RE.finditer(text):
            acr = m.group(0)
            # Strip plural 's' for checking
            base = acr.rstrip('s') if acr.endswith('s') and acr[:-1].isupper() else acr
            if base in _WELL_KNOWN_ACRONYMS or base in seen:
                continue
            # Look backwards up to 250 chars for "(ACR)" pattern, meaning expansion already happened
            window_start = max(0, m.start() - 250)
            window = text[window_start:m.start()]
            # Pattern 1: "Full Name Here (ACR)" in the preceding window
            expansion_re = re.compile(rf'\b[A-Z][A-Za-z]+(?:\s[A-Za-z-]+)+\s*\({re.escape(base)}\)')
            if expansion_re.search(text[max(0, m.start() - 250):m.end() + 5]):
                seen.add(base)
                continue
            # Pattern 2: acronym inside parentheses immediately preceded by capitalised words
            # e.g. "... Decent Work Country Programme (DWCP) ..."
            if m.start() > 0 and text[m.start() - 1] == '(':
                seen.add(base)
                continue

            # Unexpanded first use — flag
            edits.append(Edit(
                paragraph_index=p_idx,
                start=m.start(),
                end=m.end(),
                original=acr,
                replacement=acr,   # flag-only, no change
                rule_id="STRUCT_002_first_use_expansion",
                description=f"First use of '{acr}' in the document — consider expanding it on first reference per ILO §4.1 (e.g., 'Full Name ({acr})').",
                manual_ref="4.1",
                severity="suggest",
            ))
            seen.add(base)
    return edits


# -----------------------------------------------------------------------------
# STRUCT_003 — "the" before initialisms, not before acronyms
# -----------------------------------------------------------------------------
def check_the_with_initialisms(paragraphs: List[str]) -> List[Edit]:
    """Flag '[initialism] (no the)' and '[the acronym]'."""
    edits: List[Edit] = []

    # Match "word before" + acronym. The "word before" tells us if "the" is present.
    # Pattern: look for " the XYZ" and "XYZ" in context, then decide.
    for p_idx, text in enumerate(paragraphs):
        # A. "XXX" that starts a sentence/clause and should take "the"
        for m in re.finditer(r'(?:^|[.!?]\s+|[,;:]\s+)([A-Z]{2,})\b', text):
            acr = m.group(1)
            if acr in _TAKES_THE:
                # Check the character immediately before to see if it already has "the"
                preceding = text[max(0, m.start() - 5):m.start(1)]
                if 'the ' not in preceding.lower():
                    pos = m.start(1)
                    edits.append(Edit(
                        paragraph_index=p_idx,
                        start=pos, end=pos + len(acr),
                        original=acr,
                        replacement=f"the {acr}",
                        rule_id="STRUCT_003_the_before_initialism",
                        description=f"Initialisms (spelled letter-by-letter) take 'the': 'the {acr}' per ILO §4.5.1.",
                        manual_ref="4.5.1",
                        severity="suggest",
                    ))

        # B. "the UNESCO/UNICEF/..." where no "the" should appear
        for m in re.finditer(r'\bthe\s+(' + '|'.join(_NO_THE) + r')\b', text, re.IGNORECASE):
            edits.append(Edit(
                paragraph_index=p_idx,
                start=m.start(),
                end=m.end(),
                original=m.group(0),
                replacement=m.group(1),
                rule_id="STRUCT_003_no_the_before_acronym",
                description=f"Acronyms pronounced as words do NOT take 'the': drop 'the' before '{m.group(1)}' per ILO §4.5.1.",
                manual_ref="4.5.1",
                severity="suggest",
            ))
    return edits


# -----------------------------------------------------------------------------
# STRUCT_004 — Heading sentence-case enforcement
# -----------------------------------------------------------------------------
# For titles, cover, chapter heads, subtitles: only first word capitalised.
# Per manual §5.2.
_HEADING_WORDS_MIN = 3  # ignore very short headings like "Overview"

def check_heading_sentence_case(doc) -> List[Edit]:
    """Flag headings that use headline-case (every word capitalised) instead of sentence case."""
    edits: List[Edit] = []
    for p_idx, p in enumerate(doc.paragraphs):
        style = (p.style.name or "").lower() if p.style else ""
        if not ("heading" in style or "title" in style):
            continue
        text = (p.text or "").strip()
        if not text or len(text.split()) < _HEADING_WORDS_MIN:
            continue

        words = text.split()
        sig_words = [w for w in words[1:]
                     if len(w) > 3 and w.lower() not in
                     {"and", "the", "for", "with", "from", "into", "that", "this"}]
        if not sig_words:
            continue

        cap_count = sum(1 for w in sig_words if w[0].isupper())
        # If most non-first words are capitalised, it's headline case — suggest sentence case
        if cap_count / len(sig_words) > 0.6:
            # Build sentence-case replacement: lowercase all but first word and proper nouns
            # We can't reliably detect proper nouns, so be conservative: lowercase only
            # words that are clearly not proper nouns (lowercased in a simple dictionary).
            # Heuristic: keep capitalised if the original word is also commonly capitalised
            # as a proper noun (e.g. country names); this isn't perfect. Flag-only.
            edits.append(Edit(
                paragraph_index=p_idx,
                start=0, end=len(text),
                original=text,
                replacement=text,  # flag-only: we won't attempt automatic sentence case
                rule_id="STRUCT_004_heading_sentence_case",
                description=("Headline-style capitalization detected. ILO §5.2: titles, "
                             "subtitles and chapter heads use sentence case (only the first "
                             "word capitalised, plus proper nouns)."),
                manual_ref="5.2",
                severity="suggest",
            ))
    return edits


# -----------------------------------------------------------------------------
# STRUCT_001 — Numbers 1–10 in running text (simplified)
# -----------------------------------------------------------------------------
_NUMERAL_IN_TEXT_RE = re.compile(r'(?<!\d)(?<![\d.])\b([1-9]|10)\b(?!\d)')

# Contexts where numerals are CORRECT (skip these)
_NUMERAL_CONTEXT_OK = re.compile(
    r'(?:\d+\s*(?:%|per cent|percent|°C|°F|mm|cm|kg|km|mg|ml)'
    r'|(?:pages?|figures?|tables?|chapters?|articles?|paragraph)\s*\d'
    r'|\d{2}:\d{2}|\d+\s*[ap]\.?m\.?'
    r'|\$\s*\d|\d+[-–]\d+|\d{4}'
    r')'
)

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}

def check_numbers_one_to_ten(paragraphs: List[str]) -> List[Edit]:
    """Suggest spelling out digits 1-10 in running text (manual §7.1.1)."""
    edits: List[Edit] = []
    for p_idx, text in enumerate(paragraphs):
        # If paragraph contains mixed low+high numbers in same sentence, skip (manual exception)
        all_nums = re.findall(r'\b\d+\b', text)
        has_high = any(n.isdigit() and int(n) > 10 for n in all_nums)
        if has_high and len(all_nums) > 1:
            continue

        for m in _NUMERAL_IN_TEXT_RE.finditer(text):
            # Context check: skip if this is a measurement, page ref, time, etc.
            window = text[max(0, m.start() - 12):min(len(text), m.end() + 12)]
            if _NUMERAL_CONTEXT_OK.search(window):
                continue
            num = int(m.group(1))
            edits.append(Edit(
                paragraph_index=p_idx,
                start=m.start(1), end=m.end(1),
                original=m.group(1),
                replacement=_NUMBER_WORDS[num],
                rule_id="STRUCT_001_number_word",
                description=f"Numbers 1–10 spell out in running text: '{m.group(1)}' → '{_NUMBER_WORDS[num]}' per ILO §7.1.1.",
                manual_ref="7.1.1",
                severity="suggest",   # flag-only — context-dependent
            ))
    return edits


# -----------------------------------------------------------------------------
# STRUCT_005 — 'a' vs 'an' matching pronunciation of initialisms
# -----------------------------------------------------------------------------
# Initialisms starting with a vowel SOUND take "an" (F, H, L, M, N, R, S, X
# when pronounced as letter names — "an MP", "an NGO", "an FAQ").
# Initialisms starting with a consonant SOUND take "a" ("a UN report", "a EU").
_VOWEL_SOUND_LETTERS = set("AEFHILMNORSX")  # letter-name starts with vowel

def check_a_an_match(paragraphs: List[str]) -> List[Edit]:
    """Flag 'a X' / 'an X' mismatches where X is an initialism."""
    edits: List[Edit] = []
    for p_idx, text in enumerate(paragraphs):
        for m in re.finditer(r'\b(a|an)\s+([A-Z]{2,})\b', text):
            article, acr = m.group(1), m.group(2)
            if acr in _WELL_KNOWN_ACRONYMS and acr not in _TAKES_THE:
                continue  # known acronyms pronounced as words — different rule
            first = acr[0]
            needs_an = first in _VOWEL_SOUND_LETTERS
            should_be = "an" if needs_an else "a"
            if article.lower() != should_be:
                edits.append(Edit(
                    paragraph_index=p_idx,
                    start=m.start(1), end=m.start(1) + len(article),
                    original=article,
                    replacement=should_be,
                    rule_id="STRUCT_005_a_an_pronunciation",
                    description=(f"'{should_be} {acr}' — indefinite article matches "
                                 f"pronunciation of initialism (ILO §4.5.1)."),
                    manual_ref="4.5.1",
                    severity="suggest",
                ))
    return edits


# -----------------------------------------------------------------------------
# Public aggregator
# -----------------------------------------------------------------------------
def run_all_structural(doc, paragraphs: List[str]) -> List[Edit]:
    """Run every structural rule; return all Edit objects."""
    edits: List[Edit] = []
    edits.extend(check_acronym_first_use(paragraphs))
    edits.extend(check_the_with_initialisms(paragraphs))
    edits.extend(check_heading_sentence_case(doc))
    edits.extend(check_numbers_one_to_ten(paragraphs))
    edits.extend(check_a_an_match(paragraphs))
    return edits

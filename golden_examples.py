"""
golden_examples.py — curated before/after pairs from real ILO editor edits.

These are injected into the LLM system prompt as few-shot examples. Adding
2-5 short concrete examples dramatically improves the LLM's editing style
vs. abstract rules alone. Pairs chosen for:

  - Unambiguous, non-controversial edits (no style-manual conflicts)
  - Short enough to fit in prompt budget
  - Diverse patterns (typo, clarity, word precision, sentence splitting)

To refresh: run the `scripts/mine_golden_pairs.py` diff analysis
(future work) and pick new high-signal pairs.
"""

GOLDEN_EXAMPLES = [
    # --- Typo correction ---
    {
        "before": "The scheme, wich covers most workers, serves t emphasis the role of the Ministry.",
        "after":  "The scheme, that covers most workers, reinforces the role of the Ministry.",
        "note":   "Fixed typos (wich→that, 'serves t emphasis'→reinforces).",
    },
    # --- Sentence splitting (ILO ABC: Brevity & Clarity) ---
    {
        "before": "Growth averaged 8.5 per cent in 2025, which was the highest level recorded in the decade, driven largely by services.",
        "after":  "Growth averaged 8.5 per cent in 2025. This was the highest level recorded in the decade, driven largely by services.",
        "note":   "Split an overlong sentence at a natural break.",
    },
    # --- Comma for clarity ---
    {
        "before": "At the same time access to finance remained limited for smaller firms.",
        "after":  "At the same time, access to finance remained limited for smaller firms.",
        "note":   "Inserted comma after introductory phrase for readability.",
    },
    # --- Word precision ---
    {
        "before": "Unemployment remains very high, defined as those not in work for six months.",
        "after":  "Unemployment is substantial, identified as those not in work for six months.",
        "note":   "'very high'→'substantial' (stronger register); 'defined'→'identified' (precise verb).",
    },
    # --- Remove filler ---
    {
        "before": "The reform was actually designed to expand coverage to informal workers.",
        "after":  "The reform was designed to expand coverage to informal workers.",
        "note":   "Removed filler word 'actually' — tightens prose without changing meaning.",
    },
    # --- Missing article ---
    {
        "before": "Prevalence of informal employment remains high among young workers.",
        "after":  "The prevalence of informal employment remains high among young workers.",
        "note":   "Added definite article 'The' for grammatical correctness.",
    },
]


def format_examples_for_prompt() -> str:
    """Return a string block suitable for injection into the system prompt."""
    lines = ["\nEXAMPLES OF THE KIND OF EDITS YOU SHOULD MAKE:\n"]
    for i, ex in enumerate(GOLDEN_EXAMPLES, 1):
        lines.append(f'{i}. BEFORE: "{ex["before"]}"')
        lines.append(f'   AFTER:  "{ex["after"]}"')
        lines.append(f'   NOTE:   {ex["note"]}\n')
    lines.append("These illustrate your editing style: fix typos, split long sentences, ")
    lines.append("add clarifying commas and articles, remove filler, prefer precise verbs. ")
    lines.append("NEVER change numbers, statistics, proper names, or quoted material.\n")
    return "\n".join(lines)

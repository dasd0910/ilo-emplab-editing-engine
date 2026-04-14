"""
llm_editor.py — LLM-powered grammar/flow/tone pass.

Produces Edit objects compatible with rules_engine.Edit so the tracked-changes
writer can handle rule-engine edits and LLM edits uniformly.

Provider strategy:
  - Primary: Google Gemini (free tier)
  - Fallback: Groq (llama-3.3-70b) when Gemini fails or hits quota

Guardrails (hard constraints injected into the prompt):
  - MUST NOT change numbers, dates, percentages, proper nouns, direct quotes,
    citations, footnote markers, or URLs.
  - MUST return valid JSON only — no prose, no markdown.
  - MUST only suggest edits that make the paragraph adhere to ILO house style.
  - Any "edit" whose `original` is not a literal substring of the paragraph
    is DROPPED (post-hoc validation — LLMs sometimes invent near-matches).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional

from rules_engine import Edit


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
GEMINI_MODEL = "gemini-1.5-flash"
GROQ_MODEL = "llama-3.3-70b-versatile"
CEREBRAS_MODEL = "llama3.3-70b"            # Cerebras Cloud, free tier (no hyphen after 'llama')
MISTRAL_MODEL = "mistral-small-latest"     # Mistral La Plateforme, free tier

# Larger chunks = fewer API calls = less quota used (Groq TPD is the bottleneck).
# 8 paragraphs per chunk cuts request count ~3x vs the old value of 3.
CHUNK_SIZE = 8

# Paragraphs shorter than this are skipped
MIN_PARA_CHARS = 40

MAX_RETRIES = 2


# -----------------------------------------------------------------------------
# Prompt
# -----------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an expert ILO (International Labour Organization) copy editor.
Your job is to copy-edit paragraphs from ILO publications to match ILO house style
(6th edition manual) and improve grammar, clarity, and flow.

ILO HOUSE STYLE ESSENTIALS:
- ABC of writing: Accuracy, Brevity, Clarity. Prefer short sentences.
- British spelling but -ize not -ise (OED): organize, recognize, emphasize.
- "per cent" as two words in running text; "%" only in tables.
- Dates: "8 February 2019" (day-month-year, cardinal numbers, no ordinals).
- Numbers 1-10 spelled out in running text (except percentages, money,
  measurements, ages, times, dates, page/figure refs).
- Use en-rule (–) not hyphen for ranges: "2020–25", "pages 3–7".
- No Latin abbreviations in running text: "for example" not "e.g.",
  "that is" not "i.e.", "and so on" not "etc.".
- Initialisms (ILO, UN) take "the"; acronyms (UNESCO, WIPO) do not.
- Member State(s), Decent Work Agenda, Decent Work Country Programme:
  initial capitals.
- Gender-inclusive language; avoid generic "he/him".
- "The Office" (ILO secretariat) vs "the Organization" (legal entity).
- Prefer active voice and concrete subjects.

WHAT YOU MUST NEVER CHANGE:
- Numbers, percentages, years, dates, statistics.
- Proper nouns of people, organizations, countries, programmes.
- Direct quotations (anything in " " or " ").
- Citations, footnote markers (e.g., "Smith 2021"), URLs.
- Acronyms and their expansions.
- Technical terminology specific to labour economics.

OUTPUT FORMAT (strict — valid JSON only):
Return a JSON object of this exact shape:
{
  "edits": [
    {
      "paragraph": 1,
      "original": "exact substring from the paragraph",
      "replacement": "edited text",
      "reason": "one short sentence explaining the edit"
    }
  ]
}

- "edits" is a list — may be empty [] if no edits are needed.
- "paragraph" is 1-indexed into the list of input paragraphs.
- "original" MUST be a character-for-character substring of the paragraph.
- Multiple edits per paragraph are allowed (one object each).
- Return ONLY the JSON object. No markdown, no code fences, no commentary.
"""


USER_TEMPLATE = """Edit the following ILO paragraphs. Return a JSON array of edits as specified.

Paragraphs:
{paragraphs_block}
"""


# -----------------------------------------------------------------------------
# Provider clients
# -----------------------------------------------------------------------------
class LLMError(Exception):
    pass


def _call_gemini(prompt: str, api_key: str) -> str:
    """Call Gemini. Raises LLMError on failure."""
    try:
        import google.generativeai as genai
    except ImportError as e:
        raise LLMError("google-generativeai not installed") from e

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            GEMINI_MODEL,
            system_instruction=SYSTEM_PROMPT,
            generation_config={"response_mime_type": "application/json",
                               "temperature": 0.2},
        )
        resp = model.generate_content(prompt)
        return resp.text or "[]"
    except Exception as e:
        raise LLMError(f"Gemini error: {type(e).__name__}: {e}") from e


def _call_groq(prompt: str, api_key: str) -> str:
    """Call Groq as fallback."""
    try:
        from groq import Groq
    except ImportError as e:
        raise LLMError("groq not installed") from e

    try:
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
            # Groq's json_object mode requires the prompt to include the word "json"
        )
        return resp.choices[0].message.content or "[]"
    except Exception as e:
        raise LLMError(f"Groq error: {type(e).__name__}: {e}") from e


def _call_cerebras(prompt: str, api_key: str) -> str:
    """Call Cerebras Cloud — free tier, very fast. OpenAI-compatible REST."""
    try:
        import requests
    except ImportError as e:
        raise LLMError("requests not installed") from e
    try:
        resp = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": CEREBRAS_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt + "\nReturn JSON."},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or "{}"
    except Exception as e:
        raise LLMError(f"Cerebras error: {type(e).__name__}: {e}") from e


def _call_mistral(prompt: str, api_key: str) -> str:
    """Call Mistral La Plateforme — free tier, generous limits."""
    try:
        import requests
    except ImportError as e:
        raise LLMError("requests not installed") from e
    try:
        resp = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": MISTRAL_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt + "\nReturn JSON."},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or "{}"
    except Exception as e:
        raise LLMError(f"Mistral error: {type(e).__name__}: {e}") from e


# -----------------------------------------------------------------------------
# Main edit function
# -----------------------------------------------------------------------------
def llm_find_edits(paragraphs: List[str],
                   gemini_key: Optional[str] = None,
                   groq_key: Optional[str] = None,
                   cerebras_key: Optional[str] = None,
                   mistral_key: Optional[str] = None,
                   progress_callback=None) -> tuple[List[Edit], dict]:
    """
    Send paragraphs to LLMs in chunks. Fallback chain:
      Gemini  → Groq  → Cerebras  → Mistral
    """
    if not any([gemini_key, groq_key, cerebras_key, mistral_key]):
        raise LLMError(
            "No API keys configured. Set at least one of "
            "GEMINI_API_KEY / GROQ_API_KEY / CEREBRAS_API_KEY / MISTRAL_API_KEY in .env."
        )

    candidates = [(i, t) for i, t in enumerate(paragraphs)
                  if t and len(t.strip()) >= MIN_PARA_CHARS]

    all_edits: List[Edit] = []
    diag = {"chunks_total": 0, "chunks_ok_gemini": 0, "chunks_ok_groq": 0,
            "chunks_ok_cerebras": 0, "chunks_ok_mistral": 0,
            "chunks_failed": 0, "last_error": None}

    total_chunks = (len(candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for chunk_i in range(0, len(candidates), CHUNK_SIZE):
        chunk = candidates[chunk_i:chunk_i + CHUNK_SIZE]
        diag["chunks_total"] += 1
        if progress_callback:
            progress_callback(chunk_i // CHUNK_SIZE + 1, total_chunks)

        # Build a clean numbered paragraphs block. Instruct JSON output via prompt.
        lines = [f"Paragraph {n}:\n{text}\n"
                 for n, (_oi, text) in enumerate(chunk, start=1)]
        prompt = USER_TEMPLATE.format(paragraphs_block="\n".join(lines))

        raw, which_provider, err = _try_providers(
            prompt, gemini_key, groq_key, cerebras_key, mistral_key
        )
        if raw is None:
            diag["chunks_failed"] += 1
            diag["last_error"] = err
            continue

        diag[f"chunks_ok_{which_provider}"] = diag.get(f"chunks_ok_{which_provider}", 0) + 1

        # Robust JSON parsing — accept dict{edits:[...]} or raw array
        try:
            data = _extract_json(raw)
        except Exception as e:
            diag["chunks_failed"] += 1
            diag["last_error"] = f"JSON parse: {e}"
            continue

        edits_data = _find_edit_list(data)
        if not edits_data:
            continue

        for item in edits_data:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("paragraph", 0))
                original = str(item.get("original", "") or "")
                replacement = str(item.get("replacement", "") or "")
                reason = str(item.get("reason", "LLM grammar/flow edit") or
                             "LLM grammar/flow edit")
            except (TypeError, ValueError):
                continue
            if n < 1 or n > len(chunk) or not original:
                continue

            orig_para_idx = chunk[n - 1][0]
            para_text = paragraphs[orig_para_idx]
            pos = para_text.find(original)
            if pos < 0 or original == replacement:
                continue
            if _changes_numbers(original, replacement):
                continue

            all_edits.append(Edit(
                paragraph_index=orig_para_idx,
                start=pos,
                end=pos + len(original),
                original=original,
                replacement=replacement,
                rule_id="LLM_grammar_flow",
                description=reason,
                manual_ref="LLM",
                severity="auto",
            ))

    return all_edits, diag


def _find_edit_list(data) -> list:
    """Return a list of edit dicts from any plausibly-shaped LLM response."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Try common keys the LLM might use
        for key in ("edits", "changes", "corrections", "results", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        # Any list value at top level with dict items that look like edits
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) \
                    and "original" in v[0]:
                return v
    return []


def _try_providers(prompt: str,
                   gemini_key: Optional[str], groq_key: Optional[str],
                   cerebras_key: Optional[str], mistral_key: Optional[str]
                   ) -> tuple[Optional[str], str, Optional[str]]:
    """Try providers in order; return (text, provider_used, last_error_str)."""
    last_err = None
    providers = [
        ("gemini",   gemini_key,   _call_gemini),
        ("groq",     groq_key,     _call_groq),
        ("cerebras", cerebras_key, _call_cerebras),
        ("mistral",  mistral_key,  _call_mistral),
    ]
    for name, key, fn in providers:
        if not key:
            continue
        for attempt in range(MAX_RETRIES):
            try:
                return fn(prompt, key), name, None
            except LLMError as e:
                msg = str(e)
                last_err = msg
                # If it's a hard rate-limit (daily quota), skip further retries
                if "429" in msg or "rate_limit" in msg.lower() or "quota" in msg.lower():
                    break
                time.sleep(0.5 + attempt * 0.5)
    return None, "none", last_err


def _extract_json(raw: str):
    """Robust JSON extraction from LLM output.

    Handles:
      - markdown code fences ```json ... ```
      - leading/trailing commentary ("Here are the edits: {...}")
      - trailing garbage after a valid JSON value ("{...} Let me know if ...")
      - multiple concatenated JSON objects (returns the FIRST)

    Strategy: strip fences, locate the first `[` or `{`, then use
    json.JSONDecoder().raw_decode() to parse exactly one JSON value,
    ignoring everything after it.
    """
    s = raw.strip()
    # Strip markdown fences if present
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"```\s*$", "", s).strip()
    # Find the first JSON opener
    best = -1
    for opener in "[{":
        i = s.find(opener)
        if i >= 0 and (best < 0 or i < best):
            best = i
    if best < 0:
        raise ValueError("no JSON opener found in response")
    s = s[best:]
    # raw_decode consumes exactly one JSON value and returns its length —
    # anything after is ignored. This is the key fix for trailing garbage.
    decoder = json.JSONDecoder()
    value, _end = decoder.raw_decode(s)
    return value


_NUMBER_RE = re.compile(r'\d+(?:[.,]\d+)?')

def _changes_numbers(a: str, b: str) -> bool:
    """Refuse edits that alter any number."""
    return _NUMBER_RE.findall(a) != _NUMBER_RE.findall(b)


# -----------------------------------------------------------------------------
# Secrets helper
# -----------------------------------------------------------------------------
def load_api_keys() -> dict:
    """Read keys from environment (populated by python-dotenv or Streamlit secrets)."""
    from dotenv import load_dotenv
    load_dotenv()
    return {
        "gemini":   os.environ.get("GEMINI_API_KEY") or None,
        "groq":     os.environ.get("GROQ_API_KEY") or None,
        "cerebras": os.environ.get("CEREBRAS_API_KEY") or None,
        "mistral":  os.environ.get("MISTRAL_API_KEY") or None,
    }

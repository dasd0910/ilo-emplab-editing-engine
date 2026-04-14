# EMPLAB AI Editing & Proofreading Engine

An automated copy-editing, proofreading, and typesetting engine for ILO
publications. Upload a Word draft, pick a brief type, and get back:

1. **Edited `.docx`** — with Word tracked changes and comments citing the
   ILO house-style rule that fired on each edit.
2. **Typeset `.idml`** — opens in Adobe InDesign already populated using the
   chosen template (Generic Brief / Policy Brief / Fact Sheet).
3. **Print-ready `.pdf`** — ILO-branded A4 preview ready to share.

The pipeline combines:
- A **deterministic rule engine** (77 rules mined from the ILO house style
  manual, 6th ed., Rev 5) for mechanical issues (spelling, punctuation,
  number formats, terminology).
- An **LLM grammar/flow pass** with a four-provider free-tier fallback
  chain: Gemini → Groq → Cerebras → Mistral.

---

## Where this fits in the ILO publishing workflow

Per the ILO intranet, publications pass through copy-editing → review →
approval → layout → proofreading. This tool automates **stage 1
(copy-editing)** and jump-starts **stage 4 (layout)**. A human reviewer still
accepts/rejects tracked changes before approval, and a designer still refines
the IDML output in InDesign. This tool reduces donkey-work, not judgement.

---

## Local development

### Prerequisites
- Python 3.10+
- On macOS, for PDF rendering (WeasyPrint):
  ```bash
  brew install pango
  ```

### Setup (once)
```bash
python3 -m venv .venv
source .venv/bin/activate            # on Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# Edit .env and fill in API keys (free signup — see links in .env.example)
```

### Run
```bash
streamlit run app.py
```
Opens at http://localhost:8501

---

## Deployment (Streamlit Community Cloud)

1. **Push to GitHub** (see DEPLOY.md for commands).
2. On https://share.streamlit.io, "New app" → pick your repo → main file
   `app.py`.
3. **Advanced settings → Secrets**, paste in TOML format:
   ```toml
   GEMINI_API_KEY   = "AIza…"
   GROQ_API_KEY     = "gsk_…"
   CEREBRAS_API_KEY = "csk-…"
   MISTRAL_API_KEY  = "…"
   app_password     = "pick_a_strong_password"
   ```
4. Deploy. The build installs Python deps (`requirements.txt`) plus system
   libraries for PDF rendering (`packages.txt`).
5. Share the URL + password with your colleagues.

---

## Project structure

```
emplab_app/
├── app.py                  # Streamlit UI (entry point)
├── rules_engine.py         # Deterministic ILO-rule engine
├── docx_writer.py          # Word tracked-changes + comments injection
├── llm_editor.py           # Gemini / Groq / Cerebras / Mistral fallback chain
├── idml_writer.py          # InDesign IDML typesetter
├── pdf_writer.py           # WeasyPrint PDF renderer
├── ilo_rules.yaml          # ILO house-style rules (v1 — edit to add more)
├── requirements.txt        # Python dependencies
├── packages.txt            # Debian system packages (for Streamlit Cloud)
├── assets/
│   ├── brief.html.j2       # PDF HTML template
│   ├── brief_styles.css    # ILO-branded CSS
│   ├── ilo_logo.png        # ILO emblem (converted from .ai)
│   └── NotoSans-*.ttf, Overpass-*.ttf   # bundled fonts
└── templates/
    ├── Generic Brief Digital [RGB template].idml
    ├── Policy Brief Digital [RGB template].idml
    └── EN_ILO_Fact_Sheet_A4_Portrait_CMYK_.idml
```

---

## Updating the ILO style rules

All deterministic rules live in `ilo_rules.yaml`. To add a new rule:
1. Copy an existing rule block.
2. Assign a unique `id` (e.g. `SPELL_017_neighbour`).
3. Update `pattern`, `replacement`, `description`, `manual_ref`.
4. Restart the app — rules load at startup.

The LLM's editing behaviour is shaped by the system prompt in
`llm_editor.py` (constant `SYSTEM_PROMPT`).

---

## Phase status
- [x] **Phase 1** — Skeleton Streamlit app
- [x] **Phase 2** — ILO rule engine + Word tracked changes + comments
- [x] **Phase 3** — LLM grammar/flow pass (four free providers)
- [x] **Phase 4** — IDML typesetting
- [x] **Phase 5** — Print-ready PDF with ILO branding
- [x] **Phase 6** — Deployment

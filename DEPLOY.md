# Deployment steps — EMPLAB Editing Engine

Walk through these **in order**, from within the `emplab_app/` folder, in the
VS Code terminal. Stop at any step that errors and paste the error to your
assistant before continuing.

---

## 1. Install GitHub CLI (one-time)
The GitHub CLI makes authentication painless — no Personal Access Token
fiddling.
```bash
brew install gh
```

---

## 2. Authenticate with GitHub
```bash
gh auth login
```
Choose:
- **GitHub.com**
- **HTTPS**
- **Yes**, authenticate Git with your GitHub credentials
- **Login with a web browser** — it prints a one-time code and opens a
  browser tab. Paste the code, approve.

When done, verify:
```bash
gh auth status
```
Should say "Logged in to github.com as <your-handle>".

---

## 3. Initialise git locally
From inside `emplab_app/`:
```bash
git init
git branch -M main
git add .
git status       # sanity check — should NOT include .env or .venv
git commit -m "Initial commit — EMPLAB editing engine v1"
```

If `.env` or `.venv` appears in `git status`, STOP. The `.gitignore` is
missing something. Shouldn't happen if you didn't edit `.gitignore`.

---

## 4. Create the GitHub repo and push
```bash
gh repo create ilo-emplab-editing-engine --public --source=. --remote=origin --push
```
This creates the repo on GitHub, wires the local remote, and pushes `main`
in one command. When it finishes, it prints the repo URL — open it in your
browser to confirm the code is there.

---

## 5. Deploy to Streamlit Community Cloud
1. Go to https://share.streamlit.io and sign in with GitHub.
2. Click **New app**.
3. Select:
   - **Repository:** `<your-handle>/ilo-emplab-editing-engine`
   - **Branch:** `main`
   - **Main file path:** `app.py`
4. Click **Advanced settings** → **Secrets** and paste in TOML format
   (replace the placeholders with your real keys and chosen password):
   ```toml
   GEMINI_API_KEY   = "AIza..."
   GROQ_API_KEY     = "gsk_..."
   CEREBRAS_API_KEY = "csk-..."
   MISTRAL_API_KEY  = "..."
   app_password     = "a-strong-password-you-pick"
   ```
5. Click **Deploy**. First build takes 3–5 minutes (installs Python +
   Debian packages for WeasyPrint). Watch the log pane; on success the app
   URL becomes live.

---

## 6. Share with colleagues
The URL will look like `https://<your-handle>-ilo-emplab-editing-engine-app-xxxxxx.streamlit.app`.

Share it with **both** the URL and the password (send the password through a
secure channel — Teams/Signal/etc., not email or a shared doc).

---

## Making later changes
Any time you edit a file in `emplab_app/`:
```bash
git add .
git commit -m "Describe the change in one line"
git push
```
Streamlit Cloud auto-redeploys within ~60 seconds of each push.

---

## Rotating API keys / password
Streamlit Cloud → your app → **⚙ Settings** → **Secrets** → edit values →
**Save**. App restarts automatically with the new values. No git commit needed.

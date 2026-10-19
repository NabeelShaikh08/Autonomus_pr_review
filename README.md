# Autonomous PR Reviewer

A self-hosted GitHub App that automatically reviews every pull request using AI. When a PR is opened or updated, it fetches the diff, analyses the code, and posts inline comments with a summary — no manual steps required.

---

## How it works

1. A PR is opened or pushed to on an installed repository
2. GitHub sends a webhook to this server
3. The server authenticates, fetches the diff, and filters out irrelevant files
4. An AI model analyses the code and calls structured tools to flag issues
5. Review comments and a summary are posted directly on the PR

---

## What it reviews

The bot focuses only on real code issues:

- Bugs, incorrect logic, broken control flow
- Security vulnerabilities and exposed secrets
- Data integrity issues and race conditions
- Performance problems (N+1 queries, unbounded loops)
- Missing error handling at system boundaries

It ignores documentation, markdown, config files, lock files, generated code, and build artifacts.

---

## Self-hosting guide

### 1. Create a GitHub App

1. Go to **github.com/settings/apps/new**
2. Fill in:
   - **App name**: anything unique (e.g. `my-pr-reviewer`)
   - **Homepage URL**: your repo URL
   - **Webhook URL**: your server URL + `/webhook` (update this after deployment)
   - **Webhook secret**: generate a strong random string — save it
3. Under **Repository permissions**:
   - `Pull requests` → **Read & write**
   - `Contents` → **Read-only**
4. Under **Subscribe to events**: check **Pull request**
5. Click **Create GitHub App**
6. Note your **App ID** from the settings page
7. Scroll down → **Generate a private key** → download the `.pem` file

### 2. Get a Groq API key

Sign up at **console.groq.com** and create a free API key.

### 3. Configure environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | Description |
|---|---|
| `GITHUB_APP_ID` | App ID from step 1 |
| `GITHUB_PRIVATE_KEY` | Full contents of the `.pem` file |
| `GITHUB_WEBHOOK_SECRET` | The secret you set in step 1 |
| `GROQ_API_KEY` | Your Groq API key |

Optional tuning:

| Variable | Default | Description |
|---|---|---|
| `MAX_FILES_PER_REVIEW` | `50` | Max files reviewed per PR |
| `MAX_PATCH_CHARS` | `80000` | Truncate diff beyond this size |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### 4. Deploy to Railway (recommended)

1. Push this repo to GitHub
2. Go to **railway.app** → New Project → Deploy from GitHub repo
3. Select your repo → Deploy Now
4. Go to **Settings → Networking → Generate Domain** (port `8000`)
5. Go to **Variables** and add all four required env vars
   - For `GITHUB_PRIVATE_KEY`: paste the full multiline `.pem` contents directly
6. Copy your Railway domain and update the **Webhook URL** in your GitHub App settings to `https://your-domain.up.railway.app/webhook`

### 5. Install the app on a repository

1. GitHub App settings → **Install App**
2. Choose the repositories you want reviewed
3. Open a PR — the bot reviews it automatically

---

## Running locally (for development)

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
uvicorn app.main:app --reload --port 8000

# Expose it to the internet (in another terminal)
ngrok http 8000
# Paste the ngrok URL into your GitHub App webhook settings
```

Generate a webhook secret:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Verify your config loaded correctly:
```bash
python -c "from app.config import get_settings; s = get_settings(); print(s.is_configured())"
```

## Running with Docker

```bash
docker build -t pr-reviewer .
docker run --env-file .env -p 8000:8000 pr-reviewer
```

---

## Running tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

---

## Project structure

```
├── app/
│   ├── main.py       # FastAPI app, webhook endpoint, orchestration
│   ├── auth.py       # GitHub App JWT + cached installation tokens
│   ├── github.py     # GitHub API: fetch files, post review
│   ├── agent.py      # Groq agentic loop with tool use
│   ├── filters.py    # Skip vendor/generated/binary/config files
│   └── config.py     # Environment variable configuration
├── tests/
│   ├── test_filters.py
│   └── test_webhook.py
├── Dockerfile
├── .env.example
└── requirements.txt
```

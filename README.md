# PR Reviewer

An autonomous pull request code reviewer powered by Claude. Install it on any GitHub repo and every PR automatically gets inline review comments — no manual steps required.

---

## How it works

1. You open a PR
2. GitHub sends a webhook to this server
3. The server fetches the diff, filters out irrelevant files, and sends it to Claude
4. Claude analyses the code and posts inline comments + a summary directly on the PR

---

## Setup

### 1. Create a GitHub App

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**
2. Fill in:
   - **App name**: `pr-reviewer` (or anything unique)
   - **Homepage URL**: your repo URL
   - **Webhook URL**: `https://your-server.com/webhook`
   - **Webhook secret**: generate a strong random string and save it
3. Under **Repository permissions**:
   - Pull requests: **Read & write**
   - Contents: **Read-only**
4. Under **Subscribe to events**: check **Pull request**
5. Click **Create GitHub App**
6. On the next page, click **Generate a private key** and download the `.pem` file

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

| Variable | Where to find it |
|---|---|
| `GITHUB_APP_ID` | GitHub App settings page |
| `GITHUB_PRIVATE_KEY` | Contents of the downloaded `.pem` file (replace newlines with `\n`) |
| `GITHUB_WEBHOOK_SECRET` | The secret you chose in step 2 |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys |

### 3. Run locally (for testing)

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
uvicorn app.main:app --reload --port 8000

# In another terminal, expose it to the internet
ngrok http 8000
# Copy the ngrok URL and paste it into your GitHub App webhook URL
```

### 4. Run with Docker

```bash
docker build -t pr-reviewer .
docker run --env-file .env -p 8000:8000 pr-reviewer
```

### 5. Install the app on a repo

1. Go to your GitHub App settings → **Install App**
2. Select the repos you want reviewed
3. Open a PR — the review appears automatically

---

## Deployment

### AWS Lambda (recommended — scales to zero)

```bash
pip install mangum
# Wrap app with Mangum handler, deploy via SAM or Serverless Framework
```

### Fly.io

```bash
fly launch
fly secrets set GITHUB_APP_ID=... GITHUB_PRIVATE_KEY=... GITHUB_WEBHOOK_SECRET=... ANTHROPIC_API_KEY=...
fly deploy
```

### Railway / Render

Push to GitHub, connect the repo, set env vars in the dashboard.

---

## Running tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

---

## Project structure

```
prreview/
├── app/
│   ├── main.py       # FastAPI app, webhook endpoint, orchestration
│   ├── auth.py       # GitHub App JWT + installation token
│   ├── github.py     # GitHub API: fetch files, post review
│   ├── agent.py      # Claude agentic loop with tool use
│   ├── filters.py    # Skip vendor/generated/binary files
│   └── config.py     # Environment variable config
├── tests/
│   ├── test_filters.py
│   └── test_webhook.py
├── Dockerfile
├── .env.example
└── requirements.txt
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MAX_FILES_PER_REVIEW` | `50` | Cap files sent to Claude per PR |
| `MAX_PATCH_CHARS` | `80000` | Truncate diff beyond this character count |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

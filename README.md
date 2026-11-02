# HelpLoop — portfolio-grade real-time customer support platform

HelpLoop is a multi-tenant support chat application built with FastAPI, PostgreSQL, Redis, WebSockets and Celery. The browser client is hosted on **Vercel**; the API and persistent services run on **Render**.

> **Why the split deployment?** Vercel now supports WebSockets in Functions, but this existing backend also runs a continuously listening Redis Pub/Sub hub, Celery workers/beat, and writes uploads to a persistent local disk. Those long-running/background and persistent-storage assumptions are better hosted as conventional services. This deployment keeps the current real-time architecture intact: Vercel serves the frontend, while Render runs the FastAPI API and workers. A future all-Vercel version would need a serverless redesign for background jobs and file storage.

## Features

- Customer and support-agent registration with invite-gated agent accounts
- Company-isolated shared support channels
- Agent lounge, direct messages and channel creation
- JWT authentication and bcrypt password hashing
- Real-time messages, typing indicators, presence and read receipts
- File attachments with authenticated downloads
- Message search using PostgreSQL full-text search
- Offline email notifications and scheduled SLA escalation
- Redis Pub/Sub for multi-instance WebSocket fan-out
- Docker-based backend deployment and health checks

## Deployment overview

```text
Browser
  │
  ├── Vercel: static frontend (public/)
  │
  └── HTTPS + WSS
        │
        ▼
      Render: FastAPI API ───── PostgreSQL
        │
        ├── Redis Pub/Sub
        ├── Celery worker ───── SMTP provider
        ├── Celery Beat (SLA checks)
        └── Persistent disk (/var/data/uploads)
```

## Deploy the backend on Render

1. Push this repository to GitHub.
2. In Render, choose **New → Blueprint** and select the repository. Render reads `render.yaml` and provisions the API, worker, scheduler, PostgreSQL and Redis.
3. The blueprint creates a private PostgreSQL database and Redis instance. The API has a persistent disk for uploaded files.
4. Wait for `helploop-api` to become healthy. Test `https://YOUR-API.onrender.com/health`; it should return `{"status":"ok","service":"helploop-api"}`.
5. Copy the API's public URL.

The blueprint generates `JWT_SECRET` and `AGENT_INVITE`. In Render, open the API service's environment settings and copy the generated `AGENT_INVITE` somewhere safe; you will need it to create the first agent account. Do not put the invite code in public source control.

Set `FRONTEND_URLS` on the API service to your exact Vercel origin, for example `https://helploop-demo.vercel.app` (no trailing slash). After the first Vercel deployment, update this value and redeploy the API. The WebSocket endpoint also checks the browser Origin against this allowlist.

Optional email notifications: after deployment, add `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS=true`, and `MAIL_FROM` to both the worker and beat services in Render. Without SMTP configuration, chat still works and the worker logs notification details instead of sending email. Use an SMTP provider/app password, not a personal account password. If SMTP is not configured, notification messages are logged by the worker.

## Deploy the frontend on Vercel

1. In Vercel, import the same GitHub repository.
2. Set the project root to the repository root. Vercel uses `vercel.json` and publishes the `public/` directory.
3. Before or immediately after the first deployment, edit `public/config.js` and replace the placeholder with your API URL:

```js
window.HELPLOOP_CONFIG = {
  API_BASE: "https://YOUR-API.onrender.com"
};
```

4. Commit and push the change; Vercel redeploys automatically.
5. Set `FRONTEND_URLS` in Render to the exact Vercel production URL and redeploy the API.
6. Open the Vercel URL and create the first support agent using the generated `AGENT_INVITE`. Then create a customer account using a company name.

For Vercel preview deployments, add each preview origin to `FRONTEND_URLS` if you want previews to call the API. Keep the list specific; do not use `*` with credentials enabled.

## Local development

Requirements: Python 3.12+, Docker Desktop.

```bash
docker compose up --build
```

Open `http://localhost:8080`. The local demo agent invite is `agent-demo`; replace it for any shared or public environment. The local Compose stack is for development only.

## Environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection URL; `postgres://` and `postgresql://` are normalized to asyncpg |
| `REDIS_URL` | Redis connection URL |
| `JWT_SECRET` | Unique signing secret (at least 32 characters in production) |
| `AGENT_INVITE` | Invite code required for agent registration (at least 16 characters in production) |
| `FRONTEND_URLS` | Comma-separated exact allowed browser origins |
| `ENVIRONMENT` | Set to `production` to enforce secret checks |
| `UPLOAD_DIR` | Persistent upload directory |
| `SLA_MINUTES` | Customer unanswered-message SLA threshold |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `MAIL_FROM` | SMTP notification configuration |

## Security and operational notes

- Use a unique JWT secret and agent invite in production; never commit secrets.
- Use HTTPS/WSS only in production.
- Back up PostgreSQL and the upload disk. The upload disk is persistent but local to the API service; do not scale API instances horizontally without moving uploads to shared object storage.
- Configure a custom domain and exact CORS/WebSocket origin allowlist if desired.
- `create_all` is retained for this small portfolio project. For schema evolution, add Alembic migrations before changing production models.
- Add a production SMTP provider before relying on email alerts.
- Configure monitoring and database backups in the hosting dashboard.
- The current JWT is valid for 24 hours. A production extension should add refresh-token rotation and account/session revocation.
- File downloads use an authenticated endpoint; the current browser link includes the bearer token in the query string for opening a new tab. For a higher-security deployment, replace this with short-lived signed download URLs.

## Tests

Install development dependencies and run:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

The automated tests cover password hashing, password byte limits and token signing/expiry. Full integration tests require PostgreSQL and Redis.

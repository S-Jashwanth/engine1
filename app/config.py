"""Environment-backed configuration for local and cloud deployments."""
import os


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "postgresql://chat:chat@localhost:5432/chat")
    # Render/Neon commonly provide the libpq URL scheme; async SQLAlchemy needs asyncpg.
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


DATABASE_URL = _database_url()
SYNC_DATABASE_URL = DATABASE_URL.replace("+asyncpg", "+psycopg2")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
JWT_SECRET = os.getenv("JWT_SECRET", "local-development-only-change-this-secret")
AGENT_INVITE = os.getenv("AGENT_INVITE", "agent-demo")
SLA_MINUTES = int(os.getenv("SLA_MINUTES", "15"))
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
MAIL_FROM = os.getenv("MAIL_FROM", "helploop@example.com")
FRONTEND_URLS = [x.strip() for x in os.getenv("FRONTEND_URLS", "http://localhost:8000,http://localhost:8080").split(",") if x.strip()]

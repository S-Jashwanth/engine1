from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from .config import DATABASE_URL


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(10))            # agent | customer
    org: Mapped[str | None] = mapped_column(String(100))     # customer's company


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10))            # channel | support | dm
    org: Mapped[str | None] = mapped_column(String(100))
    sla_alerted_for: Mapped[int] = mapped_column(default=0)  # last message id we escalated


class Member(Base):
    __tablename__ = "members"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    last_read: Mapped[int] = mapped_column(default=0)        # read receipts + unread counts


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, default="")
    file_name: Mapped[str | None] = mapped_column(String(200))
    file_url: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# Serverless-friendly small pool; works for the persistent Render API process too.
engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=0,
    pool_recycle=300,
)
Session = async_sessionmaker(engine, expire_on_commit=False)

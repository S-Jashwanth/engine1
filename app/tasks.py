"""Celery: offline email alerts + SLA escalation for unanswered customers."""
import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import redis
from celery import Celery
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SyncSession
from . import config
from .models import Channel, Member, Message, User

celery = Celery("helploop", broker=config.REDIS_URL)
celery.conf.beat_schedule = {"sla-check": {"task": "app.tasks.sla_check", "schedule": 60.0}}
eng = create_engine(config.SYNC_DATABASE_URL, pool_pre_ping=True)
rc = redis.from_url(config.REDIS_URL, decode_responses=True)
log = logging.getLogger("notify")


def send_mail(to, subject, body):
    if not config.SMTP_HOST:
        log.warning("EMAIL to=%s | %s | %s", to, subject, body)
        return
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = config.MAIL_FROM, to, subject
    m.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as s:
        if config.SMTP_USE_TLS:
            s.starttls()
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(m)


@celery.task(name="app.tasks.notify_offline", autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def notify_offline(message_id: int):
    """Email channel members who are offline (max one email / 5 min / channel)."""
    with SyncSession(eng) as s:
        m = s.get(Message, message_id)
        if m is None:
            log.warning("Skipping notification: message %s no longer exists", message_id)
            return
        ch, sender = s.get(Channel, m.channel_id), s.get(User, m.user_id)
        if ch is None or sender is None:
            log.warning("Skipping notification: related channel or sender is missing")
            return
        ids = set(s.scalars(select(Member.user_id).where(Member.channel_id == ch.id))) - {sender.id}
        for u in s.scalars(select(User).where(User.id.in_(ids))):
            if rc.sismember("online", u.id):
                continue
            if not rc.set(f"notified:{u.id}:{ch.id}", 1, nx=True, ex=300):
                continue
            send_mail(u.email, f"[{ch.name}] new message from {sender.name}", (m.body or m.file_name or "")[:500])


@celery.task(name="app.tasks.sla_check")
def sla_check():
    """If a customer's last message has waited > SLA_MINUTES, escalate to all agents."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.SLA_MINUTES)
    with SyncSession(eng) as s:
        agents = [a.email for a in s.scalars(select(User).where(User.role == "agent"))]
        for ch in s.scalars(select(Channel).where(Channel.kind == "support")):
            last = s.scalar(select(Message).where(Message.channel_id == ch.id).order_by(Message.id.desc()).limit(1))
            if not last or last.id == ch.sla_alerted_for or last.created_at > cutoff:
                continue
            if s.get(User, last.user_id).role != "customer":
                continue
            for e in agents:
                send_mail(e, f"SLA breach: {ch.org} waiting {config.SLA_MINUTES}+ min", (last.body or "")[:300])
            rc.publish(f"chan:{ch.id}", json.dumps({"type": "sla", "channel_id": ch.id, "org": ch.org}))
            ch.sla_alerted_for = last.id
        s.commit()

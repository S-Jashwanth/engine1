import asyncio
import json
import os
import re
import uuid
import hashlib
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from . import config
from .auth import check_pw, current_user, hash_pw, make_token, user_from_token
from .models import Base, Channel, Member, Message, Session, User, engine
from .realtime import go_offline, go_online, hub, publish, r
from .tasks import notify_offline


async def run_hub():
    while True:
        try:
            await hub.listen()
        except Exception:
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_):
    if config.ENVIRONMENT == "production":
        if len(config.JWT_SECRET) < 32 or "development-only" in config.JWT_SECRET:
            raise RuntimeError("Set a unique JWT_SECRET of at least 32 characters")
        if config.AGENT_INVITE == "agent-demo" or len(config.AGENT_INVITE) < 16:
            raise RuntimeError("Set a unique AGENT_INVITE of at least 16 characters")
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    async with engine.begin() as c:   # advisory lock: safe when several instances boot together
        await c.execute(text("select pg_advisory_xact_lock(42)"))
        await c.run_sync(Base.metadata.create_all)
        await c.execute(text("insert into channels(name,kind,sla_alerted_for) select 'agents-lounge','channel',0 "
                             "where not exists (select 1 from channels where name='agents-lounge')"))
    task = asyncio.create_task(run_hub())
    yield
    task.cancel()


app = FastAPI(title="HelpLoop", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.FRONTEND_URLS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
async def health():
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))
    await r.ping()
    return {"status": "ok", "service": "helploop-api"}


# ---------- helpers ----------
async def enforce_rate_limit(key: str, limit: int, window_seconds: int):
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, window_seconds)
    if count > limit:
        raise HTTPException(429, "Too many attempts. Please wait and try again.")


def user_out(u):
    return {"id": u.id, "name": u.name, "role": u.role, "org": u.org}


def msg_out(m, name):
    return {"type": "message", "id": m.id, "channel_id": m.channel_id, "user_id": m.user_id, "name": name,
            "body": m.body, "file_name": m.file_name, "file_url": m.file_url, "at": m.created_at.isoformat()}


async def add_members(s, cid, uids, last=0):
    if uids:
        await s.execute(insert(Member).values(
            [{"user_id": i, "channel_id": cid, "last_read": last} for i in uids]).on_conflict_do_nothing())


async def mark_read(s, uid, cid, mid):
    st = insert(Member).values(user_id=uid, channel_id=cid, last_read=mid)
    await s.execute(st.on_conflict_do_update(
        index_elements=["user_id", "channel_id"],
        set_={"last_read": func.greatest(Member.__table__.c.last_read, mid)}))


async def accessible(s, u):
    mine = select(Member.channel_id).where(Member.user_id == u.id)
    q = select(Channel)
    # agents see every support/internal channel (+ their own DMs); customers only what they belong to
    q = q.where(or_(Channel.kind != "dm", Channel.id.in_(mine))) if u.role == "agent" else q.where(Channel.id.in_(mine))
    return (await s.scalars(q.order_by(Channel.id))).all()


async def get_channel(s, u, cid):
    ch = await s.get(Channel, cid)
    if not ch:
        raise HTTPException(404, "No such channel")
    if (ch.kind == "dm" or u.role != "agent") and not await s.get(Member, (u.id, cid)):
        raise HTTPException(403, "Not a member")
    return ch


async def agent_current(u=Depends(current_user)):
    if u.role != "agent":
        raise HTTPException(403, "Agents only")
    return u


# ---------- auth ----------
class Reg(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=10, max_length=72)
    role: str = "customer"
    org: str | None = Field(default=None, max_length=100)
    invite: str | None = None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return str(value).strip().lower()

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, value):
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 UTF-8 bytes")
        return value

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value):
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Name must contain at least 2 non-space characters")
        return value


class Login(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return str(value).strip().lower()

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, value):
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 UTF-8 bytes")
        return value


@app.post("/auth/register")
async def register(b: Reg, request: Request):
    ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(f"ratelimit:register:{ip}", 8, 3600)
    notify = []
    async with Session() as s:
        if await s.scalar(select(User.id).where(User.email == b.email)):
            raise HTTPException(409, "Email already registered")
        if b.role == "agent":
            if b.invite != config.AGENT_INVITE:
                raise HTTPException(403, "Invalid agent invite code")
            org = None
        elif b.role == "customer":
            if not (b.org and b.org.strip()):
                raise HTTPException(422, "Company name is required")
            org = b.org.strip()
        else:
            raise HTTPException(422, "Bad role")
        u = User(email=b.email, name=b.name.strip(), password_hash=hash_pw(b.password), role=b.role, org=org)
        s.add(u)
        await s.flush()
        if u.role == "agent":   # staff join every internal/support channel, starting "caught up"
            for cid in (await s.scalars(select(Channel.id).where(Channel.kind != "dm"))).all():
                last = await s.scalar(select(func.coalesce(func.max(Message.id), 0)).where(Message.channel_id == cid))
                await add_members(s, cid, [u.id], last)
        else:                   # each customer company gets its own shared support channel
            slug = re.sub(r"[^a-z0-9]+", "-", org.lower()).strip("-") or "customer"
            ch = await s.scalar(select(Channel).where(Channel.kind == "support", Channel.name == f"support-{slug}", Channel.org == org))
            if not ch:
                ch = Channel(name=f"support-{slug}", kind="support", org=org)
                s.add(ch)
                await s.flush()
                agents = list(await s.scalars(select(User.id).where(User.role == "agent")))
                await add_members(s, ch.id, agents)
                notify = [(a, ch.id) for a in agents]
            await add_members(s, ch.id, [u.id])
        await s.commit()
    for a, cid in notify:
        await publish(f"user:{a}", {"type": "channel_added", "channel": {"id": cid}})
    return {"token": make_token(u.id), "user": user_out(u)}


@app.post("/auth/login")
async def login(b: Login):
    email_key = hashlib.sha256(b.email.encode("utf-8")).hexdigest()
    await enforce_rate_limit(f"ratelimit:login:{email_key}", 10, 900)
    async with Session() as s:
        u = await s.scalar(select(User).where(User.email == b.email))
    if not u or not check_pw(b.password, u.password_hash):
        raise HTTPException(401, "Wrong email or password")
    return {"token": make_token(u.id), "user": user_out(u)}


@app.get("/me")
async def me(u=Depends(current_user)):
    return user_out(u)


# ---------- directory, channels, DMs ----------
@app.get("/users")
async def users(u=Depends(current_user)):
    q = select(User) if u.role == "agent" else select(User).where(User.role == "agent")
    async with Session() as s:
        return [user_out(x) for x in await s.scalars(q.order_by(User.name))]


@app.get("/presence")
async def presence(u=Depends(current_user)):
    return [int(x) for x in await r.smembers("online")]


@app.get("/channels")
async def channels(u=Depends(current_user)):
    async with Session() as s:
        out = []
        for ch in await accessible(s, u):
            m = await s.get(Member, (u.id, ch.id))
            unread = await s.scalar(select(func.count()).select_from(Message).where(
                Message.channel_id == ch.id, Message.id > (m.last_read if m else 0), Message.user_id != u.id))
            name, peer = ch.name, None
            if ch.kind == "dm":
                peer = await s.scalar(select(Member.user_id).where(Member.channel_id == ch.id, Member.user_id != u.id))
                name = (await s.get(User, peer)).name if peer else "?"
            out.append({"id": ch.id, "name": name, "kind": ch.kind, "unread": unread, "peer": peer})
        return out


class NewChannel(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def normalize_channel_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Channel name cannot be blank")
        return value


@app.post("/channels")
async def new_channel(b: NewChannel, u=Depends(agent_current)):
    async with Session() as s:
        ch = Channel(name=re.sub(r"\s+", "-", b.name.strip().lower())[:50] or "channel", kind="channel")
        s.add(ch)
        await s.flush()
        agents = list(await s.scalars(select(User.id).where(User.role == "agent")))
        await add_members(s, ch.id, agents)
        await s.commit()
    for a in agents:
        await publish(f"user:{a}", {"type": "channel_added", "channel": {"id": ch.id}})
    return {"id": ch.id}


class DM(BaseModel):
    user_id: int


@app.post("/channels/dm")
async def dm(b: DM, u=Depends(current_user)):
    async with Session() as s:
        peer = await s.get(User, b.user_id)
        if not peer or peer.id == u.id:
            raise HTTPException(404, "No such user")
        if u.role == "customer" and peer.role != "agent":
            raise HTTPException(403, "Customers can only message support agents")
        a, c = Member.__table__.alias("a"), Member.__table__.alias("c")
        cid = await s.scalar(select(Channel.id).join(a, a.c.channel_id == Channel.id).join(c, c.c.channel_id == Channel.id)
                             .where(Channel.kind == "dm", a.c.user_id == u.id, c.c.user_id == peer.id))
        if cid:
            return {"id": cid}
        ch = Channel(name="dm", kind="dm")
        s.add(ch)
        await s.flush()
        await add_members(s, ch.id, [u.id, peer.id])
        await s.commit()
    for uid in (u.id, peer.id):
        await publish(f"user:{uid}", {"type": "channel_added", "channel": {"id": ch.id}})
    return {"id": ch.id}


@app.get("/channels/{cid}/messages")
async def history(cid: int, before: int = 0, u=Depends(current_user)):
    async with Session() as s:
        await get_channel(s, u, cid)
        q = select(Message, User.name).join(User, User.id == Message.user_id).where(Message.channel_id == cid)
        if before:
            q = q.where(Message.id < before)
        rows = (await s.execute(q.order_by(Message.id.desc()).limit(50))).all()
        reads = (await s.execute(select(Member.user_id, User.name, Member.last_read).join(
            User, User.id == Member.user_id).where(Member.channel_id == cid))).all()
        return {"messages": [msg_out(m, n) for m, n in reversed(rows)],
                "reads": [{"user_id": a, "name": b, "last_read": c} for a, b, c in reads]}


@app.get("/search")
async def search(q: str, u=Depends(current_user)):
    async with Session() as s:
        ids = [c.id for c in await accessible(s, u)]
        match = func.to_tsvector("english", Message.body).op("@@")(func.plainto_tsquery("english", q))
        rows = (await s.execute(select(Message, User.name).join(User, User.id == Message.user_id)
                                .where(Message.channel_id.in_(ids), match).order_by(Message.id.desc()).limit(30))).all()
        return [msg_out(m, n) for m, n in rows]


# ---------- files ----------
@app.post("/files")
async def upload(f: UploadFile = File(...), u=Depends(current_user)):
    await enforce_rate_limit(f"ratelimit:upload:{u.id}", 30, 3600)
    data = await f.read(10_000_001)
    if len(data) > 10_000_000:
        raise HTTPException(413, "Max 10 MB")
    original_name = os.path.basename((f.filename or "upload").replace("\\\\", "/"))[:200]
    ext = os.path.splitext(original_name)[1].lower()
    allowed_extensions = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".csv", ".docx", ".xlsx", ".zip"}
    if ext not in allowed_extensions:
        raise HTTPException(415, "Unsupported file type")
    name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(config.UPLOAD_DIR, name), "wb") as o:
        o.write(data)
    return {"file_url": f"/files/{name}", "file_name": original_name}


@app.get("/files/{name}")
async def get_file(name: str, token: str = ""):
    u = await user_from_token(token)
    if not u:
        raise HTTPException(401, "Invalid or expired token")
    safe_name = os.path.basename(name)
    if safe_name != name or not re.fullmatch(r"[a-f0-9]{32}(?:\.[A-Za-z0-9]{1,10})?", safe_name):
        raise HTTPException(404, "File not found")
    p = os.path.join(config.UPLOAD_DIR, safe_name)
    if not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    async with Session() as s:
        allowed_ids = [c.id for c in await accessible(s, u)]
        shared = await s.scalar(select(Message.id).where(
            Message.file_url == f"/files/{safe_name}",
            Message.channel_id.in_(allowed_ids)
        ).limit(1))
        if not shared:
            raise HTTPException(403, "You do not have access to this file")
    return FileResponse(p, filename=safe_name)


# ---------- websocket ----------
async def handle(u, ev):
    t = ev.get("type")
    try:
        cid = int(ev.get("channel_id"))
    except (TypeError, ValueError):
        return
    async with Session() as s:
        try:
            await get_channel(s, u, cid)
        except HTTPException:
            return
        if t == "message":
            fu = ev.get("file_url")
            if fu:
                candidate = str(fu)
                filename = candidate.removeprefix("/files/")
                if (candidate != f"/files/{filename}"
                        or not re.fullmatch(r"[a-f0-9]{32}(?:\.[A-Za-z0-9]{1,10})?", filename)
                        or not os.path.isfile(os.path.join(config.UPLOAD_DIR, filename))):
                    fu = None
                else:
                    fu = candidate
            body = str(ev.get("body") or "").strip()[:4000]
            if not body and not fu:
                return
            m = Message(channel_id=cid, user_id=u.id, body=body, file_url=fu,
                        file_name=(ev.get("file_name") or "")[:200] or None)
            s.add(m)
            await s.flush()
            await mark_read(s, u.id, cid, m.id)
            await s.commit()
            await publish(f"chan:{cid}", msg_out(m, u.name))
            try:
                await asyncio.to_thread(notify_offline.delay, m.id)
            except Exception:
                pass   # broker down must never block chat
        elif t == "typing":
            await publish(f"chan:{cid}", {"type": "typing", "channel_id": cid, "user_id": u.id, "name": u.name})
        elif t == "read":
            try:
                mid = max(0, int(ev.get("message_id") or 0))
            except (TypeError, ValueError):
                return
            latest = await s.scalar(select(func.coalesce(func.max(Message.id), 0)).where(Message.channel_id == cid))
            mid = min(mid, latest)
            await mark_read(s, u.id, cid, mid)
            await s.commit()
            await publish(f"chan:{cid}", {"type": "read", "channel_id": cid, "user_id": u.id,
                                          "name": u.name, "last_read": mid})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query("")):
    origin = ws.headers.get("origin")
    if origin and origin not in config.FRONTEND_URLS:
        await ws.close(code=4403)
        return
    u = await user_from_token(token)
    if not u:
        await ws.close(code=4401)
        return
    await ws.accept()
    async with Session() as s:
        ids = [c.id for c in await accessible(s, u)]
    hub.add(ws, u.id, ids)
    await go_online(u.id)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                await handle(u, json.loads(raw))
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(ws, u.id)
        await go_offline(u.id)


STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

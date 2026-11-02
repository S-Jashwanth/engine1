"""WebSocket hub. Every API instance keeps only its own sockets; Redis Pub/Sub
fans events out between instances, so any number of servers can run behind a
load balancer with no sticky sessions."""
import asyncio
import json
from fastapi import WebSocket
import redis.asyncio as aioredis
from .config import REDIS_URL

r = aioredis.from_url(REDIS_URL, decode_responses=True)


async def publish(channel: str, payload: dict):
    await r.publish(channel, json.dumps(payload))


class Hub:
    def __init__(self):
        self.rooms: dict[int, set[WebSocket]] = {}      # channel_id -> local sockets
        self.users: dict[int, set[WebSocket]] = {}      # user_id    -> local sockets
        self.sock_rooms: dict[WebSocket, set[int]] = {}

    def add(self, ws, uid, channel_ids):
        self.users.setdefault(uid, set()).add(ws)
        self.sock_rooms[ws] = set()
        for c in channel_ids:
            self.join(ws, c)

    def join(self, ws, cid):
        self.rooms.setdefault(cid, set()).add(ws)
        self.sock_rooms[ws].add(cid)

    def remove(self, ws, uid):
        for c in self.sock_rooms.pop(ws, ()):
            self.rooms.get(c, set()).discard(ws)
        self.users.get(uid, set()).discard(ws)

    async def _send(self, sockets, data):
        for ws in list(sockets):
            try:
                await ws.send_text(data)
            except Exception:
                pass

    async def listen(self):
        ps = r.pubsub()
        try:
            await ps.psubscribe("chan:*", "user:*", "presence")
            async for m in ps.listen():
                if m["type"] != "pmessage":
                    continue
                kind, _, ident = m["channel"].partition(":")
                data = m["data"]
                if kind == "chan":
                    await self._send(self.rooms.get(int(ident), ()), data)
                elif kind == "user":
                    ev = json.loads(data)
                    for ws in list(self.users.get(int(ident), ())):
                        if ev.get("type") == "channel_added" and ws in self.sock_rooms:
                            self.join(ws, int(ev["channel"]["id"]))
                        await self._send([ws], data)
                else:
                    for socks in list(self.users.values()):
                        await self._send(socks, data)
        finally:
            await ps.aclose()


hub = Hub()


async def go_online(uid: int):
    if await r.incr(f"conn:{uid}") == 1:   # first connection across ALL servers
        await r.sadd("online", uid)
        await publish("presence", {"type": "presence", "user_id": uid, "online": True})


async def go_offline(uid: int):
    if await r.decr(f"conn:{uid}") <= 0:   # last connection closed
        await r.delete(f"conn:{uid}")
        await r.srem("online", uid)
        await publish("presence", {"type": "presence", "user_id": uid, "online": False})

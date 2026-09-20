"""In-memory registry of connected desktop agents.

There is no database by design. Every mapping here is reconstructed from the
`hello` frame an agent sends on connect, so a Render restart (or a free-tier
spin-down) costs nothing but a reconnect.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from fastapi import WebSocket

log = logging.getLogger("relay")


@dataclass
class Agent:
    agent_id: str
    ws: WebSocket
    name: str = "PC"
    pair_code: str = ""
    chats: set[int] = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    async def send(self, payload: dict) -> bool:
        try:
            await self.ws.send_json(payload)
            return True
        except Exception as exc:  # socket already torn down
            log.info("send to agent %s failed: %s", self.agent_id, exc)
            return False


class Registry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._lock = asyncio.Lock()

    async def add(self, agent: Agent) -> None:
        async with self._lock:
            old = self._agents.get(agent.agent_id)
            if old is not None and old.ws is not agent.ws:
                # Same machine reconnected before the old socket timed out.
                try:
                    await old.ws.close(code=4000, reason="replaced")
                except Exception:
                    pass
            self._agents[agent.agent_id] = agent
        log.info("agent online: %s (%s) chats=%s", agent.name, agent.agent_id[:8], len(agent.chats))

    async def remove(self, agent_id: str, ws: WebSocket) -> None:
        async with self._lock:
            cur = self._agents.get(agent_id)
            # Guard against a stale socket evicting its own replacement.
            if cur is not None and cur.ws is ws:
                self._agents.pop(agent_id, None)
                log.info("agent offline: %s", agent_id[:8])

    def by_chat(self, chat_id: int) -> Agent | None:
        for agent in self._agents.values():
            if chat_id in agent.chats:
                return agent
        return None

    def by_pair_code(self, code: str) -> Agent | None:
        code = code.strip()
        if not code:
            return None
        for agent in self._agents.values():
            if agent.pair_code and agent.pair_code == code:
                return agent
        return None

    def by_id(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    @property
    def count(self) -> int:
        return len(self._agents)

    def snapshot(self) -> list[dict]:
        return [
            {
                "id": a.agent_id[:8],
                "name": a.name,
                "chats": len(a.chats),
                "uptime_s": int(time.time() - a.connected_at),
            }
            for a in self._agents.values()
        ]

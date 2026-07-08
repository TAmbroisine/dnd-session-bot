"""Couche de persistance SQLite (aiosqlite).

Modèle :
- sessions          : une ligne par « instance » de séance. En cas de report,
                      la même ligne est mise à jour (date décalée) et les champs
                      old_* mémorisent l'ancien sondage pour la veille d'annulation.
- votes             : votes par joueur, par sondage ('cur' = sondage courant,
                      'old' = ancien sondage d'une séance reportée).
- handled_absents   : joueurs ❌ déjà couverts par une consultation refusée
                      (ils ne redéclenchent plus de consultation — point n° 5).
- consultations     : consultations GM par DM, avec deadline persistée
                      (survit à un redémarrage du bot).
"""
import json
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_at         TEXT NOT NULL,
    announced_at         TEXT,
    message_id           INTEGER,
    locked               INTEGER NOT NULL DEFAULT 0,
    dm_reminder_sent     INTEGER NOT NULL DEFAULT 0,
    monday_reminder_sent INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'active',  -- active | played | cancelled
    old_scheduled_at     TEXT,
    old_message_id       INTEGER,
    old_announced_at     TEXT,
    old_absents          TEXT                              -- JSON [user_id, ...]
);
CREATE TABLE IF NOT EXISTS votes (
    session_id INTEGER NOT NULL,
    poll       TEXT    NOT NULL,   -- 'cur' | 'old'
    user_id    INTEGER NOT NULL,
    value      TEXT    NOT NULL,   -- 'yes' | 'no'
    PRIMARY KEY (session_id, poll, user_id)
);
CREATE TABLE IF NOT EXISTS handled_absents (
    session_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    PRIMARY KEY (session_id, user_id)
);
CREATE TABLE IF NOT EXISTS consultations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    deadline      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    -- pending | approved | refused | timeout | cancelled | superseded
    dm_message_id INTEGER
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---------- sessions ----------

    async def get_active_session(self) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM sessions WHERE status='active' ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def create_session(self, scheduled_at: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO sessions (scheduled_at) VALUES (?)", (scheduled_at,)
        )
        await self.conn.commit()
        return cur.lastrowid

    async def update_session(self, session_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), session_id)
        )
        await self.conn.commit()

    async def close_all_active(self, status: str = "cancelled") -> None:
        await self.conn.execute(
            "UPDATE sessions SET status=? WHERE status='active'", (status,)
        )
        await self.conn.commit()

    # ---------- votes ----------

    async def set_vote(self, session_id: int, poll: str, user_id: int, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO votes (session_id, poll, user_id, value) VALUES (?,?,?,?) "
            "ON CONFLICT(session_id, poll, user_id) DO UPDATE SET value=excluded.value",
            (session_id, poll, user_id, value),
        )
        await self.conn.commit()

    async def get_vote(self, session_id: int, poll: str, user_id: int) -> str | None:
        cur = await self.conn.execute(
            "SELECT value FROM votes WHERE session_id=? AND poll=? AND user_id=?",
            (session_id, poll, user_id),
        )
        row = await cur.fetchone()
        return row["value"] if row else None

    async def delete_vote(self, session_id: int, poll: str, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM votes WHERE session_id=? AND poll=? AND user_id=?",
            (session_id, poll, user_id),
        )
        await self.conn.commit()

    async def get_votes(self, session_id: int, poll: str) -> dict[int, str]:
        cur = await self.conn.execute(
            "SELECT user_id, value FROM votes WHERE session_id=? AND poll=?",
            (session_id, poll),
        )
        return {r["user_id"]: r["value"] async for r in cur}

    async def clear_votes(self, session_id: int, poll: str) -> None:
        await self.conn.execute(
            "DELETE FROM votes WHERE session_id=? AND poll=?", (session_id, poll)
        )
        await self.conn.commit()

    async def move_votes_cur_to_old(self, session_id: int) -> None:
        """Report validé : les votes du sondage courant deviennent l'« ancien » sondage."""
        await self.conn.execute(
            "DELETE FROM votes WHERE session_id=? AND poll='old'", (session_id,)
        )
        await self.conn.execute(
            "UPDATE votes SET poll='old' WHERE session_id=? AND poll='cur'", (session_id,)
        )
        await self.conn.commit()

    async def move_votes_old_to_cur(self, session_id: int) -> None:
        """Annulation de report : l'ancien sondage redevient le sondage courant."""
        await self.conn.execute(
            "DELETE FROM votes WHERE session_id=? AND poll='cur'", (session_id,)
        )
        await self.conn.execute(
            "UPDATE votes SET poll='cur' WHERE session_id=? AND poll='old'", (session_id,)
        )
        await self.conn.commit()

    # ---------- absents déjà traités ----------

    async def add_handled(self, session_id: int, user_ids: list[int]) -> None:
        await self.conn.executemany(
            "INSERT OR IGNORE INTO handled_absents (session_id, user_id) VALUES (?,?)",
            [(session_id, u) for u in user_ids],
        )
        await self.conn.commit()

    async def get_handled(self, session_id: int) -> set[int]:
        cur = await self.conn.execute(
            "SELECT user_id FROM handled_absents WHERE session_id=?", (session_id,)
        )
        return {r["user_id"] async for r in cur}

    async def clear_handled(self, session_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM handled_absents WHERE session_id=?", (session_id,)
        )
        await self.conn.commit()

    # ---------- consultations ----------

    async def create_consultation(
        self, session_id: int, created_at: str, deadline: str
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO consultations (session_id, created_at, deadline) VALUES (?,?,?)",
            (session_id, created_at, deadline),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_pending_consultation(self, session_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM consultations WHERE session_id=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_consultation(self, consult_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE consultations SET {cols} WHERE id=?", (*fields.values(), consult_id)
        )
        await self.conn.commit()


def absents_from_json(raw: str | None) -> list[int]:
    return json.loads(raw) if raw else []


def absents_to_json(user_ids: list[int]) -> str:
    return json.dumps(sorted(user_ids))

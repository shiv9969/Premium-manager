import json
import asyncpg
from datetime import datetime, timezone, timedelta
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT NOT NULL DEFAULT '',
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS admins (
    telegram_id BIGINT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('owner','admin')),
    added_by BIGINT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS memberships (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(telegram_id),
    starts_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','expired','revoked')),
    validated_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memberships_user_status_idx ON memberships(user_id, status);
CREATE INDEX IF NOT EXISTS memberships_expiry_idx ON memberships(status, expires_at);
CREATE TABLE IF NOT EXISTS channels (
    chat_id BIGINT PRIMARY KEY,
    title TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    added_by BIGINT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS channel_access (
    user_id BIGINT NOT NULL REFERENCES users(telegram_id),
    chat_id BIGINT NOT NULL REFERENCES channels(chat_id) ON DELETE CASCADE,
    invite_link TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(user_id, chat_id)
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL,
    actor_id BIGINT,
    target_id BIGINT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS audit_logs_created_idx ON audit_logs(created_at DESC);
"""


class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Database":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=30)
        db = cls(pool)
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return db

    async def close(self) -> None:
        await self.pool.close()

    async def bootstrap_owners(self, owner_ids: set[int]) -> None:
        async with self.pool.acquire() as conn:
            for owner_id in owner_ids:
                await conn.execute(
                    """INSERT INTO admins(telegram_id, role) VALUES($1,'owner')
                       ON CONFLICT(telegram_id) DO UPDATE SET role='owner'""",
                    owner_id,
                )

    async def admin_ids(self) -> set[int]:
        rows = await self.pool.fetch("SELECT telegram_id FROM admins")
        return {int(row["telegram_id"]) for row in rows}

    async def list_admins(self):
        return await self.pool.fetch(
            "SELECT telegram_id, role, added_by, added_at FROM admins ORDER BY role, telegram_id"
        )

    async def add_admin(self, telegram_id: int, owner_id: int) -> None:
        await self.pool.execute(
            """INSERT INTO admins(telegram_id, role, added_by) VALUES($1,'admin',$2)
               ON CONFLICT(telegram_id) DO UPDATE SET role='admin', added_by=$2""",
            telegram_id, owner_id,
        )

    async def remove_admin(self, telegram_id: int) -> bool:
        result = await self.pool.execute(
            "DELETE FROM admins WHERE telegram_id=$1 AND role='admin'", telegram_id
        )
        return result.endswith("1")

    async def audit(self, event: str, actor_id: int | None = None,
                    target_id: int | None = None, details: dict[str, Any] | None = None) -> None:
        payload = json.dumps(details or {}, default=str)
        await self.pool.execute(
            "INSERT INTO audit_logs(event, actor_id, target_id, details) VALUES($1,$2,$3,$4::jsonb)",
            event, actor_id, target_id, payload
        )

    async def register_user(self, telegram_id: int, username: str | None, first_name: str) -> bool:
        result = await self.pool.execute(
            """INSERT INTO users(telegram_id, username, first_name)
               VALUES($1,$2,$3) ON CONFLICT(telegram_id) DO NOTHING""",
            telegram_id, username, first_name
        )
        await self.pool.execute(
            "UPDATE users SET username=$2, first_name=$3, last_seen_at=NOW() WHERE telegram_id=$1",
            telegram_id, username, first_name
        )
        return result.endswith("1")

    async def get_user(self, telegram_id: int):
        return await self.pool.fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)

    async def get_membership(self, telegram_id: int):
        return await self.pool.fetchrow(
            """SELECT * FROM memberships WHERE user_id=$1 AND status='active'
               ORDER BY expires_at DESC LIMIT 1""", telegram_id
        )

    async def activate(self, user_id: int, days: int, admin_id: int, reset: bool):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                now = datetime.now(timezone.utc)
                current = await conn.fetchrow(
                    """SELECT * FROM memberships WHERE user_id=$1 AND status='active'
                       ORDER BY expires_at DESC LIMIT 1 FOR UPDATE""", user_id
                )
                if current and current["expires_at"] > now and not reset:
                    starts = current["starts_at"]
                    expiry = current["expires_at"] + timedelta(days=days)
                    await conn.execute(
                        "UPDATE memberships SET expires_at=$2, validated_by=$3, updated_at=NOW() WHERE id=$1",
                        current["id"], expiry, admin_id
                    )
                else:
                    starts = now
                    expiry = now + timedelta(days=days)
                    await conn.execute(
                        "UPDATE memberships SET status='expired', updated_at=NOW() WHERE user_id=$1 AND status='active'",
                        user_id
                    )
                    await conn.execute(
                        """INSERT INTO memberships(user_id,starts_at,expires_at,status,validated_by)
                           VALUES($1,$2,$3,'active',$4)""", user_id, starts, expiry, admin_id
                    )
                return starts, expiry

    async def add_channel(self, chat_id: int, title: str, admin_id: int):
        await self.pool.execute(
            """INSERT INTO channels(chat_id,title,added_by) VALUES($1,$2,$3)
               ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title, enabled=TRUE""",
            chat_id, title, admin_id
        )

    async def remove_channel(self, chat_id: int):
        await self.pool.execute("DELETE FROM channels WHERE chat_id=$1", chat_id)

    async def channels(self):
        return await self.pool.fetch("SELECT * FROM channels WHERE enabled=TRUE ORDER BY title")

    async def save_invite(self, user_id: int, chat_id: int, invite_link: str):
        await self.pool.execute(
            """INSERT INTO channel_access(user_id,chat_id,invite_link,status)
               VALUES($1,$2,$3,'pending')
               ON CONFLICT(user_id,chat_id) DO UPDATE
               SET invite_link=EXCLUDED.invite_link,status='pending',updated_at=NOW()""",
            user_id, chat_id, invite_link
        )

    async def get_access_by_invite(self, chat_id: int, invite_link: str):
        return await self.pool.fetchrow(
            """SELECT ca.*, c.enabled FROM channel_access ca
               JOIN channels c USING(chat_id)
               WHERE ca.chat_id=$1 AND ca.invite_link=$2""", chat_id, invite_link
        )

    async def set_access_status(self, user_id: int, chat_id: int, status: str):
        await self.pool.execute(
            "UPDATE channel_access SET status=$3,updated_at=NOW() WHERE user_id=$1 AND chat_id=$2",
            user_id, chat_id, status
        )

    async def access_for_user(self, user_id: int):
        return await self.pool.fetch("SELECT * FROM channel_access WHERE user_id=$1", user_id)

    async def active_members(self, limit: int = 30):
        return await self.pool.fetch(
            """SELECT u.telegram_id,u.username,m.expires_at FROM users u
               JOIN memberships m ON m.user_id=u.telegram_id
               WHERE m.status='active' AND m.expires_at>NOW()
               ORDER BY m.expires_at LIMIT $1""", limit
        )

    async def due_memberships(self):
        return await self.pool.fetch(
            """SELECT m.*,u.username FROM memberships m JOIN users u ON u.telegram_id=m.user_id
               WHERE m.status='active' AND m.expires_at<=NOW()"""
        )

    async def expired_access(self):
        return await self.pool.fetch(
            """SELECT ca.user_id,ca.chat_id,ca.invite_link,ca.status,m.id AS membership_id
               FROM channel_access ca
               JOIN memberships m ON m.user_id=ca.user_id
               WHERE m.status='expired' AND m.expires_at<=NOW()
                 AND ca.status <> 'removed'
                 AND NOT EXISTS (
                   SELECT 1 FROM memberships active
                   WHERE active.user_id=ca.user_id AND active.status='active'
                     AND active.expires_at>NOW()
                 )
               ORDER BY m.expires_at"""
        )

    async def warning_candidates(self, hours: int):
        return await self.pool.fetch(
            """SELECT m.*,u.username FROM memberships m JOIN users u ON u.telegram_id=m.user_id
               WHERE m.status='active' AND m.expires_at>NOW()
               AND m.expires_at<=NOW()+($1 * INTERVAL '1 hour')
               AND NOT EXISTS (SELECT 1 FROM audit_logs a WHERE a.event='WARNING_SENT'
                 AND a.target_id=m.user_id AND (a.details->>'hours')::int=$1
                 AND (a.details->>'membership_id')::bigint=m.id)""", hours
        )

    async def mark_expired(self, membership_id: int):
        await self.pool.execute(
            "UPDATE memberships SET status='expired',updated_at=NOW() WHERE id=$1 AND status='active'",
            membership_id
        )

    async def latest_logs(self, limit: int = 15):
        return await self.pool.fetch(
            "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT $1", limit
        )

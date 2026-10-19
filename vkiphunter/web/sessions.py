"""Bounded, persistent sessions; only token hashes are written to disk."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)
MAX_SESSIONS = 32


@dataclass
class Session:
    expires: float
    csrf: str


class SessionStore:
    def __init__(self, path: Path, password: str, ttl: int):
        self.path = path
        self.password = password
        self.ttl = ttl
        self.records: OrderedDict[str, Session] = OrderedDict()
        self.salt = secrets.token_hex(16)
        self._lock = asyncio.Lock()

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def password_tag(self) -> str:
        return hmac.new(self.password.encode(), self.salt.encode(), hashlib.sha256).hexdigest()

    def load(self) -> bool:
        """Called after acquiring the process lock. Return whether to rewrite."""
        self.records.clear()
        if not self.path.exists():
            return False
        try:
            raw = json.loads(self.path.read_text('utf-8'))
            salt = raw['salt']
            if raw['version'] != 1 or not isinstance(salt, str) or len(salt) != 32:
                raise ValueError('invalid format')
            self.salt = salt
            if not hmac.compare_digest(raw['password_tag'], self.password_tag()):
                self.salt = secrets.token_hex(16)
                return True  # A password change permanently revokes old sessions.
            now = time.time()
            for item in raw['sessions']:
                digest, expires, csrf = item['digest'], item['expires'], item['csrf']
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(c not in '0123456789abcdef' for c in digest)
                        or not isinstance(expires, (int, float)) or not math.isfinite(expires)
                        or not isinstance(csrf, str) or len(csrf) < 32):
                    raise ValueError('invalid session')
                if expires > now:
                    self.records[digest] = Session(min(expires, now + self.ttl), csrf)
            while len(self.records) > MAX_SESSIONS:
                self.records.popitem(last=False)
            return len(self.records) != len(raw['sessions'])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            log.warning('Сессии не удалось восстановить; потребуется повторный вход')
            self.records.clear()
            self.salt = secrets.token_hex(16)
            return True

    def get(self, token: str) -> Session | None:
        session = self.records.get(self.digest(token)) if token else None
        return session if session and session.expires > time.time() else None

    def _write(self, snapshot):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(8) + '.tmp')
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(snapshot, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    async def save(self):
        async with self._lock:
            now = time.time()
            for digest, session in list(self.records.items()):
                if session.expires <= now:
                    self.records.pop(digest, None)
            snapshot = {
                'version': 1, 'salt': self.salt, 'password_tag': self.password_tag(),
                'sessions': [dict(digest=digest, **asdict(session))
                             for digest, session in self.records.items()],
            }
            await asyncio.to_thread(self._write, snapshot)

    async def create(self, previous: str = '') -> tuple[str, Session]:
        if previous:
            self.records.pop(self.digest(previous), None)
        now = time.time()
        for digest, session in list(self.records.items()):
            if session.expires <= now:
                self.records.pop(digest, None)
        token = secrets.token_urlsafe(32)
        session = Session(now + self.ttl, secrets.token_urlsafe(32))
        self.records[self.digest(token)] = session
        while len(self.records) > MAX_SESSIONS:
            self.records.popitem(last=False)
        await self.save()
        return token, session

    async def renew(self, token: str) -> bool:
        session = self.get(token)
        # Writes and Set-Cookie are needed at most once per hour of activity.
        if session is None or session.expires - time.time() > self.ttl - min(3600, self.ttl / 2):
            return False
        session.expires = time.time() + self.ttl
        self.records.move_to_end(self.digest(token))
        await self.save()
        return True

    async def revoke(self, token: str):
        if token:
            self.records.pop(self.digest(token), None)
        await self.save()

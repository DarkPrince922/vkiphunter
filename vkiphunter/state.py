"""Персистентное состояние: настройки охоты, статистика, найденные порты."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .cidr import DEFAULT_EXCLUDES, DEFAULT_TARGETS

ATTEMPT_HISTORY_LIMIT = 100

#: Как добывается публичный адрес.
MODE_PORT = "port"            # порт прямо во внешней сети (исторический способ)
MODE_FLOATING_IP = "floatingip"  # обычный floating IP

MODE_TITLES = {
    MODE_PORT: "порт во внешней сети",
    MODE_FLOATING_IP: "floating IP",
}

OUTCOME_MATCH = "match"
OUTCOME_REJECT = "reject"
OUTCOME_POOL_EMPTY = "pool_empty"
OUTCOME_ERROR = "error"


@dataclass
class HuntSettings:
    """Параметры цикла охоты. Все правятся через бота."""

    mode: str = MODE_PORT
    network: str = "internet"
    prefix: str = "vk-direct-probe"
    interval: int = 90
    port_timeout: int = 120
    server_timeout: int = 1800
    targets: list[str] = field(default_factory=lambda: list(DEFAULT_TARGETS))
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    report_every: int = 20
    max_attempts: int = 0  # 0 — без ограничения
    volume_size: int = 10
    volume_type: str = ""


@dataclass
class Attempt:
    """Одна итерация цикла."""

    ts: float
    outcome: str
    ip: str | None = None
    port_id: str | None = None
    detail: str = ""


@dataclass
class Stats:
    attempts: int = 0
    rejected: int = 0
    matches: int = 0
    errors: int = 0
    pool_empty: int = 0
    started_at: float | None = None
    last_attempt_at: float | None = None
    seen_ips: dict[str, int] = field(default_factory=dict)

    def register(self, attempt: Attempt) -> None:
        self.attempts += 1
        self.last_attempt_at = attempt.ts
        if attempt.outcome == OUTCOME_MATCH:
            self.matches += 1
        elif attempt.outcome == OUTCOME_REJECT:
            self.rejected += 1
        elif attempt.outcome == OUTCOME_POOL_EMPTY:
            self.pool_empty += 1
        else:
            self.errors += 1
        if attempt.ip:
            self.seen_ips[attempt.ip] = self.seen_ips.get(attempt.ip, 0) + 1


@dataclass
class FoundPort:
    """Ресурс с подходящим адресом. Удалять его нельзя — уйдёт и IP.

    ``port_id`` хранит id ресурса: порта в режиме порта и floating IP
    в режиме floating IP. ``kind`` говорит, что именно это.
    """

    port_id: str
    ip: str
    name: str
    network: str
    matched_cidr: str
    found_at: float = field(default_factory=time.time)
    kind: str = MODE_PORT
    server_id: str | None = None
    server_name: str | None = None

    @property
    def title(self) -> str:
        return "floating IP" if self.kind == MODE_FLOATING_IP else "порт"

    @property
    def title_cap(self) -> str:
        return "Floating IP" if self.kind == MODE_FLOATING_IP else "Порт"


@dataclass
class AppState:
    settings: HuntSettings = field(default_factory=HuntSettings)
    stats: Stats = field(default_factory=Stats)
    attempts: list[Attempt] = field(default_factory=list)
    found: list[FoundPort] = field(default_factory=list)
    subscribers: list[int] = field(default_factory=list)
    live_message: dict[str, int] | None = None

    def add_attempt(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        del self.attempts[:-ATTEMPT_HISTORY_LIMIT]
        self.stats.register(attempt)

    def subscribe(self, chat_id: int) -> None:
        if chat_id not in self.subscribers:
            self.subscribers.append(chat_id)

    def unsubscribe(self, chat_id: int) -> None:
        if chat_id in self.subscribers:
            self.subscribers.remove(chat_id)

    def last_found(self) -> FoundPort | None:
        return self.found[-1] if self.found else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "settings": asdict(self.settings),
            "stats": asdict(self.stats),
            "attempts": [asdict(a) for a in self.attempts],
            "found": [asdict(f) for f in self.found],
            "subscribers": list(self.subscribers),
            "live_message": self.live_message,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppState:
        return cls(
            settings=_build(HuntSettings, raw.get("settings")),
            stats=_build(Stats, raw.get("stats")),
            attempts=[_build(Attempt, a) for a in raw.get("attempts") or []],
            found=[_build(FoundPort, f) for f in raw.get("found") or []],
            subscribers=list(raw.get("subscribers") or []),
            live_message=raw.get("live_message"),
        )


def _build(cls: type, raw: Any) -> Any:
    """Собрать dataclass, игнорируя незнакомые поля из старого файла состояния."""
    if not isinstance(raw, dict):
        return cls()
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in raw.items() if k in known})


class Store:
    """Загрузка/атомарное сохранение состояния на диск."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state = AppState()
        self._lock = asyncio.Lock()

    def load(self) -> AppState:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text("utf-8"))
                self.state = AppState.from_dict(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                backup = self.path.with_suffix(".corrupt.json")
                self.path.replace(backup)
                self.state = AppState()
        return self.state

    def save_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.state.to_dict(), ensure_ascii=False, indent=2), "utf-8"
        )
        os.replace(tmp, self.path)

    async def save(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self.save_sync)

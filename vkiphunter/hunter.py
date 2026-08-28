"""Движок охоты: один порт за итерацию, проверка CIDR, пауза, повтор.

Логика повторяет исторический ``/root/wait-direct-ip.sh``:

    создать порт → получить IP → проверить CIDR
        ├─ не подходит → удалить порт → пауза → повтор
        └─ подходит    → порт сохранить и остановиться

Найденный порт не удаляется никогда и ни при каких ошибках: удаление
порта освобождает адрес, и он уходит обратно в пул.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .cidr import check_ip
from .cloud import CloudBackend, CloudError, PoolExhausted
from .state import (
    OUTCOME_ERROR,
    OUTCOME_MATCH,
    OUTCOME_POOL_EMPTY,
    OUTCOME_REJECT,
    Attempt,
    FoundPort,
    Store,
)

log = logging.getLogger(__name__)


class HunterStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"


class EventKind(str, Enum):
    STARTED = "started"
    ATTEMPT = "attempt"
    MATCH = "match"
    STOPPED = "stopped"
    FATAL = "fatal"


@dataclass(slots=True)
class HunterEvent:
    kind: EventKind
    attempt: Attempt | None = None
    found: FoundPort | None = None
    message: str = ""
    attempt_number: int = 0


Listener = Callable[[HunterEvent], Awaitable[None]]


class Hunter:
    """Фоновый цикл охоты. В один момент времени работает не больше одного."""

    def __init__(self, cloud: CloudBackend, store: Store) -> None:
        self.cloud = cloud
        self.store = store
        self.status = HunterStatus.IDLE
        self.current_attempt = 0
        self.next_attempt_at: float | None = None
        self.last_error: str = ""
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._listeners: list[Listener] = []

    # --- подписка -------------------------------------------------------

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def _emit(self, event: HunterEvent) -> None:
        for listener in list(self._listeners):
            try:
                await listener(event)
            except Exception:  # noqa: BLE001 - уведомления не должны ронять охоту
                log.exception("listener failed on %s", event.kind)

    # --- управление -----------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """Запустить цикл. ``False``, если он уже идёт."""
        if self.running:
            return False
        self._stop.clear()
        self.current_attempt = 0
        self.last_error = ""
        self.status = HunterStatus.RUNNING
        self.store.state.stats.started_at = time.time()
        self._task = asyncio.create_task(self._run(), name="hunter")
        return True

    async def stop(self, wait: bool = True) -> bool:
        """Попросить цикл остановиться после текущей итерации."""
        if not self.running:
            return False
        self.status = HunterStatus.STOPPING
        self._stop.set()
        if wait and self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        return True

    # --- основной цикл --------------------------------------------------

    async def _run(self) -> None:
        settings = self.store.state.settings
        await self._emit(HunterEvent(EventKind.STARTED))
        try:
            while not self._stop.is_set():
                settings = self.store.state.settings
                self.current_attempt += 1

                attempt, found = await self._iteration()
                self.store.state.add_attempt(attempt)
                await self.store.save()
                await self._emit(
                    HunterEvent(
                        EventKind.ATTEMPT,
                        attempt=attempt,
                        attempt_number=self.current_attempt,
                    )
                )

                if found is not None:
                    self.store.state.found.append(found)
                    await self.store.save()
                    await self._emit(
                        HunterEvent(
                            EventKind.MATCH,
                            attempt=attempt,
                            found=found,
                            attempt_number=self.current_attempt,
                        )
                    )
                    break

                limit = settings.max_attempts
                if limit and self.current_attempt >= limit:
                    await self._emit(
                        HunterEvent(
                            EventKind.STOPPED,
                            message=f"достигнут лимит попыток ({limit})",
                        )
                    )
                    break

                if await self._wait(settings.interval):
                    break
        except asyncio.CancelledError:  # pragma: no cover - остановка процесса
            raise
        except Exception as exc:  # noqa: BLE001 - цикл не должен падать молча
            log.exception("hunter crashed")
            self.last_error = str(exc)
            await self._emit(HunterEvent(EventKind.FATAL, message=str(exc)))
        finally:
            self.status = HunterStatus.IDLE
            self.next_attempt_at = None
            self._task = None
            await self._emit(HunterEvent(EventKind.STOPPED))

    async def _wait(self, interval: int) -> bool:
        """Пауза между попытками. ``True``, если за время паузы попросили стоп."""
        self.next_attempt_at = time.time() + interval
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=interval)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.next_attempt_at = None

    async def _iteration(self) -> tuple[Attempt, FoundPort | None]:
        settings = self.store.state.settings
        name = f"{settings.prefix}-{int(time.time())}"
        now = time.time()

        try:
            port = await self.cloud.create_port(
                settings.network, name, settings.port_timeout
            )
        except PoolExhausted as exc:
            return Attempt(now, OUTCOME_POOL_EMPTY, detail=str(exc)), None
        except CloudError as exc:
            # Порт мог всё-таки создаться до тайм-аута — подберём его по имени,
            # иначе он останется висеть и займёт адрес.
            port = await self._recover_port(name)
            if port is None:
                return Attempt(now, OUTCOME_ERROR, detail=str(exc)), None

        if not port.ip:
            await self._safe_delete(port.id, port.name, settings.prefix)
            return (
                Attempt(now, OUTCOME_ERROR, port_id=port.id, detail="порт без IP"),
                None,
            )

        decision = check_ip(port.ip, settings.targets, settings.excludes)

        if decision.accepted:
            found = FoundPort(
                port_id=port.id,
                ip=port.ip,
                name=port.name,
                network=settings.network,
                matched_cidr=decision.matched_target or "",
                found_at=now,
            )
            attempt = Attempt(
                now, OUTCOME_MATCH, ip=port.ip, port_id=port.id, detail=decision.reason
            )
            return attempt, found

        deleted = await self._safe_delete(port.id, port.name, settings.prefix)
        detail = decision.reason if deleted else f"{decision.reason}; порт НЕ удалён"
        return Attempt(now, OUTCOME_REJECT, ip=port.ip, port_id=port.id, detail=detail), None

    async def _recover_port(self, name: str) -> Any | None:
        try:
            return await self.cloud.find_port(name)
        except CloudError:
            return None

    async def _safe_delete(self, port_id: str, name: str, prefix: str) -> bool:
        """Удалять только собственные probe-порты — чужие трогать нельзя."""
        if not name.startswith(prefix):
            log.warning("отказ от удаления постороннего порта %s (%s)", port_id, name)
            return False
        try:
            await self.cloud.delete_port(port_id)
            return True
        except CloudError as exc:
            log.warning("не удалось удалить порт %s: %s", port_id, exc)
            self.last_error = f"порт {port_id} не удалён: {exc}"
            return False

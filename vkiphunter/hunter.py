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
    MODE_FLOATING_IP,
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


@dataclass(slots=True)
class Allocation:
    """Выданный облаком ресурс — порт или floating IP, для цикла это одно."""

    id: str
    ip: str | None
    label: str


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
        label = f"{settings.prefix}-{int(time.time())}"
        now = time.time()

        try:
            allocation = await self._allocate(settings, label)
        except PoolExhausted as exc:
            return Attempt(now, OUTCOME_POOL_EMPTY, detail=str(exc)), None
        except CloudError as exc:
            # Ресурс мог всё-таки создаться до тайм-аута — подберём его по
            # метке, иначе он останется висеть и займёт адрес.
            allocation = await self._recover(settings, label)
            if allocation is None:
                return Attempt(now, OUTCOME_ERROR, detail=str(exc)), None

        if not allocation.ip:
            await self._release(settings, allocation)
            return (
                Attempt(
                    now, OUTCOME_ERROR, port_id=allocation.id, detail="ресурс без IP"
                ),
                None,
            )

        decision = check_ip(allocation.ip, settings.targets, settings.excludes)

        if decision.accepted:
            found = FoundPort(
                port_id=allocation.id,
                ip=allocation.ip,
                name=allocation.label,
                network=settings.network,
                matched_cidr=decision.matched_target or "",
                found_at=now,
                kind=settings.mode,
            )
            attempt = Attempt(
                now,
                OUTCOME_MATCH,
                ip=allocation.ip,
                port_id=allocation.id,
                detail=decision.reason,
            )
            return attempt, found

        released = await self._release(settings, allocation)
        detail = decision.reason if released else f"{decision.reason}; ресурс НЕ удалён"
        return (
            Attempt(
                now,
                OUTCOME_REJECT,
                ip=allocation.ip,
                port_id=allocation.id,
                detail=detail,
            ),
            None,
        )

    # --- работа с ресурсом в зависимости от режима ----------------------

    async def _allocate(self, settings: Any, label: str) -> Allocation:
        if settings.mode == MODE_FLOATING_IP:
            fip = await self.cloud.create_floating_ip(
                settings.network, label, settings.port_timeout
            )
            return Allocation(fip.id, fip.ip, fip.description or label)

        port = await self.cloud.create_port(
            settings.network, label, settings.port_timeout
        )
        return Allocation(port.id, port.ip, port.name)

    async def _recover(self, settings: Any, label: str) -> Allocation | None:
        try:
            if settings.mode == MODE_FLOATING_IP:
                fip = await self.cloud.find_floating_ip(label)
                return Allocation(fip.id, fip.ip, fip.description or label) if fip else None
            port = await self.cloud.find_port(label)
            return Allocation(port.id, port.ip, port.name) if port else None
        except CloudError:
            return None

    async def _release(self, settings: Any, allocation: Allocation) -> bool:
        """Освободить неподходящий адрес.

        Удаляем только своё: метка ресурса должна начинаться с нашего
        префикса. Чужой порт или чужой floating IP не трогаем никогда.
        """
        if not allocation.label.startswith(settings.prefix):
            log.warning(
                "отказ от удаления постороннего ресурса %s (%s)",
                allocation.id,
                allocation.label,
            )
            return False
        try:
            if settings.mode == MODE_FLOATING_IP:
                await self.cloud.delete_floating_ip(allocation.id)
            else:
                await self.cloud.delete_port(allocation.id)
            return True
        except CloudError as exc:
            log.warning("не удалось освободить %s: %s", allocation.id, exc)
            self.last_error = f"ресурс {allocation.id} не удалён: {exc}"
            return False

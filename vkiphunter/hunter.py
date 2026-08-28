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
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .cidr import check_ip
from .cloud import CloudBackend, CloudError, CloudInfo, PoolExhausted, QuotaExceeded
from .state import (
    MODE_FLOATING_IP,
    MODE_TITLES,
    OUTCOME_ERROR,
    OUTCOME_QUOTA,
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


def account_label(info: CloudInfo) -> str:
    """Короткая подпись аккаунта для логов и сообщений."""
    project = info.project_name or info.project_id or "проект?"
    user = info.username or "пользователь?"
    return f"{project} / {user}"


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
        self.last_error: str = ""
        self.account: str = ""
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._found = asyncio.Event()
        self._next_at: dict[int, float] = {}
        self._listeners: list[Listener] = []

    @property
    def next_attempt_at(self) -> float | None:
        """Ближайшая по времени попытка среди всех воркеров."""
        return min(self._next_at.values()) if self._next_at else None

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
        self._found.clear()
        self._next_at.clear()
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
        limit_reached = False
        try:
            settings = self.store.state.settings
            # Аккаунт узнаём до объявления старта, чтобы он попал в сообщение.
            self.account = await self._identify(settings.network)
            await self._emit(HunterEvent(EventKind.STARTED))

            workers = max(1, min(settings.workers, 16))
            log.info(
                "старт охоты: %s · %s · воркеров %d · интервал %d с · сеть %s · "
                "подсеть %s",
                self.account or "аккаунт неизвестен",
                MODE_TITLES.get(settings.mode, settings.mode),
                workers,
                settings.interval,
                settings.network,
                settings.subnet_label or "любая",
            )

            tasks = [
                asyncio.create_task(self._worker(index, workers), name=f"hunter-{index}")
                for index in range(workers)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            limit_reached = any(r is True for r in results)

            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    raise result
        except asyncio.CancelledError:  # pragma: no cover - остановка процесса
            raise
        except Exception as exc:  # noqa: BLE001 - цикл не должен падать молча
            log.exception("охота упала")
            self.last_error = str(exc)
            await self._emit(HunterEvent(EventKind.FATAL, message=str(exc)))
        finally:
            if limit_reached:
                limit = self.store.state.settings.max_attempts
                await self._emit(
                    HunterEvent(
                        EventKind.STOPPED, message=f"достигнут лимит попыток ({limit})"
                    )
                )
            self.status = HunterStatus.IDLE
            self._next_at.clear()
            self._task = None
            log.info("охота остановлена: попыток %d", self.current_attempt)
            await self._emit(HunterEvent(EventKind.STOPPED))

    async def _identify(self, network: str) -> str:
        """Узнать, в каком аккаунте и проекте работаем — для логов и сообщений."""
        try:
            return account_label(await self.cloud.check(network))
        except CloudError as exc:
            log.warning("не удалось определить аккаунт: %s", exc)
            return ""

    async def _worker(self, index: int, total: int) -> bool:
        """Один охотник. Возвращает True, если упёрся в лимит попыток."""
        settings = self.store.state.settings

        # Разводим старты по времени: четыре запроса в одну секунду — это
        # всплеск на API и лишний риск упереться в квоту.
        if index and settings.interval:
            if await self._sleep(settings.interval * index / total, index):
                return False

        while not self._stop.is_set() and not self._found.is_set():
            settings = self.store.state.settings

            self.current_attempt += 1
            number = self.current_attempt
            if settings.max_attempts and number > settings.max_attempts:
                self.current_attempt -= 1
                return True

            attempt, found = await self._iteration()
            # Номер проставляем только в параллельном режиме — при одном
            # воркере метка была бы шумом.
            attempt.worker = index if total > 1 else None
            attempt.account = self.account
            self.store.state.add_attempt(attempt)
            await self.store.save()

            log.info(
                "воркер %d · %s · %s · %s",
                index,
                self.account or "аккаунт?",
                attempt.ip or "без адреса",
                attempt.detail or attempt.outcome,
            )

            await self._emit(
                HunterEvent(EventKind.ATTEMPT, attempt=attempt, attempt_number=number)
            )

            if found is not None:
                self._found.set()
                self.store.state.found.append(found)
                await self.store.save()
                log.info(
                    "воркер %d нашёл целевой адрес %s (%s) в %s",
                    index,
                    found.ip,
                    found.matched_cidr,
                    self.account or "аккаунте",
                )
                await self._emit(
                    HunterEvent(
                        EventKind.MATCH,
                        attempt=attempt,
                        found=found,
                        attempt_number=number,
                    )
                )
                return False

            if await self._sleep(settings.interval, index):
                return False
        return False

    async def _sleep(self, seconds: float, index: int) -> bool:
        """Пауза, прерываемая остановкой или находкой другого воркера."""
        if seconds <= 0:
            return self._stop.is_set() or self._found.is_set()

        self._next_at[index] = time.time() + seconds
        waiters = [
            asyncio.create_task(self._stop.wait()),
            asyncio.create_task(self._found.wait()),
        ]
        try:
            done, pending = await asyncio.wait(
                waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            return bool(done)
        finally:
            self._next_at.pop(index, None)

    async def _iteration(self) -> tuple[Attempt, FoundPort | None]:
        settings = self.store.state.settings
        # Хвост из случайных символов обязателен: несколько воркеров стартуют
        # в одну секунду, и метки не должны совпадать — иначе не отличить,
        # чей это ресурс, при подборе после тайм-аута.
        label = f"{settings.prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        now = time.time()

        try:
            allocation = await self._allocate(settings, label)
        except PoolExhausted as exc:
            return Attempt(now, OUTCOME_POOL_EMPTY, detail=str(exc)), None
        except QuotaExceeded as exc:
            # Ждать бесполезно: пока не освободите выданные адреса, новых
            # не будет. Особенно вероятно при нескольких воркерах.
            return Attempt(now, OUTCOME_QUOTA, detail=str(exc)), None
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
                settings.network, label, settings.port_timeout, settings.subnet_id
            )
            return Allocation(fip.id, fip.ip, fip.description or label)

        port = await self.cloud.create_port(
            settings.network, label, settings.port_timeout, settings.subnet_id
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

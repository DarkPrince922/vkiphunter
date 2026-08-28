"""Трансляция событий охоты в Telegram.

Живое сообщение статуса редактируется на каждой попытке, отдельные
сообщения шлются только на важное: старт, находку, сводку раз в N попыток,
фатальную ошибку и остановку.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from ..hunter import EventKind, Hunter, HunterEvent
from ..state import OUTCOME_ERROR, Store
from . import formatting as fmt
from .keyboards import found_actions, main_menu

log = logging.getLogger(__name__)


class Notifier:
    def __init__(
        self, bot: Bot, store: Store, hunter: Hunter, credentials: Callable[[], Any]
    ) -> None:
        self.bot = bot
        self.store = store
        self.hunter = hunter
        self.credentials = credentials
        self._live: dict[int, int] = {}

    # --- отправка -------------------------------------------------------

    @property
    def chats(self) -> list[int]:
        return list(self.store.state.subscribers)

    async def broadcast(self, text: str, markup=None) -> None:
        for chat_id in self.chats:
            try:
                await self.bot.send_message(chat_id, text, reply_markup=markup)
            except TelegramForbiddenError:
                self.store.state.unsubscribe(chat_id)
            except TelegramBadRequest as exc:
                log.warning("не отправлено в %s: %s", chat_id, exc)

    async def refresh_live(self) -> None:
        """Обновить живое сообщение статуса во всех чатах."""
        text = fmt.status_text(self.store.state, self.hunter, self.credentials())
        markup = main_menu(
            self.hunter.running,
            bool(self.store.state.found),
            self.credentials().configured,
            self.store.state.settings.mode,
        )
        for chat_id in self.chats:
            message_id = self._live.get(chat_id)
            try:
                if message_id is None:
                    message = await self.bot.send_message(
                        chat_id, text, reply_markup=markup
                    )
                    self._live[chat_id] = message.message_id
                else:
                    await self.bot.edit_message_text(
                        text, chat_id=chat_id, message_id=message_id, reply_markup=markup
                    )
            except TelegramForbiddenError:
                self.store.state.unsubscribe(chat_id)
                self._live.pop(chat_id, None)
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    continue
                # сообщение удалили или оно устарело — заведём новое
                self._live.pop(chat_id, None)

    def forget_live(self, chat_id: int | None = None) -> None:
        if chat_id is None:
            self._live.clear()
        else:
            self._live.pop(chat_id, None)

    # --- события охоты --------------------------------------------------

    async def on_event(self, event: HunterEvent) -> None:
        settings = self.store.state.settings

        if event.kind is EventKind.STARTED:
            self.forget_live()
            await self.broadcast("▶️ <b>Охота запущена.</b>")
            await self.refresh_live()
            return

        if event.kind is EventKind.ATTEMPT:
            await self.refresh_live()
            attempt = event.attempt
            number = event.attempt_number
            report_every = max(1, settings.report_every)
            if attempt and attempt.outcome == OUTCOME_ERROR and number == 1:
                await self.broadcast(
                    "⚠️ <b>Ошибка на первой же попытке</b>\n"
                    + fmt.attempt_line(attempt)
                    + "\n\nПроверьте доступ к облаку: /cloud"
                )
            elif number % report_every == 0:
                await self.broadcast(
                    f"🔔 <b>Сводка после {number} попыток</b>\n\n"
                    + fmt.status_text(self.store.state, self.hunter, self.credentials())
                )
            return

        if event.kind is EventKind.MATCH and event.found:
            await self.broadcast(
                fmt.found_block(event.found)
                + "\n\n<i>Проверьте адрес реальным запросом через целевую SIM/APN "
                "с выключенным Wi-Fi — совпадение CIDR это ещё не пропуск.</i>",
                markup=found_actions(self.store.state.settings.mode),
            )
            return

        if event.kind is EventKind.FATAL:
            await self.broadcast(f"💥 <b>Охота упала:</b> {event.message}")
            return

        if event.kind is EventKind.STOPPED:
            if event.message:
                await self.broadcast(f"⏹ <b>Охота остановлена:</b> {event.message}")
            await self.refresh_live()
            self.forget_live()

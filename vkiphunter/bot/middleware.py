"""Доступ к боту только для владельцев."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

log = logging.getLogger(__name__)

DENIED = (
    "⛔️ Доступ запрещён.\n"
    "Ваш Telegram ID: <code>{user_id}</code>\n\n"
    "Добавьте его в ALLOWED_USER_IDS или попросите владельца бота."
)

UNCLAIMED = "Бот ещё не настроен. Отправьте /start, чтобы стать владельцем."

#: Что разрешено, пока владельца нет.
CLAIM_COMMANDS = ("/start", "/help")


class AccessMiddleware(BaseMiddleware):
    """Список владельцев читается на каждом апдейте — он меняется в рантайме."""

    def __init__(self, admins: Callable[[], set[int]]) -> None:
        self._admins = admins

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return None

        admins = self._admins()

        if not admins:
            # Владельца ещё нет: пускаем только заявку на владение.
            text = (event.text or "") if isinstance(event, Message) else ""
            if text.startswith(CLAIM_COMMANDS):
                return await handler(event, data)
            if isinstance(event, Message):
                await event.answer(UNCLAIMED)
            elif isinstance(event, CallbackQuery):
                await event.answer("Бот ещё не настроен — отправьте /start", show_alert=True)
            return None

        if user.id in admins:
            return await handler(event, data)

        log.warning("отклонён доступ: id=%s username=%s", user.id, user.username)
        if isinstance(event, Message):
            await event.answer(DENIED.format(user_id=user.id))
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступ запрещён", show_alert=True)
        return None

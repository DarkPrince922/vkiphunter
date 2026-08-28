"""Доступ к боту только для ID из ALLOWED_USER_IDS."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

log = logging.getLogger(__name__)

DENIED = (
    "⛔️ Доступ запрещён.\n"
    "Ваш Telegram ID: <code>{user_id}</code>\n"
    "Добавьте его в ALLOWED_USER_IDS и перезапустите бота."
)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, admins: set[int]) -> None:
        self.admins = admins

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return None
        if user.id in self.admins:
            return await handler(event, data)

        log.warning("отклонён доступ: id=%s username=%s", user.id, user.username)
        text = DENIED.format(user_id=user.id)
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступ запрещён", show_alert=True)
        return None

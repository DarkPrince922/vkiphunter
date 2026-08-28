"""Мини-мок Telegram API: диспетчер прогоняется целиком, наружу ничего не летит."""

from __future__ import annotations

import datetime as dt
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetMe,
    SendMessage,
    SetMyCommands,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

USER = User(id=7, is_bot=False, first_name="Owner")
STRANGER = User(id=999, is_bot=False, first_name="Stranger")
CHAT = Chat(id=7, type="private")


class MockedSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._message_id = 1000

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout=None):
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            self._message_id += 1
            return Message(
                message_id=self._message_id,
                date=dt.datetime.now(dt.timezone.utc),
                chat=CHAT,
                text=getattr(method, "text", ""),
            )
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="bot", username="test_bot")
        if isinstance(method, (AnswerCallbackQuery, SetMyCommands)):
            return True
        return True

    # --- удобные выборки ------------------------------------------------

    @property
    def texts(self) -> list[str]:
        return [
            m.text
            for m in self.requests
            if isinstance(m, (SendMessage, EditMessageText)) and m.text
        ]

    @property
    def last_text(self) -> str:
        return self.texts[-1] if self.texts else ""

    def clear(self) -> None:
        self.requests.clear()


def make_bot() -> tuple[Bot, MockedSession]:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    session = MockedSession()
    bot = Bot(
        token="42:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    return bot, session


_update_id = 0


def _next_id() -> int:
    global _update_id
    _update_id += 1
    return _update_id


def text_update(text: str, user: User = USER) -> Update:
    return Update(
        update_id=_next_id(),
        message=Message(
            message_id=_next_id(),
            date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=user.id, type="private"),
            from_user=user,
            text=text,
        ),
    )


def callback_update(data: str, user: User = USER) -> Update:
    return Update(
        update_id=_next_id(),
        callback_query=CallbackQuery(
            id=str(_next_id()),
            from_user=user,
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=_next_id(),
                date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=user.id, type="private"),
                from_user=user,
                text="предыдущий экран",
            ),
        ),
    )

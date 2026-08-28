"""Общий контекст приложения, доступный всем хендлерам."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot

from ..cloud import CloudBackend
from ..config import Settings
from ..hunter import Hunter
from ..state import Store


@dataclass
class AppContext:
    settings: Settings
    store: Store
    cloud: CloudBackend
    hunter: Hunter
    bot: Bot

    @property
    def state(self):
        return self.store.state

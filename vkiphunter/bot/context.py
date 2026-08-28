"""Общий контекст приложения, доступный всем хендлерам."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot

from ..cloud import CloudProxy
from ..config import Settings
from ..hunter import Hunter
from ..secrets import CloudCredentials, SecretStore
from ..state import Store


@dataclass
class AppContext:
    settings: Settings
    store: Store
    secrets: SecretStore
    cloud: CloudProxy
    hunter: Hunter
    bot: Bot

    @property
    def state(self):
        return self.store.state

    @property
    def credentials(self) -> CloudCredentials:
        """Действующие доступы: заданные в боте важнее переменных окружения."""
        stored = self.secrets.secrets.cloud
        if stored.configured:
            return stored
        return self.settings.env_credentials()

    @property
    def admins(self) -> set[int]:
        return set(self.secrets.secrets.admins) | self.settings.admins

    def claim_owner(self, user_id: int) -> bool:
        """Первый написавший становится владельцем, пока список пуст."""
        if self.admins:
            return False
        self.secrets.secrets.admins = [user_id]
        self.secrets.save()
        return True

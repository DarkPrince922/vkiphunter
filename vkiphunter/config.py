"""Конфигурация из переменных окружения / .env.

Имена облачных переменных совпадают с OpenRC-файлом VK Cloud, поэтому
достаточно выполнить ``source mcsXXXXXXXXX-openrc.sh`` перед запуском бота —
отдельно переписывать креды в .env не нужно.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram -------------------------------------------------------
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    allowed_user_ids: str = Field(default="", alias="ALLOWED_USER_IDS")

    # --- Хранилище ------------------------------------------------------
    state_file: Path = Field(default=Path("data/state.json"), alias="STATE_FILE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- Режим без облака (демо/отладка) --------------------------------
    simulate: bool = Field(default=False, alias="SIMULATE")

    # --- OpenStack (OpenRC) ---------------------------------------------
    os_auth_url: str = Field(default="", alias="OS_AUTH_URL")
    os_region_name: str = Field(default="RegionOne", alias="OS_REGION_NAME")
    os_interface: str = Field(default="public", alias="OS_INTERFACE")
    os_identity_api_version: str = Field(default="3", alias="OS_IDENTITY_API_VERSION")

    os_username: str = Field(default="", alias="OS_USERNAME")
    os_password: str = Field(default="", alias="OS_PASSWORD")
    os_project_id: str = Field(default="", alias="OS_PROJECT_ID")
    os_project_name: str = Field(default="", alias="OS_PROJECT_NAME")
    os_user_domain_name: str = Field(default="users", alias="OS_USER_DOMAIN_NAME")
    os_user_domain_id: str = Field(default="", alias="OS_USER_DOMAIN_ID")
    os_project_domain_id: str = Field(default="", alias="OS_PROJECT_DOMAIN_ID")
    os_project_domain_name: str = Field(default="", alias="OS_PROJECT_DOMAIN_NAME")

    # Application credentials — предпочтительнее пароля для долгоживущего бота.
    os_application_credential_id: str = Field(
        default="", alias="OS_APPLICATION_CREDENTIAL_ID"
    )
    os_application_credential_secret: str = Field(
        default="", alias="OS_APPLICATION_CREDENTIAL_SECRET"
    )

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def admins(self) -> set[int]:
        """Белый список Telegram-ID, которым разрешено управление ботом."""
        ids: set[int] = set()
        for chunk in self.allowed_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                ids.add(int(chunk))
        return ids

    @property
    def use_application_credential(self) -> bool:
        return bool(
            self.os_application_credential_id
            and self.os_application_credential_secret
        )

    @property
    def cloud_configured(self) -> bool:
        if not self.os_auth_url:
            return False
        if self.use_application_credential:
            return True
        return bool(self.os_username and self.os_password)

    def auth_kwargs(self) -> dict[str, str]:
        """Аргументы для ``openstack.connection.Connection``."""
        common = {
            "auth_url": self.os_auth_url,
            "region_name": self.os_region_name,
            "interface": self.os_interface,
            "identity_api_version": self.os_identity_api_version,
        }
        if self.use_application_credential:
            return {
                **common,
                "auth_type": "v3applicationcredential",
                "application_credential_id": self.os_application_credential_id,
                "application_credential_secret": self.os_application_credential_secret,
            }

        auth: dict[str, str] = {
            **common,
            "auth_type": "v3password",
            "username": self.os_username,
            "password": self.os_password,
        }
        if self.os_project_id:
            auth["project_id"] = self.os_project_id
        if self.os_project_name:
            auth["project_name"] = self.os_project_name
        if self.os_user_domain_id:
            auth["user_domain_id"] = self.os_user_domain_id
        elif self.os_user_domain_name:
            auth["user_domain_name"] = self.os_user_domain_name
        if self.os_project_domain_id:
            auth["project_domain_id"] = self.os_project_domain_id
        elif self.os_project_domain_name:
            auth["project_domain_name"] = self.os_project_domain_name
        return auth

    def describe_problems(self) -> list[str]:
        """Понятный список того, чего не хватает для запуска."""
        problems: list[str] = []
        if not self.bot_token:
            problems.append("BOT_TOKEN не задан")
        elif not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", self.bot_token):
            problems.append(
                "BOT_TOKEN не похож на токен от @BotFather "
                "(ожидается вид 123456789:AA...)"
            )
        if not self.admins:
            problems.append("ALLOWED_USER_IDS пуст — бот был бы открыт для всех")
        if not self.simulate and not self.cloud_configured:
            problems.append(
                "нет доступа к OpenStack: задайте OS_AUTH_URL и "
                "OS_USERNAME/OS_PASSWORD (или application credential), "
                "либо включите SIMULATE=1"
            )
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

"""Конфигурация из переменных окружения / .env.

Ничего из этого не обязательно: токен и доступы к облаку бот умеет
спросить сам и сохранить в ``credentials.json``. Переменные нужны, только
если удобнее задать их заранее — имена совпадают с OpenRC-файлом VK Cloud,
поэтому ``source mcsXXXXXXXXX-openrc.sh`` перед запуском тоже сработает.
Заданное в боте имеет приоритет над окружением.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .secrets import METHOD_APPCRED, METHOD_PASSWORD, METHOD_SIMULATE, CloudCredentials


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- HTTP panel -----------------------------------------------------
    web_host: str = Field(default="127.0.0.1", alias="WEB_HOST")
    web_port: int = Field(default=8080, ge=1, le=65535, alias="WEB_PORT")
    web_password: str = Field(default="", alias="WEB_PASSWORD", repr=False)
    web_secure_cookie: bool = Field(default=False, alias="WEB_SECURE_COOKIE")
    web_session_days: int = Field(default=7, ge=1, le=365, alias="WEB_SESSION_DAYS")

    # --- Telegram -------------------------------------------------------
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    allowed_user_ids: str = Field(default="", alias="ALLOWED_USER_IDS")

    # --- Хранилище ------------------------------------------------------
    state_file: Path = Field(default=Path("data/state.json"), alias="STATE_FILE")
    credentials_file: Path = Field(
        default=Path("data/credentials.json"), alias="CREDENTIALS_FILE"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: Path = Field(default=Path("data/vkiphunter.log"), alias="LOG_FILE")

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
        return self.env_credentials().configured

    def env_credentials(self) -> CloudCredentials:
        """Креды из окружения — запасной вариант, если в боте ничего не задано."""
        if self.simulate:
            return CloudCredentials(method=METHOD_SIMULATE)

        credentials = CloudCredentials(
            auth_url=self.os_auth_url,
            region=self.os_region_name,
            interface=self.os_interface,
            username=self.os_username,
            password=self.os_password,
            project_id=self.os_project_id,
            project_name=self.os_project_name,
            user_domain_name=self.os_user_domain_name or "users",
            app_cred_id=self.os_application_credential_id,
            app_cred_secret=self.os_application_credential_secret,
        )
        credentials.method = (
            METHOD_APPCRED if self.use_application_credential else METHOD_PASSWORD
        )
        return credentials

    def token_problem(self) -> str | None:
        """Проверка формата токена — до создания Bot, чтобы не ловить трейсбек."""
        if not self.bot_token:
            return None
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", self.bot_token):
            return (
                "BOT_TOKEN не похож на токен от @BotFather "
                "(ожидается вид 123456789:AA...)"
            )
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


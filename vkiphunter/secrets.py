"""Секреты, которые бот спрашивает сам и хранит на диске.

Файл пишется с правами 0600 и лежит рядом с состоянием. Пароль от VK Cloud
и токен бота в .env класть не обязательно — всё настраивается в чате.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

METHOD_PASSWORD = "password"
METHOD_APPCRED = "appcred"
METHOD_SIMULATE = "simulate"


@dataclass
class CloudCredentials:
    """Доступ к OpenStack. Пустой ``method`` — облако ещё не подключено."""

    method: str = ""
    auth_url: str = ""
    region: str = "RegionOne"
    interface: str = "public"
    username: str = ""
    password: str = ""
    project_id: str = ""
    project_name: str = ""
    user_domain_name: str = "users"
    app_cred_id: str = ""
    app_cred_secret: str = ""

    @property
    def configured(self) -> bool:
        if self.method == METHOD_SIMULATE:
            return True
        if not self.auth_url:
            return False
        if self.method == METHOD_APPCRED:
            return bool(self.app_cred_id and self.app_cred_secret)
        if self.method == METHOD_PASSWORD:
            return bool(self.username and self.password)
        return False

    @property
    def simulate(self) -> bool:
        return self.method == METHOD_SIMULATE

    def auth_kwargs(self) -> dict[str, str]:
        common = {
            "auth_url": self.auth_url,
            "region_name": self.region,
            "interface": self.interface,
            "identity_api_version": "3",
        }
        if self.method == METHOD_APPCRED:
            return {
                **common,
                "auth_type": "v3applicationcredential",
                "application_credential_id": self.app_cred_id,
                "application_credential_secret": self.app_cred_secret,
            }
        auth = {
            **common,
            "auth_type": "v3password",
            "username": self.username,
            "password": self.password,
            "user_domain_name": self.user_domain_name,
        }
        if self.project_id:
            auth["project_id"] = self.project_id
        if self.project_name:
            auth["project_name"] = self.project_name
        return auth

    def describe(self) -> str:
        """Человекочитаемое описание без утечки пароля."""
        if self.method == METHOD_SIMULATE:
            return "режим симуляции (реальное облако не подключено)"
        if not self.configured:
            return "не подключено"
        where = f"{self.auth_url} · регион {self.region}"
        if self.method == METHOD_APPCRED:
            return f"application credential {_mask(self.app_cred_id)} · {where}"
        project = self.project_id or self.project_name or "проект по умолчанию"
        return f"пароль · пользователь {self.username} · {project} · {where}"


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


@dataclass
class Secrets:
    bot_token: str = ""
    admins: list[int] = field(default_factory=list)
    cloud: CloudCredentials = field(default_factory=CloudCredentials)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_token": self.bot_token,
            "admins": list(self.admins),
            "cloud": asdict(self.cloud),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Secrets:
        cloud_raw = raw.get("cloud") or {}
        known = set(CloudCredentials.__dataclass_fields__)
        cloud = CloudCredentials(
            **{k: v for k, v in cloud_raw.items() if k in known}
        )
        admins = []
        for value in raw.get("admins") or []:
            try:
                admins.append(int(value))
            except (TypeError, ValueError):
                continue
        return cls(bot_token=str(raw.get("bot_token") or ""), admins=admins, cloud=cloud)


class SecretStore:
    """Загрузка и сохранение секретов с правами 0600."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.secrets = Secrets()

    def load(self) -> Secrets:
        if self.path.exists():
            try:
                self.secrets = Secrets.from_dict(json.loads(self.path.read_text("utf-8")))
            except (json.JSONDecodeError, TypeError, ValueError):
                self.path.replace(self.path.with_suffix(".corrupt.json"))
                self.secrets = Secrets()
        return self.secrets

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.secrets.to_dict(), ensure_ascii=False, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

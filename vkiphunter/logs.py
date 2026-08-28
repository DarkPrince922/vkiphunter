"""Логирование: файл с ротацией, уровни и чтение хвоста.

Вынесено из бота отдельно, чтобы и запуск, и команда /debug пользовались
одним кодом без кольцевых импортов.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3

#: Болтливые чужие логгеры. В обычном режиме молчат, в DEBUG показывают
#: запросы и ответы VK Cloud целиком — ради этого DEBUG и включают.
NOISY_LOGGERS = ("openstack", "keystoneauth", "urllib3", "aiogram.event")


def apply_log_level(level: str) -> str:
    """Выставить уровень логов. Возвращает применённое имя уровня."""
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logging.getLogger().setLevel(numeric)

    verbose = numeric <= logging.DEBUG

    # openstacksdk печатает тела запросов только со своим собственным
    # включённым логированием — и сам поднимает уровни своих логгеров,
    # поэтому его зовём до того, как выставим их окончательно.
    try:
        import openstack

        openstack.enable_logging(debug=verbose, http_debug=verbose)
    except Exception:  # noqa: BLE001 - в режиме симуляции SDK может не быть
        pass

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if verbose else logging.WARNING)
    return logging.getLevelName(numeric)


def current_level() -> str:
    return logging.getLevelName(logging.getLogger().level)


def is_verbose() -> bool:
    return logging.getLogger().level <= logging.DEBUG


def setup_logging(level: str, log_file: Path | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    if log_file is not None:
        # Отдельный файл нужен, чтобы /log в боте показывал историю даже
        # там, где вывод процесса никто не собирает.
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(handler)

    apply_log_level(level)


def tail(path: Path | str, lines: int) -> str:
    """Последние строки файла. Читаем с конца, чтобы не тянуть мегабайты."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            chunk = min(size, 256 * 1024)
            handle.seek(size - chunk)
            text = handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"не прочитать лог: {exc}"
    return "\n".join(text.splitlines()[-lines:])

"""Роутеры бота."""

from __future__ import annotations

from aiogram import Router

from . import connect, hunt, menu, ports, settings, vm


def build_router() -> Router:
    """Собрать свежее дерево роутеров — по одному на каждый экземпляр бота."""
    router = Router(name="root")
    for module in (menu, connect, hunt, ports, vm, settings):
        router.include_router(module.build_router())
    return router

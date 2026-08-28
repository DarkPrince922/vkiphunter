"""Слой доступа к облаку."""

from __future__ import annotations

from ..config import Settings
from .base import (
    CloudAuthError,
    CloudBackend,
    CloudError,
    CloudInfo,
    FlavorInfo,
    ImageInfo,
    NotFound,
    PoolExhausted,
    PortInfo,
    ServerInfo,
)


def build_backend(settings: Settings) -> CloudBackend:
    """Выбрать бэкенд: симулятор при SIMULATE=1, иначе реальный OpenStack."""
    if settings.simulate:
        from .fake import FakeCloud

        return FakeCloud()

    from .openstack_backend import OpenStackBackend

    return OpenStackBackend(settings)


__all__ = [
    "CloudAuthError",
    "CloudBackend",
    "CloudError",
    "CloudInfo",
    "FlavorInfo",
    "ImageInfo",
    "NotFound",
    "PoolExhausted",
    "PortInfo",
    "ServerInfo",
    "build_backend",
]

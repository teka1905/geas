"""Пакет контрактов проекта-потребителя.

Ручной код проекта живёт здесь, рядом с подпакетом
:mod:`app_contracts.generated`, который целиком принадлежит генератору.

Граница простая: всё, что внутри ``generated/``, переписывается командой
``openapi-contracts update`` и проверяется в CI командой
``openapi-contracts check``; всё, что снаружи (обёртки ``mocked_*``, overlay'и,
фабрики фикстур), пишет и правит команда.
"""

from __future__ import annotations

__all__: list[str] = []

"""Статический namespace операций.

Имена строятся из стабильных ключей manifest, а не из ``operationId``:
переименование ``operationId`` на стороне бэкенда ломает binding, но публичное
Python-имя само не меняется.
"""

# Файл сгенерирован автоматически командой 'openapi-contracts update'.
# Не редактируйте его руками: изменения будут затёрты, а 'openapi-contracts check'
# уронит CI на расхождении.
from __future__ import annotations

from openapi_contracts.runtime import OperationHandle, OperationRegistry

from ._registry import REGISTRY

__all__ = ["Operations", "operations"]


class _NsApi:
    """Операции пространства имён ``api``."""

    def __init__(self, registry: OperationRegistry) -> None:
        self._registry = registry

    @property
    def create_document(self) -> OperationHandle:
        """Операция ``api.createDocument``."""
        return self._registry.by_key('api.createDocument')

    @property
    def list_documents(self) -> OperationHandle:
        """Операция ``api.listDocuments``."""
        return self._registry.by_key('api.listDocuments')


class Operations:
    """Корень generated namespace."""

    def __init__(self, registry: OperationRegistry) -> None:
        self._registry = registry
        self._api = _NsApi(registry)

    @property
    def api(self) -> _NsApi:
        """Пространство имён ``api``."""
        return self._api

    def by_key(self, key: str) -> OperationHandle:
        """Найти операцию по стабильному ключу manifest.

        Строковый доступ — escape hatch для инструментов и CLI.
        В тестах рекомендуется generated namespace.
        """
        return self._registry.by_key(key)

    def keys(self) -> tuple[str, ...]:
        """Все ключи операций."""
        return self._registry.keys()


operations = Operations(REGISTRY)

"""Реестр операций: сериализованные контракты → :class:`OperationHandle`.

Реестр ленив: документ контракта читается и разбирается при первом обращении к
операции. Это держит импорт generated-пакета дешёвым даже на сотне операций и
не требует ни d42, ни JJ.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from ..errors import ArtifactError, OperationLookupError
from ..models import ParameterLocation
from .operation import (
    OperationHandle,
    ParameterView,
    RequestBodyView,
    RequestView,
    ResponseView,
)

__all__ = ["CONTRACT_DOCUMENT_VERSION", "OperationRegistry", "handle_from_document"]

#: Версия формата документа контракта. Реестр читает только её.
CONTRACT_DOCUMENT_VERSION = 1


def handle_from_document(
    document: Mapping[str, Any], *, d42_package: str | None = None
) -> OperationHandle:
    """Собрать :class:`OperationHandle` из канонического документа контракта."""
    artifact = document.get("artifact") or {}
    version = artifact.get("version")
    if version != CONTRACT_DOCUMENT_VERSION:
        raise ArtifactError(
            f"документ контракта версии {version!r} не поддерживается "
            f"(ожидалась {CONTRACT_DOCUMENT_VERSION}). Перегенерируйте артефакты командой "
            f"'openapi-contracts update' той же версией библиотеки"
        )

    request_raw = document["request"]
    parameters = [_parameter(item) for item in request_raw["parameters"]]
    request = RequestView(
        path=tuple(p for p in parameters if p.location is ParameterLocation.PATH),
        query=tuple(p for p in parameters if p.location is ParameterLocation.QUERY),
        header=tuple(p for p in parameters if p.location is ParameterLocation.HEADER),
        cookie=tuple(p for p in parameters if p.location is ParameterLocation.COOKIE),
        bodies=tuple(
            RequestBodyView(
                content_type=item["content_type"],
                required=item["required"],
                json_schema=item["schema"],
                d42_export=item.get("d42"),
            )
            for item in request_raw["bodies"]
        ),
    )

    responses = tuple(
        ResponseView(
            status=item["status"],
            content_type=item["content_type"],
            json_schema=item["schema"],
            headers=tuple(_parameter(header) for header in item["headers"]),
            d42_export=item.get("d42"),
        )
        for item in document["responses"]
    )

    d42_section = document.get("d42") or {}
    d42_reason = d42_section.get("reason")
    modules: dict[str, str | None] = {"request": None, "response": None}
    if d42_package and d42_section.get("enabled"):
        for direction in ("request", "response"):
            module = d42_section.get(f"{direction}_module")
            if module:
                modules[direction] = f"{d42_package}.{module}"

    return OperationHandle(
        key=document["key"],
        operation_id=document["operation_id"],
        method=document["method"],
        path=document["path"],
        source=document["source"],
        python_path=tuple(document["python_path"]),
        request=request,
        responses=responses,
        unsupported=tuple(document.get("unsupported") or ()),
        d42_modules=modules,
        d42_reason=d42_reason,
    )


def _parameter(item: Mapping[str, Any]) -> ParameterView:
    return ParameterView(
        name=item["name"],
        location=ParameterLocation(item["in"]),
        required=item["required"],
        style=item["style"],
        explode=item["explode"],
        json_schema=item["schema"],
    )


class OperationRegistry:
    """Ленивый реестр операций generated-пакета."""

    __slots__ = ("_by_python_path", "_cache", "_contracts_dir", "_d42_package", "_index")

    def __init__(
        self,
        *,
        index: Mapping[str, str],
        contracts_dir: Path,
        d42_package: str | None = None,
    ) -> None:
        self._index = dict(index)
        self._contracts_dir = Path(contracts_dir)
        self._d42_package = d42_package
        self._cache: dict[str, OperationHandle] = {}
        self._by_python_path: dict[tuple[str, ...], str] | None = None

    def keys(self) -> tuple[str, ...]:
        """Все ключи операций в стабильном порядке."""
        return tuple(sorted(self._index))

    def __iter__(self) -> Iterator[OperationHandle]:
        for key in self.keys():
            yield self.by_key(key)

    def __len__(self) -> int:
        return len(self._index)

    def by_key(self, key: str) -> OperationHandle:
        """Найти операцию по стабильному ключу manifest.

        Строковый доступ — escape hatch для инструментов. В тестах рекомендуется
        generated namespace: ``operations.ws2.add_ticket``.
        """
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        slug = self._index.get(key)
        if slug is None:
            raise OperationLookupError(
                f"операции {key!r} нет в реестре; известны {list(self.keys())}"
            )
        path = self._contracts_dir / f"{slug}.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ArtifactError(
                f"не найден документ контракта {path}. Если generated-пакет попадает в wheel, "
                f"добавьте *.json в package data; иначе перегенерируйте артефакты командой "
                f"'openapi-contracts update'"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"документ контракта {path} повреждён: {exc}") from exc
        handle = handle_from_document(document, d42_package=self._d42_package)
        self._cache[key] = handle
        return handle

    def by_python_path(self, *segments: str) -> OperationHandle:
        """Найти операцию по её пути в generated namespace."""
        if self._by_python_path is None:
            self._by_python_path = {self.by_key(key).python_path: key for key in self.keys()}
        key = self._by_python_path.get(tuple(segments))
        if key is None:
            raise OperationLookupError(f"в реестре нет операции с Python path {'.'.join(segments)}")
        return self.by_key(key)

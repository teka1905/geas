"""Реестр операций: сериализованные контракты → :class:`OperationHandle`.

Реестр ленив: документ контракта читается и разбирается при первом обращении к
операции. Это держит импорт generated-пакета дешёвым даже на сотне операций и
не требует ни d42, ни JJ.

Реестр — единственное место, которое знает физическую раскладку артефактов
(``contracts/<slug>.json`` и ``_d42/<module>.py``), поэтому именно он проставляет
во views координаты: файл контракта, JSON Pointer варианта и путь к исходнику
generated d42-схемы.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from ..errors import ArtifactError, OperationLookupError
from ..models import ParameterLocation
from .description import join_pointer
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

#: Каталог generated d42-модулей внутри каталога артефактов.
_D42_DIRECTORY = "_d42"


def handle_from_document(
    document: Mapping[str, Any],
    *,
    d42_package: str | None = None,
    contract_file: Path | str | None = None,
    d42_dir: Path | str | None = None,
) -> OperationHandle:
    """Собрать :class:`OperationHandle` из канонического документа контракта.

    ``contract_file`` и ``d42_dir`` необязательны: без них handle работает
    ровно как раньше, просто у его views нет координат артефактов. Если
    ``d42_dir`` не передан, он выводится из ``contract_file`` по фиксированной
    раскладке каталога артефактов.
    """
    artifact = document.get("artifact") or {}
    version = artifact.get("version")
    if version != CONTRACT_DOCUMENT_VERSION:
        raise ArtifactError(
            f"документ контракта версии {version!r} не поддерживается "
            f"(ожидалась {CONTRACT_DOCUMENT_VERSION}). Перегенерируйте артефакты командой "
            f"'geas update' той же версией библиотеки"
        )

    key = document["key"]
    contract = Path(contract_file) if contract_file is not None else None
    d42_section = document.get("d42") or {}
    d42_reason = d42_section.get("reason")
    modules: dict[str, str | None] = {"request": None, "response": None}
    sources: dict[str, Path | None] = {"request": None, "response": None}
    if d42_section.get("enabled"):
        directory = _d42_directory(d42_dir, contract)
        for direction in ("request", "response"):
            module = d42_section.get(f"{direction}_module")
            if not module:
                continue
            if d42_package:
                modules[direction] = f"{d42_package}.{module}"
            sources[direction] = _d42_source(directory, module)

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
                operation_key=key,
                contract_file=contract,
                json_pointer=join_pointer("request", "bodies", index, "schema"),
                # Координаты d42 проставляются только тому варианту, у которого
                # своя generated-схема действительно есть: у соседнего варианта
                # без ``d42`` путь к модулю уводил бы к чужой схеме.
                d42_module=modules["request"] if item.get("d42") else None,
                d42_source_path=sources["request"] if item.get("d42") else None,
            )
            for index, item in enumerate(request_raw["bodies"])
        ),
    )

    responses = tuple(
        ResponseView(
            status=item["status"],
            content_type=item["content_type"],
            json_schema=item["schema"],
            headers=tuple(_parameter(header) for header in item["headers"]),
            d42_export=item.get("d42"),
            operation_key=key,
            contract_file=contract,
            json_pointer=join_pointer("responses", index, "schema"),
            d42_module=modules["response"] if item.get("d42") else None,
            d42_source_path=sources["response"] if item.get("d42") else None,
        )
        for index, item in enumerate(document["responses"])
    )

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


def _d42_directory(d42_dir: Path | str | None, contract_file: Path | None) -> Path | None:
    """Каталог generated d42-модулей.

    Явное значение имеет приоритет; иначе каталог выводится из файла контракта
    по раскладке артефактов: ``<output>/contracts/<slug>.json`` и
    ``<output>/_d42/<module>.py`` лежат рядом.
    """
    if d42_dir is not None:
        return Path(d42_dir)
    if contract_file is None:
        return None
    return contract_file.parent.parent / _D42_DIRECTORY


def _d42_source(directory: Path | None, module: str) -> Path | None:
    """Файл generated d42-модуля, если он действительно лежит на диске.

    Путь вычисляется по имени модуля, а не через импорт: описание контракта
    обязано работать и там, где extra ``[d42]`` не установлен. Если файла нет
    (например, пакет установлен без generated d42), возвращается ``None`` —
    печатать несуществующий путь хуже, чем не печатать ничего.
    """
    if directory is None:
        return None
    candidate = directory / f"{module}.py"
    return candidate if candidate.is_file() else None


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
                f"'geas update'"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"документ контракта {path} повреждён: {exc}") from exc
        handle = handle_from_document(
            document,
            d42_package=self._d42_package,
            contract_file=path,
            d42_dir=self._contracts_dir.parent / _D42_DIRECTORY,
        )
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

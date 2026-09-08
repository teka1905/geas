"""Нормализация параметров и поддержанная матрица сериализации.

Библиотека не обязана поддерживать все комбинации ``style``/``explode``, но обязана
никогда не сериализовать и не разбирать их **приблизительно**. Поэтому здесь есть
явная матрица: комбинация либо в ней, либо генерация падает с понятным текстом.

Поддержано:

============  ===============  ========  ================================
location      style            explode   форма значения
============  ===============  ========  ================================
path          simple           false     скаляр или массив через запятую
query         form             true      скаляр; массив как повторы ключа
query         form             false     скаляр; массив через запятую
query         spaceDelimited   false     массив через пробел
query         pipeDelimited    false     массив через ``|``
header        simple           false     скаляр или массив через запятую
cookie        form             true      скаляр
============  ===============  ========  ================================

Не поддержано (и приводит к ошибке): ``deepObject``, ``label``, ``matrix``,
объекты и вложенные массивы в параметрах, ``content`` вместо ``schema``.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import UnsupportedConstructError
from ..models import (
    ArrayNode,
    BooleanNode,
    IntegerNode,
    NumberNode,
    ParameterContract,
    ParameterLocation,
    RefNode,
    SchemaNode,
    StringNode,
)
from ..paths import ContractPath
from ..waivers import WaiverRule
from .raw import RawParameter
from .schemas import SchemaNormalizer

__all__ = [
    "ARRAY_DELIMITERS",
    "SUPPORTED_SERIALIZATION",
    "default_style",
    "normalize_parameter",
]

#: Разрешённые сочетания ``(location, style, explode, kind)``.
#: ``kind`` — ``scalar`` или ``array``.
SUPPORTED_SERIALIZATION: frozenset[tuple[str, str, bool, str]] = frozenset(
    {
        ("path", "simple", False, "scalar"),
        ("path", "simple", False, "array"),
        ("query", "form", True, "scalar"),
        ("query", "form", False, "scalar"),
        ("query", "form", True, "array"),
        ("query", "form", False, "array"),
        ("query", "spaceDelimited", False, "array"),
        ("query", "pipeDelimited", False, "array"),
        ("header", "simple", False, "scalar"),
        ("header", "simple", False, "array"),
        ("cookie", "form", True, "scalar"),
        ("cookie", "form", False, "scalar"),
    }
)

#: Разделитель для массивов, сериализованных одной строкой.
ARRAY_DELIMITERS: dict[str, str] = {
    "form": ",",
    "simple": ",",
    "spaceDelimited": " ",
    "pipeDelimited": "|",
}

_SCALAR_NODES = (StringNode, IntegerNode, NumberNode, BooleanNode)


def default_style(location: ParameterLocation) -> str:
    """Стиль по умолчанию из спецификации OpenAPI 3.0."""
    if location in (ParameterLocation.QUERY, ParameterLocation.COOKIE):
        return "form"
    return "simple"


def _kind(node: SchemaNode) -> str:
    if isinstance(node, ArrayNode):
        return "array"
    if isinstance(node, _SCALAR_NODES):
        return "scalar"
    return "other"


def normalize_parameter(
    raw: RawParameter,
    *,
    normalizer: SchemaNormalizer,
    base: Path,
    root: ContractPath,
    status: int | str | None = None,
) -> ParameterContract:
    """Нормализовать параметр и проверить, что его сериализация поддержана."""
    schema = normalizer.normalize(
        raw.schema,
        base=base,
        origin=raw.origin.child("schema"),
        root=root,
        status=status,
    )
    kind = _kind(schema)

    def rejected(message: str) -> UnsupportedConstructError:
        """Ошибка параметра со всеми известными координатами.

        Одного JSON Pointer мало: в проекте с сотней операций по нему непонятно,
        чей это параметр и в каком направлении он разбирался.
        """
        return UnsupportedConstructError(
            message,
            source=raw.origin.source,
            operation_key=normalizer.operation_key,
            direction=normalizer.direction.value,
            json_pointer=raw.origin.pointer,
        )

    if isinstance(schema, RefNode):
        raise rejected(
            f"параметр {raw.name!r} ссылается на именованную схему; параметры должны быть "
            f"скалярами или массивами скаляров"
        )
    if kind == "other":
        raise rejected(
            f"параметр {raw.name!r} в {raw.location.value} описан как "
            f"{type(schema).__name__}; поддержаны только скаляры и массивы скаляров"
        )
    if isinstance(schema, ArrayNode) and _kind(schema.items) != "scalar":
        raise rejected(
            f"параметр {raw.name!r}: вложенные массивы и объекты внутри массива не "
            f"сериализуются однозначно"
        )

    marker = (raw.location.value, raw.style, raw.explode, kind)
    if marker not in SUPPORTED_SERIALIZATION and not _serialization_waived(
        raw, normalizer, root, status
    ):
        raise rejected(
            f"параметр {raw.name!r}: сочетание in={raw.location.value}, "
            f"style={raw.style}, explode={raw.explode} для {kind} не поддержано. "
            f"Библиотека не сериализует и не разбирает такие значения приблизительно"
        )

    if raw.location is ParameterLocation.PATH and not raw.required:
        raise rejected(f"path-параметр {raw.name!r} обязан быть required: true")

    return ParameterContract(
        name=raw.name,
        location=raw.location,
        required=raw.required,
        schema=schema,
        style=raw.style,
        explode=raw.explode,
        origin=raw.origin,
    )


def _serialization_waived(
    raw: RawParameter,
    normalizer: SchemaNormalizer,
    root: ContractPath,
    status: int | str | None,
) -> bool:
    """Разрешена ли неподдержанная сериализация явным waiver'ом."""
    from ..fingerprints import semantic_source_digest

    waiver = normalizer.waivers.consult(
        operation=normalizer.operation_key,
        direction=normalizer.direction,
        path=root,
        rule=WaiverRule.ALLOW_UNSUPPORTED_SERIALIZATION,
        source_digest=semantic_source_digest(raw.schema),
        status=status,
    )
    return waiver is not None

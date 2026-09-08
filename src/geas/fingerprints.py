"""Канонизация и отпечатки.

Отпечаток обязан меняться ровно тогда, когда меняется **смысл** контракта.
Изменение ``description``, ``example``, ``default`` или порядка ключей смысл не
меняет и отпечаток менять не должно.

Ключевое требование к обходу — **position-awareness**. В реальных спецификациях
сотни бизнес-полей называются как ключевые слова (``type``, ``items``,
``required``, ``description``, ``properties``). Наивный рекурсивный обход, который
смотрит на имя ключа без учёта позиции в дереве, портит их все. Поэтому
:func:`strip_annotations` спускается только туда, где по структуре Schema Object
обязана лежать схема, и никогда не интерпретирует ключи внутри ``properties``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "ANNOTATION_KEYS",
    "canonical_json",
    "digest",
    "semantic_source_digest",
    "strip_annotations",
]

#: Ключи Schema Object, которые не влияют на семантику контракта.
ANNOTATION_KEYS = frozenset(
    {
        "description",
        "title",
        "example",
        "examples",
        "externalDocs",
        "xml",
        "deprecated",
        "default",
        "summary",
        "readOnly",  # применяется отдельно на этапе direction и в IR уже развёрнут
        "writeOnly",
    }
)

#: Ключи Schema Object, значение которых — одна вложенная схема.
_SINGLE_SCHEMA_KEYS = ("items", "additionalProperties", "not")
#: Ключи Schema Object, значение которых — список вложенных схем.
_SCHEMA_LIST_KEYS = ("allOf", "oneOf", "anyOf")
#: Ключи Schema Object, значение которых — словарь «имя → схема».
_SCHEMA_MAP_KEYS = ("properties", "patternProperties", "definitions")


def canonical_json(value: Any) -> str:
    """Каноническая сериализация: сортировка ключей, компактно, UTF-8, без пробелов."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    """SHA-256 канонической сериализации, hex."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def strip_annotations(node: Any, *, keys: frozenset[str] = ANNOTATION_KEYS) -> Any:
    """Удалить несемантические ключи, спускаясь только по позициям схем.

    ``properties`` обходится как словарь имён: ключи копируются как есть, а обход
    продолжается только в значениях. Поэтому свойство с именем ``description``
    остаётся на месте.
    """
    if isinstance(node, list):
        return [strip_annotations(item, keys=keys) for item in node]
    if not isinstance(node, dict):
        return node

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in keys:
            continue
        if key in _SINGLE_SCHEMA_KEYS and isinstance(value, dict):
            result[key] = strip_annotations(value, keys=keys)
        elif key in _SINGLE_SCHEMA_KEYS:
            result[key] = value
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            result[key] = [strip_annotations(item, keys=keys) for item in value]
        elif key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            result[key] = {name: strip_annotations(item, keys=keys) for name, item in value.items()}
        else:
            result[key] = value
    return result


def semantic_source_digest(fragment: Any) -> str:
    """Отпечаток исходного фрагмента спецификации для закрепления waiver'а.

    Считается по фрагменту, очищенному от аннотаций, поэтому правка
    ``description`` не ломает waiver, а изменение типа, ``enum``, ``required`` или
    ``pattern`` — ломает.
    """
    return digest(strip_annotations(fragment))

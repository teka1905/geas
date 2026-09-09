"""Человекочитаемое описание generated JSON Schema.

Модуль отвечает на единственный вопрос автора теста: «какие поля разрешает
контракт, какие из них обязательны и какие у них ограничения». Это навигация по
уже сгенерированному документу, а не второй валидатор, поэтому:

* **ничего не ослабляется.** Неизвестное ключевое слово не превращается в «любое
  значение»: оно называется в строке узла как есть, а точная семантика остаётся
  в самой JSON Schema — путь к ней печатается в конце описания;
* **сеть не используется.** Раскрываются только локальные ``$ref``
  (``#/$defs/...``, ``#/definitions/...``); внешний URL или файл лишь называется
  ссылкой;
* **обход конечен.** Рекурсивный ``$ref`` и вложенность глубже ``max_depth``
  обрезаются явным маркером, а не уводят обход в бесконечность;
* **вывод детерминирован.** Порядок ключей берётся из документа (в generated-
  артефактах он отсортирован), длинные ``enum`` и ``pattern`` усекаются по
  фиксированным границам.

Обязательность поля определяется **только** массивом ``required`` родительского
объекта. Ни ``default``, ни ``const``, ни само присутствие в ``properties``
обязательным поле не делают.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Direction

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "describe_contract",
    "escape_pointer_token",
    "join_pointer",
    "resolve_json_pointer",
    "unescape_pointer_token",
]

#: Глубина обхода по умолчанию. Дальше печатается маркер продолжения: описание
#: должно помещаться в терминал, а полная схема всегда доступна по пути.
DEFAULT_MAX_DEPTH = 6

#: Сколько значений ``enum`` показывать целиком.
_MAX_ENUM_VALUES = 12

#: Максимальная длина показанного ``pattern``.
_MAX_PATTERN_LENGTH = 60

#: Полные диапазоны целочисленных ``format``. Генератор выписывает их в схему
#: явными ``minimum``/``maximum``; в описании они лишь повторяют сам ``format`` и
#: заслоняют настоящие ограничения, поэтому печатается что-то одно.
_INTEGER_FORMAT_RANGES = {
    "int32": (-(2**31), 2**31 - 1),
    "int64": (-(2**63), 2**63 - 1),
}

#: Отдельный от ``None`` признак «ключа нет»: ``None`` — легальное значение JSON.
_MISSING: Any = object()

#: Ключевые слова, которые модуль печатает сам.
_RENDERED_KEYWORDS = frozenset(
    {
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "nullable",
        "oneOf",
        "pattern",
        "prefixItems",
        "properties",
        "required",
        "type",
        "uniqueItems",
    }
)

#: Аннотации: на множество валидных значений не влияют, в описании только шумят.
_ANNOTATION_KEYWORDS = frozenset(
    {
        "$comment",
        "$defs",
        "$id",
        "$schema",
        "default",
        "definitions",
        "deprecated",
        "description",
        "discriminator",
        "example",
        "examples",
        "externalDocs",
        "readOnly",
        "title",
        "writeOnly",
        "xml",
    }
)


# ------------------------------------------------------------------ RFC 6901


def escape_pointer_token(token: str) -> str:
    """Экранировать один сегмент JSON Pointer по RFC 6901.

    Порядок замен обязателен: сначала ``~``, потом ``/``. Обратный порядок
    превратил бы ``/`` в ``~1``, а затем в ``~01`` — то есть в другой указатель.
    """
    return token.replace("~", "~0").replace("/", "~1")


def unescape_pointer_token(token: str) -> str:
    """Снять экранирование одного сегмента JSON Pointer (RFC 6901)."""
    return token.replace("~1", "/").replace("~0", "~")


def join_pointer(*tokens: str | int) -> str:
    """Собрать JSON Pointer из сегментов, экранировав каждый."""
    return "".join(f"/{escape_pointer_token(str(token))}" for token in tokens)


def resolve_json_pointer(document: Any, pointer: str, *, default: Any = None) -> Any:
    """Разрешить JSON Pointer внутри уже загруженного документа.

    Ведущий ``#`` допускается: в ``$ref`` указатель приходит именно в такой
    форме. Ничего не читается с диска и из сети — только обход переданного
    значения.
    """
    if pointer.startswith("#"):
        pointer = pointer[1:]
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return default
    current = document
    for raw in pointer.split("/")[1:]:
        token = unescape_pointer_token(raw)
        if isinstance(current, Mapping):
            if token not in current:
                return default
            current = current[token]
            continue
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            try:
                index = int(token)
            except ValueError:
                return default
            if index < 0 or index >= len(current):
                return default
            current = current[index]
            continue
        return default
    return current


# -------------------------------------------------------------------- дерево


@dataclass(frozen=True, kw_only=True, slots=True)
class _Node:
    """Узел схемы, подготовленный к печати."""

    schema: Mapping[str, Any]
    #: Метка ``$ref``, если узел пришёл через ссылку.
    ref_label: str | None
    #: Стек уже раскрытых ссылок на пути к узлу — защита от рекурсии.
    stack: tuple[str, ...]
    #: Можно ли спускаться в детей. ``False`` у рекурсии и внешних ссылок.
    expandable: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class _Entry:
    """Ребёнок узла: имя, схема и обязательность (если применимо)."""

    label: str
    schema: Mapping[str, Any]
    requirement: str | None


class _SchemaTree:
    """Обход JSON Schema с раскрытием локальных ссылок и защитой от рекурсии."""

    __slots__ = ("_max_depth", "_root")

    def __init__(self, root: Mapping[str, Any], *, max_depth: int) -> None:
        self._root = root
        self._max_depth = max(1, max_depth)

    def render(self) -> list[str]:
        """Построчное описание схемы: корень и дерево полей под ним."""
        node = self._node(self._root, stack=())
        lines = [self._summary(node)]
        if node.expandable:
            lines.extend(self._render_children(node, prefix="", depth=1))
        return lines

    # ------------------------------------------------------------ раскрытие

    def _node(self, schema: Mapping[str, Any], *, stack: tuple[str, ...]) -> _Node:
        """Раскрыть локальный ``$ref`` и решить, можно ли спускаться в детей."""
        ref = schema.get("$ref")
        if not isinstance(ref, str):
            return _Node(schema=schema, ref_label=None, stack=stack, expandable=True)
        if not ref.startswith("#"):
            # Внешний URL или соседний файл. Разрешать его — это поход в сеть или
            # на диск; описание обязано остаться офлайновым.
            label = f"$ref {ref} (внешняя ссылка не раскрывается)"
            return _Node(schema=schema, ref_label=label, stack=stack, expandable=False)
        if ref in stack:
            return _Node(
                schema=schema, ref_label=f"$ref {ref} (рекурсия)", stack=stack, expandable=False
            )
        target = resolve_json_pointer(self._root, ref, default=_MISSING)
        if not isinstance(target, Mapping):
            label = f"$ref {ref} (не найден в контракте)"
            return _Node(schema=schema, ref_label=label, stack=stack, expandable=False)
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        merged: Mapping[str, Any] = {**target, **siblings} if siblings else target
        return _Node(schema=merged, ref_label=f"$ref {ref}", stack=(*stack, ref), expandable=True)

    # --------------------------------------------------------------- строки

    def _render_children(self, node: _Node, *, prefix: str, depth: int) -> list[str]:
        entries = self._entries(node.schema)
        if not entries:
            return []
        if depth > self._max_depth:
            return [
                f"{prefix}└── … (вложенность глубже {self._max_depth}; полная схема — по пути ниже)"
            ]
        lines: list[str] = []
        for index, entry in enumerate(entries):
            last = index == len(entries) - 1
            child = self._node(entry.schema, stack=node.stack)
            requirement = f"{entry.requirement}; " if entry.requirement else ""
            branch = "└── " if last else "├── "
            lines.append(f"{prefix}{branch}{entry.label} — {requirement}{self._summary(child)}")
            if child.expandable:
                child_prefix = prefix + ("    " if last else "│   ")
                lines.extend(self._render_children(child, prefix=child_prefix, depth=depth + 1))
        return lines

    def _entries(self, schema: Mapping[str, Any]) -> list[_Entry]:
        """Дети узла в стабильном порядке: свойства, элементы, ветки комбинаторов."""
        entries: list[_Entry] = []
        properties = schema.get("properties")
        required = _string_sequence(schema.get("required"))
        if isinstance(properties, Mapping):
            for name, value in properties.items():
                if not isinstance(value, Mapping):
                    continue
                requirement = "required" if str(name) in required else "optional"
                entries.append(_Entry(label=str(name), schema=value, requirement=requirement))
        additional = schema.get("additionalProperties")
        if isinstance(additional, Mapping) and _has_structure(additional):
            entries.append(
                _Entry(label="<дополнительные свойства>", schema=additional, requirement=None)
            )
        for keyword in ("items", "prefixItems"):
            entries.extend(_items_entries(keyword, schema.get(keyword)))
        for keyword in ("oneOf", "anyOf", "allOf"):
            branches = schema.get(keyword)
            if not isinstance(branches, Sequence) or isinstance(branches, (str, bytes)):
                continue
            for index, branch in enumerate(branches):
                if isinstance(branch, Mapping) and _has_structure(branch):
                    entries.append(
                        _Entry(label=f"{keyword}[{index}]", schema=branch, requirement=None)
                    )
        return entries

    def _summary(self, node: _Node) -> str:
        """Однострочное описание узла: тип, комбинаторы и ограничения."""
        if node.ref_label is not None and not node.expandable:
            return node.ref_label
        parts: list[str] = []
        if node.ref_label is not None:
            parts.append(node.ref_label)
        schema = node.schema
        type_part = _type_summary(schema)
        if type_part:
            parts.append(type_part)
        combinator = _combinator_summary(schema)
        if combinator and combinator != type_part:
            parts.append(combinator)
        parts.extend(_constraints(schema))
        return "; ".join(parts)


def _items_entries(keyword: str, value: Any) -> list[_Entry]:
    """Дети для ``items``/``prefixItems`` в обеих формах — схема и кортеж схем."""
    if isinstance(value, Mapping):
        return [_Entry(label=keyword, schema=value, requirement=None)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _Entry(label=f"{keyword}[{index}]", schema=item, requirement=None)
            for index, item in enumerate(value)
            if isinstance(item, Mapping)
        ]
    return []


def _has_structure(schema: Mapping[str, Any]) -> bool:
    """Есть ли у узла собственная структура, ради которой стоит его раскрывать."""
    if any(key in schema for key in ("properties", "items", "prefixItems", "$ref")):
        return True
    if any(key in schema for key in ("oneOf", "anyOf", "allOf")):
        return True
    return isinstance(schema.get("additionalProperties"), Mapping)


# ---------------------------------------------------------------- summaries


def _string_sequence(value: Any) -> frozenset[str]:
    """Множество строк из массива JSON Schema (``required`` и подобные)."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return frozenset(str(item) for item in value)
    return frozenset()


def _types(schema: Mapping[str, Any]) -> tuple[list[str], bool]:
    """Объявленные типы и признак nullable.

    ``null`` внутри ``type`` и ``nullable``/``x-nullable`` — три записи одного и
    того же факта; наружу отдаётся один флаг.
    """
    raw = schema.get("type")
    if isinstance(raw, str):
        types = [raw]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        types = [item for item in raw if isinstance(item, str)]
    else:
        types = []
    nullable = schema.get("nullable") is True or schema.get("x-nullable") is True
    if "null" in types:
        nullable = True
        types = [item for item in types if item != "null"]
    return types, nullable


def _type_summary(schema: Mapping[str, Any]) -> str:
    types, nullable = _types(schema)
    if types:
        base = " | ".join(types)
    elif combinator := _combinator_summary(schema):
        base = combinator
    elif "enum" in schema or "const" in schema:
        # Множество значений задано перечислением: тип из него и так очевиден,
        # а придумывать его за спецификацию нельзя.
        base = ""
    else:
        base = "тип не указан"
    if nullable:
        return f"{base} | null" if base else "null"
    return base


def _combinator_summary(schema: Mapping[str, Any]) -> str:
    """Краткая запись ``oneOf``/``anyOf``/``allOf`` — по первому присутствующему."""
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, Sequence) or isinstance(branches, (str, bytes)):
            continue
        names = [_short(branch) for branch in branches if isinstance(branch, Mapping)]
        if names:
            return f"{keyword}({', '.join(names)})"
    return ""


def _short(schema: Mapping[str, Any]) -> str:
    """Имя ветки комбинатора или значения ``additionalProperties`` одним словом."""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1] or ref
    types, nullable = _types(schema)
    if types:
        return " | ".join([*types, "null"] if nullable else types)
    if nullable:
        # Ветка ``{"type": "null"}`` — это тип, а не отсутствие типа.
        return "null"
    if "enum" in schema:
        return "enum"
    if "const" in schema:
        return "const"
    if "properties" in schema:
        return "object"
    if "items" in schema or "prefixItems" in schema:
        return "array"
    combinator = _combinator_summary(schema)
    return combinator or "тип не указан"


def _constraints(schema: Mapping[str, Any]) -> list[str]:
    """Ограничения узла в фиксированном порядке."""
    parts: list[str] = []
    if "const" in schema:
        parts.append(f"const {_json(schema['const'])}")
    enum = schema.get("enum")
    if isinstance(enum, Sequence) and not isinstance(enum, (str, bytes)):
        parts.append(_enum_summary(enum))
    for keyword in ("format", "pattern", "minLength", "maxLength"):
        value = schema.get(keyword)
        if value is None:
            continue
        if keyword == "pattern":
            parts.append(f"pattern {_truncate(str(value), _MAX_PATTERN_LENGTH)}")
        elif keyword == "format":
            parts.append(f"format {value}")
        else:
            parts.append(f"{keyword} {_json(value)}")
    if not _bounds_repeat_format(schema):
        parts.extend(_bounds(schema))
    for keyword in ("minItems", "maxItems"):
        value = schema.get(keyword)
        if value is not None:
            parts.append(f"{keyword} {_json(value)}")
    if schema.get("uniqueItems") is True:
        parts.append("uniqueItems")
    note = _object_note(schema)
    if note is not None:
        parts.append(note)
    extra = _extra_keywords(schema)
    if extra:
        parts.append(f"ещё: {', '.join(extra)}")
    return parts


def _bounds_repeat_format(schema: Mapping[str, Any]) -> bool:
    """Совпадают ли границы ровно с диапазоном объявленного целочисленного ``format``.

    Сузить контракт это не может: если границы отличаются от диапазона формата
    хотя бы на единицу или заданы исключающей формой, они печатаются как есть.
    """
    bounds = _INTEGER_FORMAT_RANGES.get(str(schema.get("format")))
    if bounds is None:
        return False
    if "exclusiveMinimum" in schema or "exclusiveMaximum" in schema:
        return False
    return (schema.get("minimum"), schema.get("maximum")) == bounds


def _bounds(schema: Mapping[str, Any]) -> list[str]:
    """Числовые границы в обеих формах: 2020-12 и булевой из draft-4."""
    parts: list[str] = []
    for keyword, exclusive_keyword in (
        ("minimum", "exclusiveMinimum"),
        ("maximum", "exclusiveMaximum"),
    ):
        value = schema.get(keyword)
        exclusive = schema.get(exclusive_keyword)
        if isinstance(exclusive, bool):
            # draft-4: ``exclusiveMinimum: true`` уточняет соседний ``minimum``.
            if value is not None:
                name = exclusive_keyword if exclusive else keyword
                parts.append(f"{name} {_json(value)}")
            continue
        if value is not None:
            parts.append(f"{keyword} {_json(value)}")
        if exclusive is not None:
            parts.append(f"{exclusive_keyword} {_json(exclusive)}")
    return parts


def _object_note(schema: Mapping[str, Any]) -> str | None:
    """Как объект относится к незаявленным свойствам.

    Отсутствие ``additionalProperties`` в JSON Schema означает «разрешены»;
    выдавать его за ``false`` нельзя — это ужесточение чужого контракта.
    """
    types, _ = _types(schema)
    additional = schema.get("additionalProperties", _MISSING)
    if "object" not in types and "properties" not in schema and additional is _MISSING:
        return None
    if additional is _MISSING or additional is True:
        return "дополнительные свойства разрешены"
    if additional is False:
        return "дополнительные свойства запрещены"
    if isinstance(additional, Mapping):
        return f"дополнительные свойства: {_short(additional)}"
    return None


def _extra_keywords(schema: Mapping[str, Any]) -> list[str]:
    """Ключевые слова, которые модуль не печатает разбором.

    Они не игнорируются и не превращаются в «любое значение»: их имена видны в
    строке узла, а смысл читается в самой JSON Schema по напечатанному пути.
    """
    return sorted(
        key
        for key in schema
        if key not in _RENDERED_KEYWORDS
        and key not in _ANNOTATION_KEYWORDS
        and not key.startswith("x-")
    )


def _enum_summary(values: Sequence[Any]) -> str:
    shown = [_json(item) for item in values[:_MAX_ENUM_VALUES]]
    if len(values) > _MAX_ENUM_VALUES:
        shown.append(f"… всего {len(values)}")
    return f"enum[{', '.join(shown)}]"


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# -------------------------------------------------------------------- фасад


def describe_contract(
    *,
    operation_key: str | None = None,
    direction: Direction,
    status: int | str | None = None,
    content_type: str | None = None,
    required: bool | None = None,
    json_schema: Mapping[str, Any] | None,
    contract_path: str | None = None,
    d42_reference: str | None = None,
    d42_source_path: Path | str | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> str:
    """Собрать описание одного направления контракта.

    Функция ничего не читает и не пишет: на вход подаётся уже разобранная схема
    и готовые координаты артефактов, на выход отдаётся строка.
    """
    lines: list[str] = []
    if operation_key:
        lines.append(operation_key)
    lines.append(_direction_line(direction, status, content_type, required))
    lines.append("")
    if json_schema is None:
        lines.append("тело отсутствует")
    else:
        lines.extend(_SchemaTree(json_schema, max_depth=max_depth).render())
    if contract_path:
        lines.extend(["", "JSON Schema:", f"  {contract_path}"])
    lines.extend(["", "d42:", f"  {d42_reference or 'схемы не сгенерированы'}"])
    if d42_source_path is not None:
        lines.extend(["", "d42 source:", f"  {d42_source_path}"])
    return "\n".join(lines)


def _direction_line(
    direction: Direction,
    status: int | str | None,
    content_type: str | None,
    required: bool | None,
) -> str:
    if direction is Direction.RESPONSE:
        parts = ["Response"]
        if status is not None:
            parts.append(str(status))
        parts.append(content_type or "без тела")
        return " ".join(parts)
    parts = ["Request", content_type or "без тела"]
    if required is not None:
        parts.append("(тело обязательно)" if required else "(тело необязательно)")
    return " ".join(parts)

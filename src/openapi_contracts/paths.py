"""Contract path — стабильный адрес места внутри контракта.

Исходный JSON Pointer документа (``/components/schemas/Thing/properties/name``)
нестабилен: стоит вынести схему в ``$ref`` или обратно — и все waiver'ы протухнут,
хотя контракт не изменился.

Поэтому waiver'ы, ``non_waivable`` и overlay'и адресуются **contract path** —
указателем по *форме данных*, а не по тексту спецификации:

===============  ====================================================
Сегмент          Значение
===============  ====================================================
``name``         свойство объекта
``-``            элементы массива (аналог ``EACH`` в overlay'ах)
``#0``           вариант ``oneOf``/``anyOf`` или часть ``allOf`` по индексу
``+``            схема ``additionalProperties``
===============  ====================================================

Строковая форма выглядит как JSON Pointer: ``/groups/-/title``, ``/payload/#1/id``.
Экранирование — по RFC 6901 (``~0`` для ``~``, ``~1`` для ``/``) плюс одно
расширение: свойство, чьё имя совпадает со служебным сегментом, пишется с
префиксом ``~2`` (``/~2-`` — это свойство с именем ``-``, а не элементы массива).
Пустые сегменты запрещены: свойство с пустым именем встречается исчезающе редко,
а спутать ``/a//b`` с опечаткой — легко.

Ошибки библиотеки печатают contract path рядом с исходным JSON Pointer, поэтому
готовый waiver можно скопировать прямо из сообщения.
"""

from __future__ import annotations

import re

from .errors import ContractError

__all__ = [
    "ADDITIONAL_PROPERTIES",
    "ARRAY_ITEMS",
    "ContractPath",
    "escape_property",
    "format_contract_path",
    "is_variant_segment",
    "parse_contract_path",
    "variant_index",
    "variant_segment",
]

#: Сегмент, адресующий элементы массива.
ARRAY_ITEMS = "-"
#: Сегмент, адресующий схему ``additionalProperties``.
ADDITIONAL_PROPERTIES = "+"

_VARIANT = re.compile(r"^#(\d+)$")

#: Contract path — кортеж сегментов. Пустой кортеж означает корень контракта.
ContractPath = tuple[str, ...]


def variant_segment(index: int) -> str:
    """Сегмент для варианта композиции по индексу."""
    return f"#{index}"


def is_variant_segment(segment: str) -> bool:
    """Является ли сегмент указателем на вариант композиции."""
    return _VARIANT.match(segment) is not None


def variant_index(segment: str) -> int:
    """Индекс варианта из сегмента ``#N``."""
    match = _VARIANT.match(segment)
    if match is None:
        raise ContractError(f"сегмент {segment!r} не является указателем на вариант композиции")
    return int(match.group(1))


def _is_reserved(segment: str) -> bool:
    return segment in (ARRAY_ITEMS, ADDITIONAL_PROPERTIES) or is_variant_segment(segment)


def escape_property(name: str) -> str:
    """Экранировать имя свойства для использования как сегмент contract path."""
    escaped = name.replace("~", "~0").replace("/", "~1")
    return f"~2{escaped}" if _is_reserved(escaped) else escaped


def format_contract_path(path: ContractPath) -> str:
    """Собрать строковую форму contract path.

    Служебные сегменты (``-``, ``+``, ``#N``) попадают в строку как есть; всё
    остальное считается именем свойства и экранируется.
    """
    if not path:
        return "/"
    parts = [segment if _is_reserved(segment) else escape_property(segment) for segment in path]
    return "/" + "/".join(parts)


def parse_contract_path(text: str) -> ContractPath:
    """Разобрать строковую форму contract path.

    Пустая строка и ``"/"`` означают корень контракта.
    """
    if text in ("", "/"):
        return ()
    if not text.startswith("/"):
        raise ContractError(
            f"contract path {text!r} должен начинаться с '/' (например '/groups/-/title')"
        )
    segments: list[str] = []
    for raw in text[1:].split("/"):
        if not raw:
            raise ContractError(
                f"contract path {text!r} содержит пустой сегмент. "
                f"Уберите лишний '/' — свойства с пустым именем не поддерживаются"
            )
        token = raw[2:] if raw.startswith("~2") else raw
        if not token:
            # '/~2' — экранирующий префикс без имени. Без этой проверки пустой
            # сегмент проходил бы в обход запрета выше.
            raise ContractError(
                f"contract path {text!r}: префикс '~2' стоит без имени свойства. "
                f"Свойства с пустым именем не поддерживаются"
            )
        segments.append(token.replace("~1", "/").replace("~0", "~"))
    return tuple(segments)

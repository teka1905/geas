"""Классификация media type: что именно контракт знает о теле.

Контракт строится для двух видов тел:

* **JSON** — ``application/json``, ``…+json``, ``text/json`` и ``*/*``. ``*/*`` серверные
  фреймворки (springdoc, JAX-RS) массово пишут для JSON-ответов, поэтому в обоих
  диалектах он считается JSON; ``text/json`` — устаревшее имя JSON, его по умолчанию
  пишет Swashbuckle (ASP.NET Core);
* **текст** — конкретный ``text/*`` (``text/plain``, ``text/html``, ``text/csv``). Тело —
  строка, а схема обязана описывать ``type: string``.

Всё остальное (``multipart/*``, ``application/octet-stream``, XML — ``application/xml``,
``text/xml``, ``…+xml`` —, YAML — ``text/yaml``, ``…+yaml`` —, поток
``text/event-stream``, ``image/*``, wildcard вроде ``text/*``) в контракт не попадает:
вариант объявлен спецификацией, но его тело не проверяется. Причину называет
:func:`unchecked_reason`.
"""

from __future__ import annotations

from email.message import Message
from enum import Enum
from typing import Any

__all__ = [
    "DEFAULT_TEXT_CHARSET",
    "JSON_ALIASES",
    "MediaKind",
    "declared_charset",
    "is_json_media_type",
    "is_text_media_type",
    "media_kind",
    "media_priority",
    "media_range_specificity",
    "normalize_media_type",
    "unchecked_body_reason",
    "unchecked_reason",
]

#: Кодировка текстового тела, если в ``Content-Type`` нет ``charset``.
DEFAULT_TEXT_CHARSET = "utf-8"

#: ``text/*``-имена JSON: тело — JSON, а не строка.
_TEXT_JSON = frozenset({"text/json", "text/x-json"})

#: ``text/*``-имена XML (RFC 7303): структурированный формат, контракта для него нет.
_TEXT_XML = frozenset({"text/xml"})

#: ``text/*``-имена YAML: структурированный формат, контракта для него нет.
_TEXT_YAML = frozenset({"text/yaml", "text/x-yaml"})

#: Потоки: тело — последовательность событий, а не одна строка.
_TEXT_STREAMS = frozenset({"text/event-stream"})

#: JSON-типы, которые генераторы пишут рядом с основным (``*/*`` у springdoc и JAX-RS,
#: ``text/json`` у Swashbuckle). Среди вариантов одного слота они второстепенны.
JSON_ALIASES = frozenset({"*/*", "text/json", "text/x-json"})


class MediaKind(str, Enum):
    """Вид тела, который контракт умеет проверять."""

    JSON = "json"
    TEXT = "text"
    OTHER = "other"


def normalize_media_type(value: str) -> str:
    """Привести media type к канонической форме.

    Реальные спецификации массово пишут ``application/json;charset=UTF-8`` в
    разном регистре. Параметры отбрасываются, регистр приводится к нижнему —
    иначе сравнение content type просто никогда не совпадает.
    """
    return value.split(";", 1)[0].strip().lower()


def is_json_media_type(value: str) -> bool:
    """Является ли media type JSON строго: ``application/json`` или ``…+json``."""
    canonical = normalize_media_type(value)
    return canonical == "application/json" or canonical.endswith("+json")


def is_text_media_type(value: str) -> bool:
    """Является ли media type конкретным текстовым: ``text/plain``, ``text/html``, …

    ``text/*`` не называет формат тела, ``text/json`` — это JSON, ``text/xml`` —
    XML, поэтому текстовыми они не считаются.
    """
    return media_kind(value) is MediaKind.TEXT


def media_kind(value: str) -> MediaKind:
    """Вид тела по media type."""
    canonical = normalize_media_type(value)
    if canonical == "*/*" or canonical in _TEXT_JSON or is_json_media_type(canonical):
        return MediaKind.JSON
    if _is_structured_text(canonical) or canonical in _TEXT_STREAMS:
        return MediaKind.OTHER
    if canonical.startswith("text/") and canonical != "text/*":
        return MediaKind.TEXT
    return MediaKind.OTHER


def media_priority(value: str) -> tuple[int, str]:
    """Порядок «главности» варианта среди вариантов одного слота: меньше — главнее.

    ``application/json``, затем другие конкретные JSON-типы, затем
    :data:`JSON_ALIASES`, затем текст, затем остальное; внутри группы — по алфавиту.
    По этому порядку выбирается вариант, когда несколько вариантов описаны одной и той
    же схемой, и вариант, которому остаётся базовое имя d42-схемы.
    """
    canonical = normalize_media_type(value)
    if canonical == "application/json":
        return (0, canonical)
    kind = media_kind(canonical)
    if kind is MediaKind.JSON:
        return (2 if canonical in JSON_ALIASES else 1, canonical)
    if kind is MediaKind.TEXT:
        return (3, canonical)
    return (4, canonical)


def _is_structured_text(canonical: str) -> bool:
    """XML и YAML под ``text/*``: у таких тел есть структура, а не одна строка."""
    return (
        canonical in _TEXT_XML
        or canonical in _TEXT_YAML
        or canonical.endswith("+xml")
        or canonical.endswith("+yaml")
    )


def media_range_specificity(declared: str, actual: str) -> int | None:
    """Насколько объявленный media type (или media range) покрывает фактический.

    ``2`` — точное совпадение, ``1`` — ``type/*``, ``0`` — ``*/*``, ``None`` — не
    покрывает. Спецификация объявляет диапазон (``image/*``, ``*/*``), а клиент
    всегда шлёт конкретный тип (``image/png``), поэтому сравнивать надо так, а не на
    равенство.
    """
    declared_type = normalize_media_type(declared)
    actual_type = normalize_media_type(actual)
    if declared_type == actual_type:
        return 2
    if declared_type == "*/*":
        return 0
    if (
        declared_type.endswith("/*")
        and actual_type.partition("/")[0] == (declared_type.partition("/")[0])
    ):
        return 1
    return None


def unchecked_reason(value: str) -> str | None:
    """Почему тело этого media type не проверяется; ``None`` — проверяется.

    Формулировка одна для всех таких типов и совпадает с прежними версиями: причина
    входит в ``unsupported``, а ``unsupported`` — в семантику контракта, и её смена
    дала бы ложное изменение в ``geas diff`` у каждой такой операции. Почему XML, YAML,
    потоки и диапазоны не проверяются, объясняет матрица поддержки.
    """
    if media_kind(value) is not MediaKind.OTHER:
        return None
    canonical = normalize_media_type(value)
    return (
        f"media type {canonical!r} не является JSON-совместимым, "
        f"структурный контракт тела для него не строится"
    )


def unchecked_body_reason(value: str, schema: Any) -> str | None:
    """Как :func:`unchecked_reason`, но с учётом схемы тела.

    ``*/*`` считается JSON, но springdoc пишет его и для выгрузки файла:
    ``*/*: {type: string, format: binary}``. Такое тело — байты, а не JSON-строка, и мок
    отдал бы его в кавычках с ``Content-Type: application/json``.
    """
    reason = unchecked_reason(value)
    if reason is not None:
        return reason
    if (
        normalize_media_type(value) == "*/*"
        and isinstance(schema, dict)
        and schema.get("type") == "string"
        and schema.get("format") == "binary"
    ):
        return (
            "media type '*/*' со схемой type: string, format: binary — бинарное тело, "
            "структурный контракт тела для него не строится"
        )
    return None


def declared_charset(header: str) -> str | None:
    """``charset`` из значения ``Content-Type`` в нижнем регистре; ``None``, если не указан."""
    message = Message()
    message["content-type"] = header
    charset = message.get_content_charset()
    return charset.lower() if charset else None

"""Детерминированные правила именования.

Все имена в generated-артефактах выводятся отсюда: Python path операции, имена
d42-переменных, имена файлов. Правила обязаны быть документированными,
детерминированными и покрытыми тестами, потому что переименование generated Python
path — это breaking change для потребителя.

Ключевой принцип: **никаких молчаливых числовых суффиксов**. Любая коллизия — ошибка
генерации; разрешается только явным ``python_path`` в manifest.
"""

from __future__ import annotations

import keyword
import re

from .errors import NamespaceCollisionError

__all__ = [
    "RESERVED_NAMESPACE_ATTRIBUTES",
    "check_namespace_names",
    "d42_schema_name",
    "python_identifier",
    "python_path_from_key",
    "slugify_key",
    "to_pascal_case",
    "to_snake_case",
]

#: Публичные атрибуты generated namespace, которые нельзя занять операцией.
RESERVED_NAMESPACE_ATTRIBUTES = frozenset(
    {
        "by_key",
        "by_python_path",
        "keys",
        "operations",
        "registry",
    }
)

#: Soft keywords Python, которые нельзя использовать как имя без риска путаницы.
_SOFT_KEYWORDS = frozenset({"match", "case", "type", "_"})

_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]+")
_LOWER_UPPER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM_WORD = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def to_snake_case(name: str) -> str:
    """Привести произвольное имя к ``snake_case``.

    Алгоритм фиксирован и покрыт тестами:

    1. любая последовательность не буквенно-цифровых символов → ``_``;
    2. граница «конец аббревиатуры → слово» → ``_`` (``HTTPServer`` → ``http_server``);
    3. граница «строчная или цифра → заглавная» → ``_`` (``addTicket`` → ``add_ticket``,
       ``v2Get`` → ``v2_get``);
    4. всё в нижний регистр, повторные ``_`` схлопываются, крайние ``_`` убираются.

    Цифры не отрываются от предшествующих букв: ``ws2`` остаётся ``ws2``,
    ``oauth2`` — ``oauth2``.
    """
    if not name:
        return ""
    text = _NON_ALNUM.sub("_", name)
    text = _ACRONYM_WORD.sub("_", text)
    text = _LOWER_UPPER.sub("_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text.lower()


def to_pascal_case(name: str) -> str:
    """Привести имя к ``PascalCase`` (используется для имён d42-переменных)."""
    return "".join(part[:1].upper() + part[1:] for part in to_snake_case(name).split("_") if part)


def python_identifier(name: str, *, context: str) -> str:
    """Проверить, что имя пригодно как публичный Python-идентификатор.

    Бросает :class:`NamespaceCollisionError`, если имя пустое, начинается с цифры,
    совпадает с ключевым словом или не является валидным идентификатором.
    ``context`` попадает в текст ошибки, чтобы было понятно, что именно чинить.
    """
    if not name:
        raise NamespaceCollisionError(f"{context}: пустое имя после нормализации")
    if name[0].isdigit():
        raise NamespaceCollisionError(
            f"{context}: имя {name!r} начинается с цифры. "
            f"Числовой суффикс не добавляется автоматически — задайте python_path в manifest"
        )
    if not name.isidentifier():
        raise NamespaceCollisionError(f"{context}: {name!r} не является Python-идентификатором")
    if keyword.iskeyword(name) or name in _SOFT_KEYWORDS:
        raise NamespaceCollisionError(
            f"{context}: {name!r} — ключевое слово Python. Задайте python_path в manifest"
        )
    if name.startswith("_"):
        raise NamespaceCollisionError(
            f"{context}: {name!r} начинается с подчёркивания и не может быть публичным атрибутом"
        )
    return name


def python_path_from_key(key: str) -> tuple[str, ...]:
    """Вывести Python path операции из стабильного ключа manifest.

    ``"ws2.addTicket"`` → ``("ws2", "add_ticket")``.

    Путь строится из **ключа**, а не из ``operationId``: если бэкенд переименовал
    ``operationId``, падает manifest binding, но публичное Python-имя не меняется само.
    """
    if not key:
        raise NamespaceCollisionError("пустой ключ операции")
    segments = key.split(".")
    if len(segments) < 2:
        raise NamespaceCollisionError(
            f"ключ операции {key!r} должен содержать хотя бы один namespace, "
            f"например 'api.getThing'"
        )
    path: list[str] = []
    for index, segment in enumerate(segments):
        converted = to_snake_case(segment)
        path.append(python_identifier(converted, context=f"ключ {key!r}, сегмент #{index}"))
    # Проверяется каждый сегмент, а не только последний: имя пространства имён
    # становится атрибутом того же generated-объекта, что и операция, поэтому
    # ключ вида ``by_key.getThing`` затирал бы метод ``operations.by_key``.
    check_namespace_names(path, context=f"ключ {key!r}")
    return tuple(path)


def check_namespace_names(path: list[str] | tuple[str, ...], *, context: str) -> None:
    """Проверить, что ни один сегмент Python path не занимает зарезервированное имя.

    Бросает :class:`NamespaceCollisionError` с указанием сегмента: молчаливое
    переименование запрещено, чинится только явным ``python_path`` в manifest.
    """
    for name in path:
        if name in RESERVED_NAMESPACE_ATTRIBUTES:
            raise NamespaceCollisionError(
                f"{context}: имя {name!r} зарезервировано публичным API namespace. "
                f"Задайте python_path в manifest"
            )


def slugify_key(key: str) -> str:
    """Имя файла для операции: ``"ws2.addTicket"`` → ``"ws2__add_ticket"``.

    Разделитель ``__`` выбран так, чтобы ключи ``a.b_c`` и ``a_b.c`` давали разные слаги.
    """
    return "__".join(to_snake_case(segment) for segment in key.split("."))


def d42_schema_name(name: str) -> str:
    """Имя переменной generated d42-схемы.

    ``"TicketQueueDto"`` → ``"GeneratedTicketQueueDtoSchema"``. Суффикс ``Schema``
    обязателен: по нему generated-схемы отличают линтеры потребительских проектов.
    """
    return f"Generated{to_pascal_case(name)}Schema"

"""Тесты детерминированных правил именования.

Публичное Python-имя операции — часть контракта библиотеки с потребителем:
переименование ломает чужой код. Поэтому здесь проверяется не «примерно
snake_case», а каждое документированное правило по отдельности, и отдельно —
что ни одна коллизия не разрешается молчаливым числовым суффиксом.
"""

from __future__ import annotations

import keyword

import pytest

from geas.errors import NamespaceCollisionError
from geas.naming import (
    RESERVED_NAMESPACE_ATTRIBUTES,
    d42_schema_name,
    python_identifier,
    python_path_from_key,
    slugify_key,
    to_pascal_case,
    to_snake_case,
)

#: Пары «исходное имя → snake_case» из докстринги :func:`to_snake_case`.
SNAKE_CASES = [
    # Граница «строчная → заглавная».
    ("addTicket", "add_ticket"),
    ("aB", "a_b"),
    # Аббревиатура: разрыв делается перед последней заглавной, а не после первой.
    ("HTTPServer", "http_server"),
    ("getHTTPResponse", "get_http_response"),
    ("IOError", "io_error"),
    ("userID", "user_id"),
    # Цифры не отрываются от предшествующих букв.
    ("ws2", "ws2"),
    ("oauth2", "oauth2"),
    ("HTTP2Server", "http2_server"),
    # ...но цифра перед заглавной — это граница слова.
    ("v2Get", "v2_get"),
    ("a1B2", "a1_b2"),
    # Пунктуация и пробелы схлопываются в один разделитель.
    ("get-item", "get_item"),
    ("X-Trace-Id", "x_trace_id"),
    ("list  documents", "list_documents"),
    ("weird...name", "weird_name"),
    # Крайние подчёркивания убираются.
    ("__leading__", "leading"),
    ("_x_", "x"),
    ("---", ""),
    # Уже нормализованное имя не меняется.
    ("already_snake", "already_snake"),
    ("a", "a"),
    ("A", "a"),
    ("", ""),
    # Имя, начинающееся с цифры, остаётся таким — это ловит python_identifier.
    ("2fa", "2fa"),
]


@pytest.mark.parametrize(("name", "expected"), SNAKE_CASES)
def test_to_snake_case_follows_documented_rules(name: str, expected: str) -> None:
    assert to_snake_case(name) == expected


@pytest.mark.parametrize(("name", "expected"), SNAKE_CASES)
def test_to_snake_case_is_idempotent(name: str, expected: str) -> None:
    """Повторное применение ничего не меняет: результат — неподвижная точка."""
    assert to_snake_case(expected) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("addTicket", "AddTicket"),
        ("add_ticket", "AddTicket"),
        ("HTTPServer", "HttpServer"),
        ("getHTTPResponse", "GetHttpResponse"),
        ("ws2", "Ws2"),
        ("oauth2", "Oauth2"),
        ("v2Get", "V2Get"),
        ("get-item", "GetItem"),
        ("TicketQueueDto", "TicketQueueDto"),
        ("", ""),
    ],
)
def test_to_pascal_case(name: str, expected: str) -> None:
    assert to_pascal_case(name) == expected


@pytest.mark.parametrize(("name", "_expected"), SNAKE_CASES)
def test_pascal_case_has_no_separators(name: str, _expected: str) -> None:
    """PascalCase собирается из тех же слов, что и snake_case, но без разделителей."""
    assert to_pascal_case(name) == "".join(
        part[:1].upper() + part[1:] for part in to_snake_case(name).split("_") if part
    )


# ------------------------------------------------------------ идентификаторы


@pytest.mark.parametrize(
    "name", ["add_ticket", "ws2", "oauth2", "x", "v2_get", "get_http_response"]
)
def test_python_identifier_accepts_normalized_names(name: str) -> None:
    assert python_identifier(name, context="проверка") == name


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("", "пустое имя после нормализации"),
        ("2fa", "начинается с цифры"),
        ("class", "ключевое слово Python"),
        ("import", "ключевое слово Python"),
        # Soft keywords: формально идентификаторы, но занимать их нельзя.
        ("match", "ключевое слово Python"),
        ("case", "ключевое слово Python"),
        ("type", "ключевое слово Python"),
        ("_", "ключевое слово Python"),
        ("a-b", "не является Python-идентификатором"),
        ("a b", "не является Python-идентификатором"),
        ("_private", "начинается с подчёркивания"),
        ("__dunder__", "начинается с подчёркивания"),
    ],
)
def test_python_identifier_rejects(name: str, why: str) -> None:
    with pytest.raises(NamespaceCollisionError) as info:
        python_identifier(name, context="контекст-маркер")
    message = str(info.value)
    assert why in message
    # Контекст обязан попасть в текст: иначе непонятно, что именно чинить.
    assert "контекст-маркер" in message


@pytest.mark.parametrize("word", sorted(keyword.kwlist))
def test_python_identifier_rejects_every_keyword(word: str) -> None:
    with pytest.raises(NamespaceCollisionError):
        python_identifier(word, context="ключевое слово")


# -------------------------------------------------------------- python path


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("ws2.addTicket", ("ws2", "add_ticket")),
        ("api.createDocument", ("api", "create_document")),
        ("api.workspaces.listDocuments", ("api", "workspaces", "list_documents")),
        ("api.get-item", ("api", "get_item")),
        ("v2.v2Get", ("v2", "v2_get")),
    ],
)
def test_python_path_from_key(key: str, expected: tuple[str, ...]) -> None:
    assert python_path_from_key(key) == expected


@pytest.mark.parametrize(
    ("key", "why"),
    [
        ("", "пустой ключ операции"),
        # Ключ без namespace: имя операции обязано жить внутри пространства имён.
        ("addTicket", "должен содержать хотя бы один namespace"),
        ("ws.class", "ключевое слово Python"),
        ("ws.match", "ключевое слово Python"),
        ("ws.2fa", "начинается с цифры"),
        ("ws.by_key", "зарезервировано публичным API namespace"),
        ("ws..x", "пустое имя после нормализации"),
        ("ws.!", "пустое имя после нормализации"),
        (".x", "пустое имя после нормализации"),
    ],
)
def test_python_path_from_key_rejects(key: str, why: str) -> None:
    with pytest.raises(NamespaceCollisionError) as info:
        python_path_from_key(key)
    assert why in str(info.value)


@pytest.mark.parametrize("attribute", sorted(RESERVED_NAMESPACE_ATTRIBUTES))
def test_reserved_attribute_rejected_as_leaf(attribute: str) -> None:
    with pytest.raises(NamespaceCollisionError, match="зарезервировано"):
        python_path_from_key(f"api.{attribute}")


@pytest.mark.parametrize("attribute", sorted(RESERVED_NAMESPACE_ATTRIBUTES))
def test_reserved_attribute_rejected_as_namespace(attribute: str) -> None:
    """Пространство имён живёт в том же namespace, что и методы generated-объекта.

    Ключ ``by_key.getThing`` дал бы класс с ``@property by_key`` и методом
    ``by_key(key)`` одновременно — второе определение молча затирает первое.
    """
    with pytest.raises(NamespaceCollisionError, match="зарезервировано"):
        python_path_from_key(f"{attribute}.getThing")


def test_python_path_does_not_invent_numeric_suffixes() -> None:
    """Коллизия — это ошибка, а не ``get_item_2``."""
    assert python_path_from_key("api.getItem") == python_path_from_key("api.get_item")


# -------------------------------------------------------------------- слаги


def test_slugify_key() -> None:
    assert slugify_key("ws2.addTicket") == "ws2__add_ticket"
    assert slugify_key("api.workspaces.listDocuments") == "api__workspaces__list_documents"


def test_slugify_distinguishes_dot_from_underscore() -> None:
    """Разделитель ``__`` выбран именно ради этой пары ключей."""
    assert slugify_key("a.b_c") != slugify_key("a_b.c")
    assert slugify_key("a.b_c") == "a__b_c"
    assert slugify_key("a_b.c") == "a_b__c"


@pytest.mark.parametrize(
    "keys",
    [
        ("a.b_c", "a_b.c"),
        ("api.getItem", "api.get_item"),
        ("api.x", "api.y", "other.x"),
    ],
)
def test_slugs_are_unique_when_python_paths_are(keys: tuple[str, ...]) -> None:
    slugs = [slugify_key(key) for key in keys]
    paths = {python_path_from_key(key) for key in keys}
    # Разные Python path обязаны давать разные имена файлов.
    assert len(set(slugs)) >= len(paths)


# --------------------------------------------------------------- d42-имена


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("TicketQueueDto", "GeneratedTicketQueueDtoSchema"),
        ("document_page", "GeneratedDocumentPageSchema"),
        ("ws2.addTicket", "GeneratedWs2AddTicketSchema"),
        ("", "GeneratedSchema"),
    ],
)
def test_d42_schema_name(name: str, expected: str) -> None:
    assert d42_schema_name(name) == expected


@pytest.mark.parametrize("name", ["TicketQueueDto", "document_page", "get-item", "ws2"])
def test_d42_schema_name_is_identifier_with_required_suffix(name: str) -> None:
    """Суффикс ``Schema`` обязателен: по нему generated-схемы узнают линтеры."""
    generated = d42_schema_name(name)
    assert generated.startswith("Generated")
    assert generated.endswith("Schema")
    assert generated.isidentifier()
    assert not keyword.iskeyword(generated)

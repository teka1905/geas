"""Тесты грамматики contract path.

Contract path — стабильный адрес точки внутри контракта, на который ссылаются
waiver'ы и ``non_waivable``. Грамматика обязана быть обратимой: то, что
библиотека напечатала в сообщении об ошибке, потребитель копирует в
``waivers.yaml``, и разбор должен дать ровно тот же путь.
"""

from __future__ import annotations

import pytest

from geas.errors import ContractError
from geas.paths import (
    ADDITIONAL_PROPERTIES,
    ARRAY_ITEMS,
    ContractPath,
    escape_property,
    format_contract_path,
    is_variant_segment,
    parse_contract_path,
    variant_index,
    variant_segment,
)

#: Пары «строковая форма ↔ кортеж сегментов», обратимые в обе стороны.
ROUND_TRIPS: list[tuple[str, ContractPath]] = [
    ("/", ()),
    ("/body", ("body",)),
    ("/body/payload", ("body", "payload")),
    ("/groups/-/title", ("groups", ARRAY_ITEMS, "title")),
    ("/body/+", ("body", ADDITIONAL_PROPERTIES)),
    ("/labels/+/value", ("labels", ADDITIONAL_PROPERTIES, "value")),
    ("/payload/#0", ("payload", "#0")),
    ("/payload/#1/id", ("payload", "#1", "id")),
    ("/payload/#10", ("payload", "#10")),
    ("/param/query/status", ("param", "query", "status")),
    # RFC 6901: '/' в имени свойства — это ~1, '~' — это ~0.
    ("/a~1b", ("a/b",)),
    ("/a~0b", ("a~b",)),
    ("/~0~1", ("~/",)),
    # Свойство с именем, похожим на служебный сегмент, но не совпадающим с ним.
    ("/#x", ("#x",)),
    ("/--", ("--",)),
]


@pytest.mark.parametrize(("text", "path"), ROUND_TRIPS)
def test_parse_contract_path(text: str, path: ContractPath) -> None:
    assert parse_contract_path(text) == path


@pytest.mark.parametrize(("text", "path"), ROUND_TRIPS)
def test_format_contract_path(text: str, path: ContractPath) -> None:
    assert format_contract_path(path) == text


@pytest.mark.parametrize(("text", "path"), ROUND_TRIPS)
def test_round_trip_is_stable(text: str, path: ContractPath) -> None:
    assert parse_contract_path(format_contract_path(path)) == path
    assert format_contract_path(parse_contract_path(text)) == text


@pytest.mark.parametrize("text", ["", "/"])
def test_empty_and_slash_mean_root(text: str) -> None:
    assert parse_contract_path(text) == ()
    assert format_contract_path(()) == "/"


# ------------------------------------------------------------ экранирование


@pytest.mark.parametrize(
    ("name", "escaped"),
    [
        ("plain", "plain"),
        ("a/b", "a~1b"),
        ("a~b", "a~0b"),
        ("/", "~1"),
        ("~", "~0"),
        # Служебные сегменты в роли имени свойства получают префикс ~2.
        (ARRAY_ITEMS, "~2-"),
        (ADDITIONAL_PROPERTIES, "~2+"),
        ("#0", "~2#0"),
        ("#12", "~2#12"),
        # '~2' в исходном имени сначала становится '~02' и потому не путается
        # с самим префиксом.
        ("~2-", "~02-"),
        ("~2x", "~02x"),
    ],
)
def test_escape_property(name: str, escaped: str) -> None:
    assert escape_property(name) == escaped


@pytest.mark.parametrize(
    "name",
    ["plain", "a/b", "a~b", "/", "~", ARRAY_ITEMS, ADDITIONAL_PROPERTIES, "#0", "~2-", "~2x", "#x"],
)
def test_escaped_property_parses_back_to_the_same_name(name: str) -> None:
    """Экранированное имя свойства разбирается обратно в исходное имя."""
    assert parse_contract_path("/" + escape_property(name)) == (name,)


@pytest.mark.parametrize("name", [ARRAY_ITEMS, ADDITIONAL_PROPERTIES, "#0"])
def test_service_segment_and_escaped_property_collapse_to_one_tuple(name: str) -> None:
    """Известное ограничение представления.

    Свойство с именем ``-`` и элементы массива — это один и тот же сегмент
    кортежа, поэтому ``/~2-`` и ``/-`` разбираются одинаково, а печатается
    короткая форма. Практического расхождения нет: имена свойств ``-``, ``+`` и
    ``#0`` в реальных спецификациях не встречаются, но поведение зафиксировано
    осознанно, а не «как получилось».
    """
    escaped = parse_contract_path("/" + escape_property(name))
    assert escaped == parse_contract_path("/" + name)
    assert format_contract_path(escaped) == "/" + name


# ---------------------------------------------------------------- отказы


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("body", "должен начинаться с '/'"),
        ("body/payload", "должен начинаться с '/'"),
        ("~2-", "должен начинаться с '/'"),
        ("//", "пустой сегмент"),
        ("/a//b", "пустой сегмент"),
        ("/a/", "пустой сегмент"),
        # Экранирующий префикс без имени — тоже пустой сегмент.
        ("/~2", "префикс '~2' стоит без имени свойства"),
        ("/a/~2", "префикс '~2' стоит без имени свойства"),
    ],
)
def test_parse_contract_path_rejects(text: str, why: str) -> None:
    with pytest.raises(ContractError) as info:
        parse_contract_path(text)
    assert why in str(info.value)


def test_empty_property_name_is_not_representable() -> None:
    """Свойство с пустым именем не выражается грамматикой ни в одну сторону."""
    assert escape_property("") == ""
    with pytest.raises(ContractError, match="пустой сегмент"):
        parse_contract_path("/a/" + escape_property(""))


# --------------------------------------------------------------- варианты


@pytest.mark.parametrize("index", [0, 1, 7, 42])
def test_variant_segment_round_trip(index: int) -> None:
    segment = variant_segment(index)
    assert segment == f"#{index}"
    assert is_variant_segment(segment)
    assert variant_index(segment) == index


@pytest.mark.parametrize(
    "segment", [ARRAY_ITEMS, ADDITIONAL_PROPERTIES, "#", "#x", "# 1", "#-1", "name", ""]
)
def test_non_variant_segments(segment: str) -> None:
    assert not is_variant_segment(segment)
    with pytest.raises(ContractError, match="не является указателем на вариант"):
        variant_index(segment)

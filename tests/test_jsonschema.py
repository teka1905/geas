"""Generated JSON Schema: точность трансляции и независимая валидация.

JSON Schema — это второй, независимый от d42 путь проверки, и именно он держит
точную семантику ``oneOf``, ``discriminator``, ``format`` и границ. Поэтому здесь
проверяется не «что-то сгенерировалось», а конкретные ключевые слова: OpenAPI 3.0
и Draft 2020-12 совпадают не полностью, и каждое расхождение транслируется явно.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openapi_contracts.errors import RefResolutionError, ResponseContractError
from openapi_contracts.jsonschema_gen import JSON_SCHEMA_DIALECT, to_json_schema
from openapi_contracts.models import (
    AdditionalProperties,
    ArrayNode,
    BooleanNode,
    Direction,
    Discriminator,
    IntegerNode,
    NumberNode,
    ObjectNode,
    Origin,
    PropertySpec,
    RefNode,
    StringNode,
    UnionKind,
    UnionNode,
)
from openapi_contracts.runtime.validation import validate_instance
from support import make_project, spec

ORIGIN = Origin(source="spec.yaml", pointer="")


def _schema(node: Any, definitions: tuple[tuple[str, Any], ...] = ()) -> dict[str, Any]:
    return to_json_schema(node, definitions)


def _validate(schema: dict[str, Any], value: Any) -> None:
    validate_instance(
        schema, value, operation_key="demo.op", direction=Direction.RESPONSE, part="тело"
    )


# --------------------------------------------------------------- трансляция


def test_document_declares_the_dialect() -> None:
    """Каждый generated-документ называет свой диалект явно."""
    assert _schema(StringNode(origin=ORIGIN))["$schema"] == JSON_SCHEMA_DIALECT


def test_nullable_scalar_becomes_a_type_union() -> None:
    """``nullable: true`` — это ``type: [T, "null"]``, а не отдельный ключ."""
    assert _schema(StringNode(origin=ORIGIN, nullable=True))["type"] == ["string", "null"]


def test_nullable_enum_gains_null() -> None:
    """У nullable-enum ``null`` обязан появиться и в ``enum``, иначе он невалиден."""
    schema = _schema(StringNode(origin=ORIGIN, nullable=True, enum=("a", "b")))

    assert schema["enum"] == ["a", "b", None]
    _validate(schema, None)
    _validate(schema, "a")
    with pytest.raises(ResponseContractError):
        _validate(schema, "c")


def test_nullable_ref_is_wrapped_in_any_of() -> None:
    """К ``$ref`` нельзя дописать ``"null"`` в ``type`` — нужна обёртка."""
    definitions = (("Thing", ObjectNode(origin=ORIGIN)),)
    schema = _schema(RefNode(origin=ORIGIN, name="Thing", nullable=True), definitions)

    assert schema["anyOf"] == [{"$ref": "#/$defs/Thing"}, {"type": "null"}]


def test_defs_contain_only_reachable_definitions() -> None:
    """В ``$defs`` попадает только то, что достижимо из корня."""
    used = ObjectNode(origin=ORIGIN)
    definitions = (("Used", used), ("Unused", ObjectNode(origin=ORIGIN)))
    schema = _schema(RefNode(origin=ORIGIN, name="Used"), definitions)

    assert set(schema["$defs"]) == {"Used"}


def test_mutually_recursive_definitions_are_all_reachable() -> None:
    """Взаимная рекурсия не должна терять половину графа."""
    left = ObjectNode(
        origin=ORIGIN,
        properties=(
            PropertySpec(name="right", schema=RefNode(origin=ORIGIN, name="Right"), required=False),
        ),
    )
    right = ObjectNode(
        origin=ORIGIN,
        properties=(
            PropertySpec(name="left", schema=RefNode(origin=ORIGIN, name="Left"), required=False),
        ),
    )
    schema = _schema(RefNode(origin=ORIGIN, name="Left"), (("Left", left), ("Right", right)))

    assert set(schema["$defs"]) == {"Left", "Right"}


def test_additional_properties_false_is_preserved() -> None:
    """``additionalProperties: false`` — часть контракта, а не рекомендация."""
    node = ObjectNode(origin=ORIGIN, additional_properties=AdditionalProperties.FORBIDDEN)

    assert _schema(node)["additionalProperties"] is False


def test_one_of_is_never_replaced_by_any_of() -> None:
    """``oneOf`` остаётся ``oneOf``: эксклюзивность — часть контракта."""
    node = UnionNode(
        origin=ORIGIN,
        kind=UnionKind.ONE_OF,
        variants=(StringNode(origin=ORIGIN), IntegerNode(origin=ORIGIN)),
    )
    schema = _schema(node)

    assert "oneOf" in schema
    assert "anyOf" not in schema


def test_discriminator_becomes_a_real_constraint() -> None:
    """Дискриминатор с mapping разворачивается в ``if``/``then``, а не в подсказку."""
    alpha = ObjectNode(
        origin=ORIGIN,
        properties=(
            PropertySpec(name="kind", schema=StringNode(origin=ORIGIN), required=True),
            PropertySpec(name="alpha", schema=IntegerNode(origin=ORIGIN), required=True),
        ),
    )
    beta = ObjectNode(
        origin=ORIGIN,
        properties=(
            PropertySpec(name="kind", schema=StringNode(origin=ORIGIN), required=True),
            PropertySpec(name="beta", schema=StringNode(origin=ORIGIN), required=True),
        ),
    )
    node = UnionNode(
        origin=ORIGIN,
        kind=UnionKind.ONE_OF,
        variants=(RefNode(origin=ORIGIN, name="Alpha"), RefNode(origin=ORIGIN, name="Beta")),
        discriminator=Discriminator(property_name="kind", mapping=(("a", "Alpha"), ("b", "Beta"))),
    )
    schema = _schema(node, (("Alpha", alpha), ("Beta", beta)))

    assert schema["required"] == ["kind"]
    assert len(schema["allOf"]) == 2

    _validate(schema, {"kind": "a", "alpha": 1})
    # kind="a" обязывает соответствовать Alpha — beta-форма не подойдёт.
    with pytest.raises(ResponseContractError):
        _validate(schema, {"kind": "a", "beta": "x"})


def test_rendering_is_deterministic() -> None:
    """Один и тот же IR даёт идентичный JSON Schema."""
    node = ObjectNode(
        origin=ORIGIN,
        properties=(
            PropertySpec(name="b", schema=IntegerNode(origin=ORIGIN, minimum=0), required=True),
            PropertySpec(name="a", schema=StringNode(origin=ORIGIN), required=False),
        ),
    )

    assert _schema(node) == _schema(node)


# --------------------------------------------------------------- валидация


@pytest.mark.parametrize(
    ("node", "good", "bad", "validator"),
    [
        (StringNode(origin=ORIGIN, min_length=2), "ab", "a", "minLength"),
        (StringNode(origin=ORIGIN, max_length=2), "ab", "abc", "maxLength"),
        (StringNode(origin=ORIGIN, pattern="^[a-z]+$"), "abc", "ABC", "pattern"),
        (IntegerNode(origin=ORIGIN, minimum=3), 3, 2, "minimum"),
        (IntegerNode(origin=ORIGIN, exclusive_minimum=3), 4, 3, "exclusiveMinimum"),
        (IntegerNode(origin=ORIGIN, maximum=3), 3, 4, "maximum"),
        (IntegerNode(origin=ORIGIN, multiple_of=5), 10, 11, "multipleOf"),
        (NumberNode(origin=ORIGIN, minimum=1.5), 1.5, 1.0, "minimum"),
        (BooleanNode(origin=ORIGIN), True, "true", "type"),
        (
            ArrayNode(origin=ORIGIN, items=IntegerNode(origin=ORIGIN), min_items=2),
            [1, 2],
            [1],
            "minItems",
        ),
        (
            ArrayNode(origin=ORIGIN, items=IntegerNode(origin=ORIGIN), unique_items=True),
            [1, 2],
            [1, 1],
            "uniqueItems",
        ),
    ],
)
def test_each_keyword_is_enforced(node: Any, good: Any, bad: Any, validator: str) -> None:
    """Каждое перенесённое ключевое слово действительно проверяется."""
    schema = _schema(node)
    _validate(schema, good)

    with pytest.raises(ResponseContractError) as info:
        _validate(schema, bad)

    assert info.value.validator == validator
    assert info.value.json_pointer is not None


def test_format_is_checked() -> None:
    """Проверка ``format`` включена, а не просто записана в артефакт."""
    schema = _schema(StringNode(origin=ORIGIN, format="uuid"))
    _validate(schema, "3f5b0c1e-0000-4000-8000-000000000001")

    with pytest.raises(ResponseContractError) as info:
        _validate(schema, "не-uuid")

    assert info.value.validator == "format"


def test_error_points_at_the_exact_place() -> None:
    """Ошибка несёт указатель внутрь значения и указатель внутрь схемы."""
    node = ObjectNode(
        origin=ORIGIN,
        properties=(
            PropertySpec(
                name="items",
                schema=ArrayNode(origin=ORIGIN, items=IntegerNode(origin=ORIGIN, minimum=0)),
                required=True,
            ),
        ),
    )

    with pytest.raises(ResponseContractError) as info:
        _validate(_schema(node), {"items": [1, -5]})

    assert info.value.json_pointer == "/items/1"
    assert info.value.schema_pointer is not None
    assert info.value.actual == "-5"


def test_external_ref_never_reaches_the_network() -> None:
    """Внешний ``$ref`` в схеме — ошибка контракта, а не HTTP-запрос."""
    schema = {
        "$schema": JSON_SCHEMA_DIALECT,
        "$ref": "https://example.invalid/schema.json",
    }

    with pytest.raises(RefResolutionError) as info:
        _validate(schema, {})

    assert "сеть" in str(info.value)
    assert "example.invalid" in str(info.value)


def test_generated_contract_validates_a_real_payload(tmp_path: Path) -> None:
    """Сквозная проверка: сгенерированная схема принимает корректное тело."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("basic", "openapi30.yaml"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={"api.createDocument": {"source": "main", "operation_id": "createDocument"}},
    )
    built = project.build().by_key("api.createDocument")
    body = next(item for item in built.document["request"]["bodies"])

    validate_instance(
        body["schema"],
        {"title": "Черновик", "slug": "chernovik", "notifyMembers": False},
        operation_key="api.createDocument",
        direction=Direction.REQUEST,
        part="тело запроса",
    )

"""``type: number`` в d42: целые и дробные значения, как в JSON Schema.

Фикстура — ``features/numbers.yaml``: ``Variable`` с полем на каждое ограничение
``number`` и типизированным значением ``oneOf`` (``NUMBER`` / ``NUMBER_LIST`` /
``STRING``) с ``discriminator``.

Контракт проверки: d42 принимает и отклоняет ровно то же, что JSON Schema
контракта, — целые и дробные в границах, без ``bool``; ``fake()``, ``%``,
``make_required()`` и ``overlay_generators`` работают на результате.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from d42 import ValidationException, fake, schema, validate_or_fail
from d42.declaration.types import FloatSchema, IntSchema
from d42.substitution.errors import SubstitutionError
from d42.utils import make_required

from geas import Direction
from geas.errors import ContractOverlayError, ValidationFailedError
from geas.integrations.d42 import (
    EACH,
    JsonIntSchema,
    build_fixture,
    json_int,
    overlay_generators,
    render_module,
    to_d42,
)
from geas.integrations.d42.fixtures import validate_overlay_fixture
from geas.models import IntegerNode, NumberNode, Origin
from support import Project, make_project, spec

ORIGIN = Origin(source="numbers.yaml", pointer="/components/schemas/Demo")


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    built = make_project(tmp_path_factory.mktemp("numbers"), package="number_contracts")
    built.write_spec("api/spec.yaml", spec("features", "numbers.yaml"))
    built.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})
    built.update()
    return built


@pytest.fixture(scope="module")
def operation(project: Project) -> Iterator[Any]:
    with project.importable() as generated:
        yield generated.operations.main.add_variable


@pytest.fixture(scope="module")
def variable(operation: Any) -> Any:
    return operation.d42_schema(Direction.RESPONSE)


def accepts(d42_schema: Any, value: Any) -> bool:
    try:
        validate_or_fail(d42_schema, value)
    except ValidationException:
        return False
    return True


def number_value(value: Any) -> dict[str, Any]:
    return {"name": "limit", "value": {"type": "NUMBER", "value": value}}


def required_scalars(d42_schema: Any) -> Any:
    """Все ключи обязательны, кроме ``ranks``.

    ``fake()`` берёт случайную длину списка, а уникальных значений в ``enum`` ранков
    всего два — генератор d42 падает и без правки ``number`` (см. шапку ``fixtures``).
    """
    scalars = ["count", "fraction", "level", "name", "ratio", "score", "value", "weight"]
    return make_required(d42_schema, scalars)


def variable_with(**fields: Any) -> dict[str, Any]:
    return {"name": "limit", "value": {"type": "STRING", "value": "x"}, **fields}


# ------------------------------------------------------------ IR → d42


def test_number_accepts_integers_and_fractions() -> None:
    number = to_d42(NumberNode(origin=ORIGIN), {})

    assert number == schema.any(schema.float, json_int)
    assert accepts(number, 7)
    assert accepts(number, 7.5)
    assert accepts(number, -0.0)


@pytest.mark.parametrize("value", [True, False, "7", None, [7]])
def test_number_rejects_non_numbers_including_bool(value: Any) -> None:
    """``bool`` в Python — ``int``, но для JSON Schema это не число."""
    assert not accepts(to_d42(NumberNode(origin=ORIGIN), {}), value)


def test_bounds_apply_to_both_branches_rounded_inward() -> None:
    number = to_d42(NumberNode(origin=ORIGIN, minimum=0.5, maximum=2.5), {})

    assert number == schema.any(schema.float.min(0.5).max(2.5), json_int.min(1).max(2))
    for value in (0.5, 1, 2, 2.5, 1.75):
        assert accepts(number, value), value
    for value in (0, 3, 0.49, 2.51, -1):
        assert not accepts(number, value), value


def test_integer_bounds_keep_their_integers() -> None:
    number = to_d42(NumberNode(origin=ORIGIN, minimum=0, maximum=10), {})

    assert number == schema.any(schema.float.min(0.0).max(10.0), json_int.min(0).max(10))
    assert accepts(number, 0)
    assert accepts(number, 10)
    assert not accepts(number, 11)
    assert not accepts(number, -1)


def test_one_sided_bound_stays_one_sided() -> None:
    number = to_d42(NumberNode(origin=ORIGIN, minimum=-1.5), {})

    assert number == schema.any(schema.float.min(-1.5), json_int.min(-1))
    assert accepts(number, 10**30)
    assert not accepts(number, -2)


def test_range_without_integers_keeps_only_the_float_branch() -> None:
    number = to_d42(NumberNode(origin=ORIGIN, minimum=0.1, maximum=0.9), {})

    assert number == schema.float.min(0.1).max(0.9)


def test_degenerate_integer_range_keeps_the_integer() -> None:
    number = to_d42(NumberNode(origin=ORIGIN, minimum=2, maximum=2), {})

    assert number == schema.any(schema.float.min(2.0).max(2.0), json_int.min(2).max(2))
    assert accepts(number, 2)
    assert accepts(number, 2.0)


def test_enum_literals_compare_by_value() -> None:
    """``1`` и ``1.0`` — одно значение ``enum``; float-литералы идут первыми."""
    level = to_d42(NumberNode(origin=ORIGIN, enum=(0.5, 1, 2.0)), {})

    assert level == schema.any(
        schema.float(0.5), schema.float(1.0), schema.float(2.0), json_int(1), json_int(2)
    )
    for value in (0.5, 1, 1.0, 2, 2.0):
        assert accepts(level, value), value
    for value in (True, 0, 1.5, 3):
        assert not accepts(level, value), value


def test_enum_duplicates_by_value_collapse() -> None:
    assert to_d42(NumberNode(origin=ORIGIN, enum=(1, 1.0)), {}) == schema.any(
        schema.float(1.0), json_int(1)
    )


def test_fractional_enum_has_no_integer_literals() -> None:
    assert to_d42(NumberNode(origin=ORIGIN, enum=(0.25,)), {}) == schema.float(0.25)


def test_nullable_number_adds_none_after_both_branches() -> None:
    score = to_d42(NumberNode(origin=ORIGIN, nullable=True), {})

    assert score == schema.any(schema.float, json_int, schema.none)
    assert accepts(score, None)
    assert accepts(score, 3)


def test_integer_is_unchanged() -> None:
    count = to_d42(IntegerNode(origin=ORIGIN, minimum=0), {})

    assert type(count) is IntSchema
    assert count == schema.int.min(0)


# ------------------------------------------------------------ json_int


def test_json_int_survives_chaining_and_substitution() -> None:
    bounded = json_int.min(0).max(5)

    assert isinstance(bounded, JsonIntSchema)
    assert isinstance(json_int(3), JsonIntSchema)
    assert isinstance(bounded % 3, JsonIntSchema)
    with pytest.raises(SubstitutionError):
        bounded % True


def test_json_int_represents_itself() -> None:
    assert repr(json_int.min(0).max(5)) == "json_int.min(0).max(5)"
    assert repr(json_int(3)) == "json_int(3)"


# ------------------------------------------------------------ рендер


def test_rendered_module_imports_json_int_and_matches_to_d42() -> None:
    node = NumberNode(origin=ORIGIN, minimum=0.5, maximum=2.5)
    source = render_module(module_docstring="d", definitions={}, exports={"RatioSchema": node})
    namespace: dict[str, Any] = {}
    exec(compile(source, "<rendered>", "exec"), namespace)

    assert "from geas.integrations.d42.json_int import json_int\n" in source
    assert namespace["RatioSchema"] == to_d42(node, {})
    assert isinstance(namespace["RatioSchema"].props.types[1], JsonIntSchema)


def test_module_without_number_does_not_import_json_int() -> None:
    node = IntegerNode(origin=ORIGIN)
    source = render_module(module_docstring="d", definitions={}, exports={"CountSchema": node})

    assert "json_int" not in source


def test_contract_json_schema_is_unchanged(project: Project) -> None:
    """Правка — только d42-проекция: документ контракта ``number`` не трогает."""
    document = project.contract_document("main__add_variable")
    properties = document["responses"][0]["schema"]["$defs"]["Variable"]["properties"]

    assert properties["ratio"] == {"type": "number", "minimum": 0.5, "maximum": 2.5}
    assert properties["level"] == {"type": "number", "enum": [0.5, 1.0, 2.0]}


# ------------------------------------------------------------ сквозь контракт


@pytest.mark.parametrize("value", [7, 7.5, 0, -3, 10**20])
def test_integer_or_fraction_in_number_passes_both_paths(
    value: Any, operation: Any, variable: Any
) -> None:
    body = number_value(value)

    operation.validate_response(body, status=200)
    operation.validate_request_body(body)
    validate_or_fail(variable, body)


def test_number_list_mixes_integers_and_fractions(operation: Any, variable: Any) -> None:
    body = {"name": "limits", "value": {"type": "NUMBER_LIST", "value": [1, 2.5, 0]}}

    operation.validate_response(body, status=200)
    validate_or_fail(variable, body)


VALID_FIELDS = {
    "ratio целое": {"ratio": 2},
    "ratio дробное на границе": {"ratio": 0.5},
    "fraction дробное": {"fraction": 0.5},
    "weight целое": {"weight": 0},
    "level целое": {"level": 1},
    "level дробное": {"level": 0.5},
    "score null": {"score": None},
    "score целое": {"score": 3},
    "ranks целые": {"ranks": [1, 2]},
    "ranks вперемешку": {"ranks": [1.0, 2]},
}


@pytest.mark.parametrize("fields", list(VALID_FIELDS.values()), ids=list(VALID_FIELDS))
def test_valid_numbers_pass_d42(fields: dict[str, Any], operation: Any, variable: Any) -> None:
    body = variable_with(**fields)

    operation.validate_response(body, status=200)
    validate_or_fail(variable, body)


INVALID_FIELDS = {
    "value true": number_value(True),
    "ratio ниже границы": variable_with(ratio=0),
    "ratio выше границы": variable_with(ratio=3),
    "fraction целое": variable_with(fraction=1),
    "weight отрицательное": variable_with(weight=-1),
    "level вне enum": variable_with(level=3),
    "level true": variable_with(level=True),
    "score строкой": variable_with(score="3"),
}


@pytest.mark.parametrize("body", list(INVALID_FIELDS.values()), ids=list(INVALID_FIELDS))
def test_invalid_numbers_fail_both_paths(
    body: dict[str, Any], operation: Any, variable: Any
) -> None:
    with pytest.raises(ValidationFailedError):
        operation.validate_response(body, status=200)
    with pytest.raises(ValidationException):
        validate_or_fail(variable, body)


def test_duplicate_by_value_in_unique_list_fails_both_paths(operation: Any, variable: Any) -> None:
    """``uniqueItems`` считает ``1`` и ``1.0`` одним значением — и JSON Schema, и d42."""
    body = variable_with(ranks=[1, 1.0])

    with pytest.raises(ValidationFailedError):
        operation.validate_response(body, status=200)
    with pytest.raises(ValidationException):
        validate_or_fail(variable, body)


# ------------------------------------------------------------ fake, %, make_required, overlay


def test_fake_stays_within_the_contract(operation: Any, variable: Any) -> None:
    full = required_scalars(variable)
    values = [fake(full) for _ in range(300)]

    for value in values:
        operation.validate_response(value, status=200)
    ratios = [value["ratio"] for value in values]
    assert {type(item) for item in ratios} == {int, float}
    assert all(0.5 <= item <= 2.5 for item in ratios)


def test_fixture_stays_fractional_and_valid(operation: Any, variable: Any) -> None:
    """Проекция берёт первый вариант — ``float``: закоммиченные фикстуры не сдвигаются."""
    value = build_fixture(make_required(variable), seed=7)

    operation.validate_response(value, status=200)
    assert isinstance(value["ratio"], float)
    assert isinstance(value["weight"], float)
    assert isinstance(value["level"], float)
    assert len(value["ranks"]) == 2


def test_substitution_pins_integers_and_fractions(variable: Any) -> None:
    pinned = variable % number_value(42)
    assert fake(pinned)["value"]["value"] == 42

    pinned = variable % number_value(42.5)
    assert fake(pinned)["value"]["value"] == 42.5

    pinned = variable % variable_with(ratio=2, level=1, ranks=[1, 2])
    generated = fake(pinned)
    assert (generated["ratio"], generated["level"], generated["ranks"]) == (2, 1, [1, 2])


@pytest.mark.parametrize(
    "fields", [{"ratio": True}, {"ratio": 3}, {"level": 3}], ids=["bool", "граница", "enum"]
)
def test_substitution_rejects_values_outside_the_contract(
    fields: dict[str, Any], variable: Any
) -> None:
    with pytest.raises(SubstitutionError):
        variable % variable_with(**fields)


@pytest.mark.parametrize("manual", [schema.int.min(1).max(2), schema.float.min(1.0).max(2.0)])
def test_overlay_accepts_either_numeric_kind(manual: Any, operation: Any, variable: Any) -> None:
    overlaid = required_scalars(overlay_generators(variable, {("ratio",): manual}))
    value = fake(overlaid)

    assert isinstance(value["ratio"], manual.type)
    operation.validate_response(value, status=200)


def test_overlay_on_number_list_items(variable: Any) -> None:
    typed_value, _ = variable.props.keys["value"]
    number_list = typed_value.props.types[1]
    overlaid = overlay_generators(number_list, {("value", EACH): schema.int.min(1).max(9)})

    assert all(isinstance(item, int) for item in fake(overlaid)["value"])


def test_overlay_rejects_another_type(variable: Any) -> None:
    with pytest.raises(ContractOverlayError, match="тип ручной схемы"):
        overlay_generators(variable, {("ratio",): schema.str})


def test_overlay_enum_accepts_literals_of_either_kind(variable: Any) -> None:
    assert overlay_generators(variable, {("level",): schema.int(1)}) is not None
    assert overlay_generators(variable, {("level",): schema.float(0.5)}) is not None
    with pytest.raises(ContractOverlayError, match="отсутствуют в enum"):
        overlay_generators(variable, {("level",): schema.int(3)})


def test_overlay_out_of_bounds_is_caught_by_the_contract(variable: Any) -> None:
    overlaid = overlay_generators(variable, {("ratio",): schema.int(7)})

    with pytest.raises(ContractOverlayError):
        validate_overlay_fixture(overlaid, variable_with(ratio=7))


def test_float_and_int_branch_types() -> None:
    """Порядок веток — часть контракта стабильности фикстур: ``float`` первым."""
    number = to_d42(NumberNode(origin=ORIGIN), {})

    assert [type(item) for item in number.props.types] == [FloatSchema, JsonIntSchema]

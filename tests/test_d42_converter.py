"""Конвертер IR → живые схемы d42 (:mod:`geas.integrations.d42.converter`).

Файл закрепляет два класса утверждений.

**Перевод.** Для каждого вида узла IR проверяется, что получившийся объект d42
равен ожидаемому — сравнение идёт по ``==``, то есть по значению схемы, а не по
её текстовому представлению. Так тест ловит и лишний ``.len(...)``, и потерянный
``optional``, и открытый словарь там, где контракт закрыт.

**Fail closed.** Каждое решение «эта конструкция в d42 невыразима» из шапки
модуля-конвертера закреплено отдельным тестом: проверяется и тип исключения, и
то, что в тексте назван конкретный keyword, contract path и координаты узла.
Молчаливая деградация в ``schema.any`` была бы страшнее исключения, поэтому её
отсутствие тоже часть контракта.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from d42 import ValidationException, optional, schema, validate_or_fail

from geas.errors import (
    ContractError,
    RecursiveSchemaError,
    RefResolutionError,
    UnsupportedConstructError,
)
from geas.integrations.d42.converter import to_d42
from geas.models import (
    AdditionalProperties,
    AllOfNode,
    AnyNode,
    ArrayNode,
    BooleanNode,
    IntegerNode,
    NullNode,
    NumberNode,
    ObjectNode,
    Origin,
    PropertySpec,
    RefNode,
    SchemaNode,
    StringNode,
    UnionKind,
    UnionNode,
)

#: Общий origin: провенанс проверяется отдельно, в остальных тестах он шумит.
ORIGIN = Origin(source="demo.yaml", pointer="/components/schemas/Demo")


def prop(name: str, node: SchemaNode, *, required: bool = True) -> PropertySpec:
    """Свойство объекта — короткая запись для читаемости тестов."""
    return PropertySpec(name=name, schema=node, required=required)


def obj(
    *properties: PropertySpec,
    closed: bool = False,
    nullable: bool = False,
) -> ObjectNode:
    """Объект IR: по умолчанию открытый, как и в OpenAPI без ``additionalProperties``."""
    return ObjectNode(
        origin=ORIGIN,
        nullable=nullable,
        properties=properties,
        additional_properties=(
            AdditionalProperties.FORBIDDEN if closed else AdditionalProperties.ALLOWED
        ),
    )


def union(*variants: SchemaNode, kind: UnionKind = UnionKind.ONE_OF, nullable: bool = False):
    """Композиция-объединение IR."""
    return UnionNode(origin=ORIGIN, nullable=nullable, kind=kind, variants=variants)


# ======================================================================== строки


def test_plain_string_has_no_constraints() -> None:
    assert to_d42(StringNode(origin=ORIGIN), {}) == schema.str


@pytest.mark.parametrize(
    ("format_name", "minimum", "maximum"),
    [
        ("int32", -(2**31), 2**31 - 1),
        ("int64", -(2**63), 2**63 - 1),
    ],
)
def test_integer_formats_have_real_d42_bounds(format_name: str, minimum: int, maximum: int) -> None:
    """int32/int64 — ограничения диапазона, а не декоративный format."""
    converted = to_d42(IntegerNode(origin=ORIGIN, format=format_name), {})

    assert converted == schema.int.min(minimum).max(maximum)


@pytest.mark.parametrize(
    ("minimum", "maximum", "expected"),
    [
        (1, 5, schema.str.len(1, 5)),
        (3, None, schema.str.len(3, ...)),
        (None, 7, schema.str.len(..., 7)),
        (4, 4, schema.str.len(4, 4)),
    ],
    ids=["both", "only-min", "only-max", "exact"],
)
def test_string_length_becomes_len_call(minimum, maximum, expected) -> None:
    node = StringNode(origin=ORIGIN, min_length=minimum, max_length=maximum)
    assert to_d42(node, {}) == expected


def test_string_pattern_becomes_regex() -> None:
    node = StringNode(origin=ORIGIN, pattern="^[a-z0-9-]+$")
    assert to_d42(node, {}) == schema.str.regex("^[a-z0-9-]+$")


def test_single_enum_value_becomes_literal() -> None:
    node = StringNode(origin=ORIGIN, enum=("draft",))
    assert to_d42(node, {}) == schema.str("draft")


def test_enum_becomes_union_of_literals_in_contract_order() -> None:
    node = StringNode(origin=ORIGIN, enum=("private", "workspace", "public"))
    assert to_d42(node, {}) == schema.any(
        schema.str("private"), schema.str("workspace"), schema.str("public")
    )


def test_enum_wins_over_length_constraints() -> None:
    """Литералы уже уже любого ``len``: дублировать ограничение незачем."""
    node = StringNode(origin=ORIGIN, enum=("ok",), min_length=1, max_length=8)
    assert to_d42(node, {}) == schema.str("ok")


# ========================================================================= числа


def test_integer_bounds_become_min_max() -> None:
    node = IntegerNode(origin=ORIGIN, minimum=1, maximum=100)
    assert to_d42(node, {}) == schema.int.min(1).max(100)


def test_integer_exclusive_bounds_convert_exactly() -> None:
    """У целых «строго больше N» разворачивается в ``N + 1`` без потери точности."""
    node = IntegerNode(origin=ORIGIN, exclusive_minimum=0, exclusive_maximum=10)
    assert to_d42(node, {}) == schema.int.min(1).max(9)


def test_integer_takes_the_tighter_of_two_bounds() -> None:
    node = IntegerNode(
        origin=ORIGIN,
        minimum=5,
        exclusive_minimum=9,
        maximum=100,
        exclusive_maximum=51,
    )
    assert to_d42(node, {}) == schema.int.min(10).max(50)


def test_integer_enum_becomes_int_literals() -> None:
    node = IntegerNode(origin=ORIGIN, enum=(1, 2, 3))
    assert to_d42(node, {}) == schema.any(schema.int(1), schema.int(2), schema.int(3))


def test_number_bounds_become_float_min_max() -> None:
    node = NumberNode(origin=ORIGIN, minimum=0, maximum=1)
    assert to_d42(node, {}) == schema.float.min(0.0).max(1.0)


def test_number_enum_values_are_coerced_to_float() -> None:
    """``enum: [0.5, 1]`` — в d42 это ``schema.float``, а не смесь int и float."""
    node = NumberNode(origin=ORIGIN, enum=(0.5, 1))
    assert to_d42(node, {}) == schema.any(schema.float(0.5), schema.float(1.0))


def test_integer_multiple_of_is_accepted_together_with_a_consistent_enum() -> None:
    """``multipleOf`` сам по себе fail closed, но enum снимает вопрос генерации.

    Литералы проверяются на делимость, поэтому фикстура заведомо валидна и
    отказываться от неё не за что.
    """
    node = IntegerNode(origin=ORIGIN, enum=(4,), multiple_of=2)
    assert to_d42(node, {}) == schema.int(4)


# ================================================================ bool / none


def test_boolean_without_enum() -> None:
    assert to_d42(BooleanNode(origin=ORIGIN), {}) == schema.bool


def test_boolean_enum_becomes_literals() -> None:
    node = BooleanNode(origin=ORIGIN, enum=(True, False))
    assert to_d42(node, {}) == schema.any(schema.bool(True), schema.bool(False))


def test_null_node_becomes_schema_none() -> None:
    assert to_d42(NullNode(origin=ORIGIN), {}) == schema.none


def test_waived_any_node_becomes_schema_any() -> None:
    """``AnyNode`` рождается только по waiver — и переводится в «что угодно»."""
    assert to_d42(AnyNode(origin=ORIGIN, reason="waiver ISSUE-1"), {}) == schema.any


# ====================================================================== nullable


def test_nullable_leaf_gets_a_none_branch() -> None:
    node = StringNode(origin=ORIGIN, nullable=True, max_length=500)
    assert to_d42(node, {}) == schema.any(schema.str.len(..., 500), schema.none)


def test_nullable_union_appends_none_to_existing_variants() -> None:
    node = union(StringNode(origin=ORIGIN), IntegerNode(origin=ORIGIN), nullable=True)
    assert to_d42(node, {}) == schema.any(schema.str, schema.int, schema.none)


def test_nullable_union_that_already_allows_null_is_left_alone() -> None:
    """``schema.none`` не дублируется: ``oneOf: [string, null]`` + ``nullable``."""
    node = union(StringNode(origin=ORIGIN), NullNode(origin=ORIGIN), nullable=True)
    assert to_d42(node, {}) == schema.any(schema.str, schema.none)


def test_nullable_null_node_stays_schema_none() -> None:
    assert to_d42(NullNode(origin=ORIGIN, nullable=True), {}) == schema.none


def test_nullable_any_node_stays_schema_any() -> None:
    node = AnyNode(origin=ORIGIN, reason="waiver ISSUE-2", nullable=True)
    assert to_d42(node, {}) == schema.any


def test_nullable_object_wraps_the_whole_dict() -> None:
    node = obj(prop("id", StringNode(origin=ORIGIN)), closed=True, nullable=True)
    assert to_d42(node, {}) == schema.any(schema.dict({"id": schema.str}), schema.none)


# ======================================================================= объекты


def test_required_and_optional_keys() -> None:
    node = obj(
        prop("title", StringNode(origin=ORIGIN)),
        prop("summary", StringNode(origin=ORIGIN), required=False),
        closed=True,
    )
    assert to_d42(node, {}) == schema.dict({"title": schema.str, optional("summary"): schema.str})


def test_additional_properties_false_produces_an_exact_dict() -> None:
    """Закрытый объект не должен получить ``...: ...`` — иначе контракт шире спеки."""
    node = obj(prop("id", StringNode(origin=ORIGIN)), closed=True)
    built = to_d42(node, {})

    assert built == schema.dict({"id": schema.str})
    assert built != schema.dict({"id": schema.str, ...: ...})
    assert ... not in built.props.keys


def test_additional_properties_allowed_produces_an_open_dict() -> None:
    node = obj(prop("id", StringNode(origin=ORIGIN)))
    built = to_d42(node, {})

    assert built == schema.dict({"id": schema.str, ...: ...})
    assert built.props.keys[...] == (..., False)


def test_empty_closed_object_is_an_empty_dict() -> None:
    assert to_d42(obj(closed=True), {}) == schema.dict({})


def test_empty_open_object_keeps_only_the_ellipsis() -> None:
    assert to_d42(obj(), {}) == schema.dict({...: ...})


def test_properties_are_ordered_by_name() -> None:
    """Порядок ключей нормализуется: перестановка в спецификации ничего не двигает."""
    forward = obj(
        prop("alpha", StringNode(origin=ORIGIN)),
        prop("beta", StringNode(origin=ORIGIN)),
        closed=True,
    )
    backward = obj(
        prop("beta", StringNode(origin=ORIGIN)),
        prop("alpha", StringNode(origin=ORIGIN)),
        closed=True,
    )

    assert to_d42(forward, {}) == to_d42(backward, {})
    assert list(to_d42(backward, {}).props.keys) == ["alpha", "beta"]


# ======================================================================= массивы


def test_array_without_bounds() -> None:
    node = ArrayNode(origin=ORIGIN, items=StringNode(origin=ORIGIN))
    assert to_d42(node, {}) == schema.list(schema.str)


@pytest.mark.parametrize(
    ("minimum", "maximum", "expected"),
    [
        (1, 4, schema.list(schema.int).len(1, 4)),
        (2, None, schema.list(schema.int).len(2, ...)),
        (None, 10, schema.list(schema.int).len(..., 10)),
    ],
    ids=["both", "only-min", "only-max"],
)
def test_array_item_counts_become_len(minimum, maximum, expected) -> None:
    node = ArrayNode(
        origin=ORIGIN, items=IntegerNode(origin=ORIGIN), min_items=minimum, max_items=maximum
    )
    assert to_d42(node, {}) == expected


def test_unique_items_becomes_unique_call() -> None:
    """``uniqueItems`` переводится точно: d42 honor'ит ``unique()`` и при валидации."""
    node = ArrayNode(origin=ORIGIN, items=StringNode(origin=ORIGIN), unique_items=True)
    built = to_d42(node, {})

    assert built == schema.list(schema.str).unique()
    assert built != schema.list(schema.str)
    assert built.props.unique is True


def test_unique_items_combines_with_length() -> None:
    node = ArrayNode(
        origin=ORIGIN,
        items=StringNode(origin=ORIGIN),
        min_items=1,
        max_items=3,
        unique_items=True,
    )
    assert to_d42(node, {}) == schema.list(schema.str).len(1, 3).unique()


def test_array_of_objects() -> None:
    node = ArrayNode(
        origin=ORIGIN,
        items=obj(prop("heading", StringNode(origin=ORIGIN)), closed=True),
    )
    assert to_d42(node, {}) == schema.list(schema.dict({"heading": schema.str}))


# ==================================================================== union / ref


@pytest.mark.parametrize("kind", [UnionKind.ONE_OF, UnionKind.ANY_OF])
def test_both_composition_kinds_become_schema_any(kind: UnionKind) -> None:
    """Расширение принято осознанно: эксклюзивность ``oneOf`` держит JSON Schema."""
    node = union(StringNode(origin=ORIGIN), IntegerNode(origin=ORIGIN), kind=kind)
    assert to_d42(node, {}) == schema.any(schema.str, schema.int)


def test_union_preserves_variant_order() -> None:
    node = union(IntegerNode(origin=ORIGIN), StringNode(origin=ORIGIN))
    assert to_d42(node, {}) != schema.any(schema.str, schema.int)
    assert to_d42(node, {}) == schema.any(schema.int, schema.str)


def test_ref_is_resolved_through_definitions() -> None:
    definitions = {"Member": obj(prop("id", StringNode(origin=ORIGIN)), closed=True)}
    node = obj(prop("owner", RefNode(origin=ORIGIN, name="Member")), closed=True)

    assert to_d42(node, definitions) == schema.dict({"owner": schema.dict({"id": schema.str})})


def test_nested_refs_are_resolved_in_dependency_order() -> None:
    definitions = {
        "Outer": obj(prop("inner", RefNode(origin=ORIGIN, name="Inner")), closed=True),
        "Inner": obj(prop("value", IntegerNode(origin=ORIGIN)), closed=True),
    }
    node = RefNode(origin=ORIGIN, name="Outer")

    assert to_d42(node, definitions) == schema.dict({"inner": schema.dict({"value": schema.int})})


def test_the_same_definition_is_built_once_and_shared() -> None:
    definitions = {"Label": obj(prop("text", StringNode(origin=ORIGIN)), closed=True)}
    node = obj(
        prop("left", RefNode(origin=ORIGIN, name="Label")),
        prop("right", RefNode(origin=ORIGIN, name="Label")),
        closed=True,
    )
    built = to_d42(node, definitions)

    left, _ = built.props.keys["left"]
    right, _ = built.props.keys["right"]
    assert left is right


def test_nullable_ref_gets_a_none_branch() -> None:
    definitions = {"Label": obj(prop("text", StringNode(origin=ORIGIN)), closed=True)}
    node = RefNode(origin=ORIGIN, name="Label", nullable=True)

    assert to_d42(node, definitions) == schema.any(schema.dict({"text": schema.str}), schema.none)


def test_missing_definition_is_a_ref_resolution_error() -> None:
    with pytest.raises(RefResolutionError) as info:
        to_d42(RefNode(origin=ORIGIN, name="Ghost"), {})

    assert "Ghost" in str(info.value)


# ==================================================================== fail closed


def test_all_of_fails_closed() -> None:
    node = AllOfNode(
        origin=ORIGIN,
        parts=(
            obj(prop("a", StringNode(origin=ORIGIN))),
            obj(prop("b", StringNode(origin=ORIGIN))),
        ),
    )
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    assert "allOf" in str(info.value)
    assert "пересечения типов" in str(info.value)


@pytest.mark.parametrize(
    ("field", "keyword"),
    [("exclusive_minimum", "exclusiveMinimum"), ("exclusive_maximum", "exclusiveMaximum")],
)
def test_float_exclusive_bounds_fail_closed(field: str, keyword: str) -> None:
    """У ``number`` границы d42 только включающие — точного перевода нет."""
    node = NumberNode(origin=ORIGIN, **{field: 0.5})
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    assert keyword in str(info.value)
    assert "number" in str(info.value)


@pytest.mark.parametrize(
    "node",
    [
        IntegerNode(origin=ORIGIN, multiple_of=5),
        NumberNode(origin=ORIGIN, multiple_of=0.5),
    ],
    ids=["integer", "number"],
)
def test_multiple_of_fails_closed(node: SchemaNode) -> None:
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    assert "multipleOf" in str(info.value)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(1, None), (None, 32), (1, 32)],
    ids=["min", "max", "both"],
)
def test_pattern_with_length_fails_closed(minimum, maximum) -> None:
    """``regex()`` и ``len()`` в d42 взаимоисключающи — молча терять нельзя ни то, ни то."""
    node = StringNode(origin=ORIGIN, pattern="^[a-z]+$", min_length=minimum, max_length=maximum)
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    message = str(info.value)
    assert "pattern" in message
    assert "minLength/maxLength" in message


@pytest.mark.parametrize(
    ("field", "keyword"),
    [("min_properties", "minProperties"), ("max_properties", "maxProperties")],
)
def test_property_count_bounds_fail_closed(field: str, keyword: str) -> None:
    node = ObjectNode(origin=ORIGIN, **{field: 2})
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    assert keyword in str(info.value)


def test_typed_additional_properties_are_validated_exactly() -> None:
    """Значения динамических ключей проверяются схемой из additionalProperties."""
    node = ObjectNode(
        origin=ORIGIN,
        properties=(prop("fixed", IntegerNode(origin=ORIGIN)),),
        additional_properties=StringNode(origin=ORIGIN),
    )
    converted = to_d42(node, {})

    validate_or_fail(converted, {"fixed": 1, "dynamic": "ok"})
    with pytest.raises(ValidationException):
        validate_or_fail(converted, {"fixed": 1, "dynamic": 42})


def test_typed_additional_properties_support_fake_substitution_and_make_required() -> None:
    """Custom d42-тип сохраняет привычный API схем словаря."""
    from d42 import fake
    from d42.utils import make_required

    node = ObjectNode(
        origin=ORIGIN,
        properties=(prop("fixed", IntegerNode(origin=ORIGIN), required=False),),
        additional_properties=StringNode(origin=ORIGIN),
    )
    converted = to_d42(node, {})
    required = make_required(converted, keys={"fixed"})
    substituted = required % {"fixed": 7, "dynamic": "value"}

    assert fake(substituted) == {"fixed": 7, "dynamic": "value"}
    validate_or_fail(required, {"fixed": 7, "dynamic": "value"})


def test_self_recursive_definition_is_rejected() -> None:
    definitions = {
        "Node": obj(prop("child", RefNode(origin=ORIGIN, name="Node"), required=False), closed=True)
    }
    with pytest.raises(RecursiveSchemaError) as info:
        to_d42(RefNode(origin=ORIGIN, name="Node"), definitions)

    assert "Node -> Node" in str(info.value)


def test_mutual_recursion_names_the_whole_cycle() -> None:
    definitions = {
        "Alpha": obj(prop("beta", RefNode(origin=ORIGIN, name="Beta")), closed=True),
        "Beta": obj(prop("gamma", RefNode(origin=ORIGIN, name="Gamma")), closed=True),
        "Gamma": obj(prop("alpha", RefNode(origin=ORIGIN, name="Alpha")), closed=True),
    }
    with pytest.raises(RecursiveSchemaError) as info:
        to_d42(RefNode(origin=ORIGIN, name="Alpha"), definitions)

    assert "Alpha -> Beta -> Gamma -> Alpha" in str(info.value)


def test_recursive_error_is_an_unsupported_construct_error() -> None:
    """Потребителю достаточно ловить ``UnsupportedConstructError``."""
    assert issubclass(RecursiveSchemaError, UnsupportedConstructError)


@pytest.mark.parametrize(
    "node",
    [
        StringNode(origin=ORIGIN, enum=()),
        IntegerNode(origin=ORIGIN, enum=()),
        NumberNode(origin=ORIGIN, enum=()),
        BooleanNode(origin=ORIGIN, enum=()),
    ],
    ids=["str", "int", "float", "bool"],
)
def test_empty_enum_fails_closed(node: SchemaNode) -> None:
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    assert "пустой enum" in str(info.value)


def test_union_without_variants_fails_closed() -> None:
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(union(kind=UnionKind.ANY_OF), {})

    assert "anyOf" in str(info.value)


def test_enum_value_conflicting_with_pattern_fails_closed() -> None:
    node = StringNode(origin=ORIGIN, enum=("Draft",), pattern="^[a-z]+$")
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    assert "не соответствует pattern" in str(info.value)


@pytest.mark.parametrize(
    ("node", "fragment"),
    [
        (StringNode(origin=ORIGIN, enum=("ab",), min_length=3), "короче minLength=3"),
        (StringNode(origin=ORIGIN, enum=("abcd",), max_length=3), "длиннее maxLength=3"),
        (IntegerNode(origin=ORIGIN, enum=(0,), minimum=1), "меньше нижней границы"),
        (IntegerNode(origin=ORIGIN, enum=(9,), maximum=5), "больше верхней границы"),
        (IntegerNode(origin=ORIGIN, enum=(0,), exclusive_minimum=0), "меньше нижней границы"),
        (IntegerNode(origin=ORIGIN, enum=(3,), multiple_of=2), "не делится на multipleOf"),
        (NumberNode(origin=ORIGIN, enum=(2.0,), maximum=1.0), "больше верхней границы"),
    ],
    ids=[
        "str-too-short",
        "str-too-long",
        "int-below-min",
        "int-above-max",
        "int-below-exclusive-min",
        "int-not-multiple",
        "float-above-max",
    ],
)
def test_enum_value_conflicting_with_neighbours_fails_closed(
    node: SchemaNode, fragment: str
) -> None:
    """Противоречие «литерал против соседнего ограничения» разрешимо сразу — и решается."""
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    assert fragment in str(info.value)


@pytest.mark.parametrize(
    ("node", "keyword"),
    [
        (StringNode(origin=ORIGIN, min_length=9, max_length=2), "minLength"),
        (IntegerNode(origin=ORIGIN, minimum=9, maximum=2), "minimum"),
        (NumberNode(origin=ORIGIN, minimum=9.5, maximum=2.5), "minimum"),
        (
            ArrayNode(origin=ORIGIN, items=StringNode(origin=ORIGIN), min_items=5, max_items=1),
            "minItems",
        ),
    ],
    ids=["string", "integer", "number", "array"],
)
def test_empty_range_fails_closed(node: SchemaNode, keyword: str) -> None:
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    assert keyword in str(info.value)
    assert "контракт пуст" in str(info.value)


def test_duplicate_property_fails_closed() -> None:
    node = obj(
        prop("title", StringNode(origin=ORIGIN)),
        prop("title", IntegerNode(origin=ORIGIN)),
        closed=True,
    )
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    assert "дважды" in str(info.value)


def test_unknown_ir_node_fails_closed() -> None:
    """Новый вид узла обязан упасть, а не молча стать «чем угодно»."""

    @dataclass(frozen=True, kw_only=True, slots=True)
    class MysteryNode(SchemaNode):
        pass

    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(MysteryNode(origin=ORIGIN), {})

    assert "MysteryNode" in str(info.value)


# ===================================================================== координаты


def test_error_carries_contract_path_and_node_origin() -> None:
    """По сообщению должно быть видно, куда именно выписывать waiver."""
    node = obj(
        prop(
            "sections",
            ArrayNode(
                origin=ORIGIN,
                items=obj(
                    prop(
                        "slug",
                        StringNode(
                            origin=Origin(source="api.yaml", pointer="/paths/~1x/get"),
                            pattern="^[a-z]+$",
                            min_length=2,
                        ),
                    ),
                    closed=True,
                ),
            ),
        ),
        closed=True,
    )
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    error: ContractError = info.value
    assert "[contract path /sections/-/slug]" in str(error)
    assert error.source == "api.yaml"
    assert error.json_pointer == "/paths/~1x/get"


def test_error_inside_a_definition_is_prefixed_with_its_name() -> None:
    definitions = {
        "Member": obj(
            prop("role", ObjectNode(origin=ORIGIN, min_properties=1)),
            closed=True,
        )
    }
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(RefNode(origin=ORIGIN, name="Member"), definitions)

    assert "[contract path Member:/role]" in str(info.value)


def test_error_at_the_contract_root_prints_a_slash() -> None:
    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(NumberNode(origin=ORIGIN, multiple_of=0.5), {})

    assert "[contract path /]" in str(info.value)

"""Правила ``∧`` и распределение ``allOf`` (:mod:`geas.integrations.d42.intersection`).

Каждое правило из шапки модуля закреплено на уровне IR: что даёт пересечение
(узел, пустоту или «не доказуемо»), какой d42 получается в итоге и когда
результат остаётся исходной ссылкой. Сквозной сценарий на спецификации — в
``test_d42_allof.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from d42 import optional, schema

from geas.errors import RefResolutionError, UnsupportedConstructError
from geas.integrations.d42 import intersection
from geas.integrations.d42.converter import DroppedBranchNote, plan_node, to_d42
from geas.integrations.d42.intersection import (
    Empty,
    UnprovableIntersectionError,
    distribute,
)
from geas.models import (
    AdditionalProperties,
    AllOfNode,
    AnyNode,
    ArrayNode,
    BooleanNode,
    Discriminator,
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

ORIGIN = Origin(source="demo.yaml", pointer="/components/schemas/Demo")


def prop(name: str, node: SchemaNode, *, required: bool = True) -> PropertySpec:
    return PropertySpec(name=name, schema=node, required=required)


def obj(
    *properties: PropertySpec,
    additional: AdditionalProperties | SchemaNode = AdditionalProperties.ALLOWED,
    nullable: bool = False,
) -> ObjectNode:
    return ObjectNode(
        origin=ORIGIN,
        nullable=nullable,
        properties=tuple(sorted(properties, key=lambda item: item.name)),
        additional_properties=additional,
    )


def closed(*properties: PropertySpec) -> ObjectNode:
    return obj(*properties, additional=AdditionalProperties.FORBIDDEN)


def string(**kwargs: Any) -> StringNode:
    return StringNode(origin=ORIGIN, **kwargs)


def enum(*values: str) -> StringNode:
    return string(enum=values)


def ref(name: str, *, nullable: bool = False) -> RefNode:
    return RefNode(origin=ORIGIN, name=name, nullable=nullable)


def one_of(*variants: SchemaNode, nullable: bool = False) -> UnionNode:
    return UnionNode(origin=ORIGIN, kind=UnionKind.ONE_OF, variants=variants, nullable=nullable)


def all_of(*parts: SchemaNode, nullable: bool = False) -> AllOfNode:
    return AllOfNode(origin=ORIGIN, parts=parts, nullable=nullable)


def meet(left: SchemaNode, right: SchemaNode, definitions: dict[str, SchemaNode] | None = None):
    """``left ∧ right`` как единственная ветка ``allOf``."""
    return distribute(all_of(left, right), definitions or {}).node


#: Полиморфный DTO в стиле springdoc и его варианты.
KINDS = {
    "Text": obj(prop("kind", enum("TEXT")), prop("text", string())),
    "Image": obj(prop("kind", enum("IMAGE")), prop("url", string(format="uri"))),
}
BLOCK = all_of(obj(prop("kind", enum("TEXT", "IMAGE"))), one_of(ref("Text"), ref("Image")))


# ============================================================== распределение


def test_springdoc_all_of_distributes_over_the_union() -> None:
    """``allOf(base, oneOf(V1, V2))`` → ``any(base ∧ V1, base ∧ V2)``; равные варианты — ссылки."""
    distribution = distribute(BLOCK, KINDS)

    assert isinstance(distribution.node, UnionNode)
    assert distribution.node.kind is UnionKind.ONE_OF
    assert list(distribution.node.variants) == [ref("Text"), ref("Image")]
    assert distribution.dropped == ()
    assert to_d42(BLOCK, KINDS) == schema.any(
        schema.dict({"kind": schema.str("TEXT"), "text": schema.str, ...: ...}),
        schema.dict({"kind": schema.str("IMAGE"), "url": schema.str, ...: ...}),
    )


def test_nullable_all_of_keeps_null() -> None:
    nullable = AllOfNode(origin=ORIGIN, parts=BLOCK.parts, nullable=True)

    converted = to_d42(nullable, KINDS)

    assert converted == schema.any(
        to_d42(ref("Text"), KINDS), to_d42(ref("Image"), KINDS), schema.none
    )


def test_branch_that_differs_from_its_variant_is_inlined() -> None:
    """Свойство, объявленное только базой, сужает вариант: ветка — отдельный словарь."""
    base = obj(prop("kind", enum("TEXT")), prop("meta", obj(), required=False))

    branch = meet(base, ref("Text"), KINDS)

    assert isinstance(branch, ObjectNode)
    assert to_d42(branch, KINDS) == schema.dict(
        {
            "kind": schema.str("TEXT"),
            optional("meta"): schema.dict({...: ...}),
            "text": schema.str,
            ...: ...,
        }
    )


def test_inheritance_through_all_of_ref_is_the_base_itself() -> None:
    """``Child: {type: object, allOf: [$ref Block]}`` — алиас ``Block``."""
    definitions = {**KINDS, "Block": BLOCK}
    child = all_of(obj(), all_of(ref("Block")))

    assert distribute(child, definitions).node == ref("Block")


def test_inheritance_adds_its_own_properties_to_every_branch() -> None:
    definitions = {**KINDS, "Block": BLOCK}
    child = all_of(ref("Block"), obj(prop("pinned", BooleanNode(origin=ORIGIN))))

    assert to_d42(child, definitions) == schema.any(
        schema.dict(
            {"kind": schema.str("TEXT"), "pinned": schema.bool, "text": schema.str, ...: ...}
        ),
        schema.dict(
            {"kind": schema.str("IMAGE"), "pinned": schema.bool, "url": schema.str, ...: ...}
        ),
    )


def test_polymorphic_base_property_meets_a_variant_property() -> None:
    """База ссылается на полиморфный DTO, вариант — на один из его вариантов."""
    definitions = {
        **KINDS,
        "Meta": all_of(obj(prop("kind", string())), one_of(ref("TextMeta"), ref("ImageMeta"))),
        "TextMeta": obj(prop("kind", enum("TEXT_META")), prop("words", IntegerNode(origin=ORIGIN))),
        "ImageMeta": obj(prop("kind", enum("IMAGE_META"))),
        "Rich": obj(prop("kind", enum("RICH")), prop("meta", ref("TextMeta"), required=False)),
    }
    node = all_of(
        obj(prop("kind", enum("RICH")), prop("meta", ref("Meta"), required=False)),
        one_of(ref("Rich")),
    )

    assert distribute(node, definitions).node == ref("Rich")


def test_product_of_two_unions_is_folded_pairwise() -> None:
    """``allOf`` из двух ``oneOf`` — произведение веток; пустые пары выбрасываются молча."""
    definitions = {
        "Text": KINDS["Text"],
        "Image": KINDS["Image"],
        "Small": obj(prop("kind", enum("TEXT", "IMAGE")), prop("size", enum("S"))),
        "Large": obj(prop("kind", enum("IMAGE")), prop("size", enum("L"))),
    }
    node = all_of(one_of(ref("Text"), ref("Image")), one_of(ref("Small"), ref("Large")))

    distribution = distribute(node, definitions)

    assert isinstance(distribution.node, UnionNode)
    assert distribution.node.kind is UnionKind.ONE_OF
    assert [branch.branch for branch in distribution.dropped] == ["Text ∧ Large"]
    kinds = [
        next(p.schema.enum for p in variant.properties if p.name == "kind")  # type: ignore[union-attr]
        for variant in distribution.node.variants
    ]
    assert kinds == [("TEXT",), ("IMAGE",), ("IMAGE",)]


# ============================================================== пустые ветки


def test_empty_branch_is_dropped_and_named() -> None:
    definitions = {**KINDS, "Video": obj(prop("kind", enum("VIDEO")))}
    node = all_of(BLOCK.parts[0], one_of(ref("Text"), ref("Video"), obj(prop("kind", enum("GIF")))))

    distribution = distribute(node, definitions)

    assert distribution.node == ref("Text")
    assert [(item.branch, item.reason) for item in distribution.dropped] == [
        (
            "Video",
            "обязательное свойство 'kind': enum ['TEXT', 'IMAGE'] и ['VIDEO'] не пересекаются",
        ),
        (
            "oneOf[2]",
            "обязательное свойство 'kind': enum ['TEXT', 'IMAGE'] и ['GIF'] не пересекаются",
        ),
    ]


def test_planner_collects_dropped_branches_with_their_place() -> None:
    definitions = {**KINDS, "Video": obj(prop("kind", enum("VIDEO")))}
    node = all_of(BLOCK.parts[0], one_of(ref("Text"), ref("Video")))
    dropped: list[DroppedBranchNote] = []

    plan_node(obj(prop("block", node)), definitions, definition="Page", dropped=dropped)

    assert [(item.where, item.branch) for item in dropped] == [("Page:/block", "Video")]


def test_all_branches_empty_fails_closed() -> None:
    node = all_of(obj(prop("size", IntegerNode(origin=ORIGIN))), obj(prop("size", string())))

    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(node, {})

    message = str(info.value)
    assert "allOf не допускает ни одного значения" in message
    assert "типы integer и string несовместимы" in message


# ============================================================== объекты


def test_properties_are_merged_shared_ones_intersected_required_united() -> None:
    left = obj(prop("a", string(), required=False), prop("b", IntegerNode(origin=ORIGIN)))
    right = obj(
        prop("a", string(max_length=5)), prop("c", BooleanNode(origin=ORIGIN), required=False)
    )

    assert to_d42(meet(left, right), {}) == schema.dict(
        {"a": schema.str.len(..., 5), "b": schema.int, optional("c"): schema.bool, ...: ...}
    )


def test_empty_open_object_yields_to_the_concrete_reference() -> None:
    definitions = {"AnyParams": obj(), "BotParams": closed(prop("bot", string()))}

    assert meet(ref("AnyParams"), ref("BotParams"), definitions) == ref("BotParams")
    assert meet(ref("BotParams"), ref("AnyParams"), definitions) == ref("BotParams")


def test_semantically_equal_references_keep_the_right_one() -> None:
    """Два пустых открытых объекта под разными именами равны — остаётся ссылка варианта."""
    definitions = {"AnyParams": obj(), "EmptyParams": obj()}

    assert meet(ref("AnyParams"), ref("EmptyParams"), definitions) == ref("EmptyParams")


def test_closed_side_drops_optional_foreign_keys() -> None:
    """Ключ, который закрытая сторона не объявляет, недопустим; необязательный — выбрасывается."""
    result = meet(obj(prop("note", string(), required=False)), closed(prop("id", string())))

    assert to_d42(result, {}) == schema.dict({"id": schema.str})


def test_closed_side_empties_required_foreign_keys() -> None:
    result = meet(obj(prop("note", string())), closed(prop("id", string())))

    assert isinstance(result, Empty)
    assert "обязательное свойство 'note'" in result.reason
    assert "additionalProperties: false" in result.reason


def test_typed_additional_properties_meet_declared_keys() -> None:
    counters = obj(additional=IntegerNode(origin=ORIGIN, minimum=0))
    declared = obj(prop("visits", IntegerNode(origin=ORIGIN, maximum=10)))

    result = meet(counters, declared)

    assert isinstance(result, ObjectNode)
    assert result.property("visits") == prop(
        "visits", IntegerNode(origin=ORIGIN, minimum=0, maximum=10)
    )
    assert result.additional_properties == IntegerNode(origin=ORIGIN, minimum=0)


def test_key_that_must_be_absent_in_an_open_object_is_unprovable() -> None:
    """Необязательный ключ с пустым пересечением в открытом словаре d42 не выражает."""
    with pytest.raises(UnprovableIntersectionError, match="допустимо только отсутствующим"):
        meet(
            obj(prop("x", string(), required=False)),
            obj(prop("x", BooleanNode(origin=ORIGIN), required=False)),
        )


# ============================================================== скаляры


def test_enum_intersection_keeps_the_order_of_the_narrower_side() -> None:
    result = meet(enum("C", "B", "A", "D"), enum("A", "B", "C"))

    assert result == string(enum=("A", "B", "C"))


def test_string_without_enum_filters_literals_by_pattern_and_length() -> None:
    result = meet(string(pattern="^[A-Z]+$", max_length=3), enum("ABC", "abc", "ABCD"))

    assert result == string(enum=("ABC",))


def test_string_bounds_take_the_stricter_side() -> None:
    result = meet(string(min_length=1, max_length=10, pattern="^x"), string(min_length=3))

    assert result == string(min_length=3, max_length=10, pattern="^x")


def test_contradicting_string_lengths_are_empty() -> None:
    result = meet(string(min_length=5), string(max_length=3))

    assert result == Empty("minLength=5 больше maxLength=3")


def test_two_different_patterns_are_unprovable() -> None:
    with pytest.raises(UnprovableIntersectionError, match="два разных pattern"):
        meet(string(pattern="^a"), string(pattern="b$"))


def test_integer_bounds_and_enum() -> None:
    bounded = meet(
        IntegerNode(origin=ORIGIN, minimum=0, exclusive_maximum=100),
        IntegerNode(origin=ORIGIN, minimum=10, maximum=50, multiple_of=4),
    )
    literal = meet(
        IntegerNode(origin=ORIGIN, minimum=2), IntegerNode(origin=ORIGIN, enum=(1, 2, 3))
    )

    assert bounded == IntegerNode(
        origin=ORIGIN, minimum=10, maximum=50, exclusive_maximum=100, multiple_of=4
    )
    assert literal == IntegerNode(origin=ORIGIN, enum=(2, 3))


def test_integer_multiple_of_is_the_least_common_multiple() -> None:
    result = meet(
        IntegerNode(origin=ORIGIN, multiple_of=4), IntegerNode(origin=ORIGIN, multiple_of=6)
    )

    assert isinstance(result, IntegerNode)
    assert result.multiple_of == 12


def test_integer_format_bounds_are_kept_when_formats_differ() -> None:
    result = meet(
        IntegerNode(origin=ORIGIN, format="int64"), IntegerNode(origin=ORIGIN, format="int32")
    )

    assert to_d42(result, {}) == schema.int.min(-(2**31)).max(2**31 - 1)


def test_integer_and_number_are_unprovable() -> None:
    with pytest.raises(UnprovableIntersectionError, match="integer и number"):
        meet(IntegerNode(origin=ORIGIN), NumberNode(origin=ORIGIN))


def test_number_multiple_of_must_be_equal() -> None:
    same = meet(NumberNode(origin=ORIGIN, multiple_of=0.5), NumberNode(origin=ORIGIN, maximum=2.0))

    assert same == NumberNode(origin=ORIGIN, maximum=2.0, multiple_of=0.5)
    with pytest.raises(UnprovableIntersectionError, match="multipleOf"):
        meet(NumberNode(origin=ORIGIN, multiple_of=0.5), NumberNode(origin=ORIGIN, multiple_of=0.3))


def test_boolean_enum_intersection() -> None:
    assert meet(
        BooleanNode(origin=ORIGIN), BooleanNode(origin=ORIGIN, enum=(True,))
    ) == BooleanNode(origin=ORIGIN, enum=(True,))
    assert isinstance(
        meet(BooleanNode(origin=ORIGIN, enum=(False,)), BooleanNode(origin=ORIGIN, enum=(True,))),
        Empty,
    )


def test_incompatible_types_are_empty() -> None:
    assert meet(string(), IntegerNode(origin=ORIGIN)) == Empty("типы string и integer несовместимы")


# ============================================================== массивы


def test_arrays_intersect_items_lengths_and_uniqueness() -> None:
    result = meet(
        ArrayNode(origin=ORIGIN, items=string(), min_items=1, unique_items=True),
        ArrayNode(origin=ORIGIN, items=string(max_length=4), max_items=3),
    )

    assert to_d42(result, {}) == schema.list(schema.str.len(..., 4)).len(1, 3).unique()


def test_arrays_with_incompatible_items_allow_only_the_empty_array() -> None:
    maybe_empty = meet(
        ArrayNode(origin=ORIGIN, items=string()),
        ArrayNode(origin=ORIGIN, items=IntegerNode(origin=ORIGIN)),
    )
    non_empty = meet(
        ArrayNode(origin=ORIGIN, items=string(), min_items=1),
        ArrayNode(origin=ORIGIN, items=IntegerNode(origin=ORIGIN)),
    )

    assert isinstance(maybe_empty, ArrayNode)
    assert maybe_empty.max_items == 0
    assert to_d42(maybe_empty, {}) == schema.list(schema.int).len(..., 0)
    assert isinstance(non_empty, Empty)


# ============================================================== nullable и any


def test_nullable_survives_only_when_both_sides_allow_null() -> None:
    both = meet(string(nullable=True), enum("A"))
    nullable = meet(string(nullable=True), StringNode(origin=ORIGIN, nullable=True, enum=("A",)))

    assert both == string(enum=("A",))
    assert to_d42(nullable, {}) == schema.any(schema.str("A"), schema.none)


def test_incompatible_nullable_types_leave_only_null() -> None:
    result = meet(string(nullable=True), IntegerNode(origin=ORIGIN, nullable=True))

    assert isinstance(result, NullNode)


def test_nullable_reference_counts_as_nullable() -> None:
    definitions = {"Name": string()}

    assert meet(ref("Name", nullable=True), ref("Name"), definitions) == ref("Name")
    assert meet(ref("Name", nullable=True), ref("Name", nullable=True), definitions) == ref(
        "Name", nullable=True
    )


def test_waived_any_node_yields_to_the_other_side() -> None:
    waived = AnyNode(origin=ORIGIN, reason="waiver")

    assert meet(waived, enum("A")) == enum("A")
    assert meet(enum("A"), waived) == enum("A")


def test_union_meets_a_node_variant_by_variant() -> None:
    result = meet(one_of(enum("A"), IntegerNode(origin=ORIGIN), nullable=True), string())

    assert result == enum("A")


# ============================================================== fail closed


def test_unprovable_branch_error_names_the_branch_and_the_property() -> None:
    definitions = {"Digits": obj(prop("code", string(pattern="^[0-9]+$")))}
    node = all_of(obj(prop("code", string(pattern="^[A-Z]+$"))), one_of(ref("Digits")))

    with pytest.raises(UnsupportedConstructError) as info:
        to_d42(obj(prop("stamp", node)), definitions)

    message = str(info.value)
    assert "allOf не выражается в d42: пересечение ветки Digits не доказуемо" in message
    assert "два разных pattern" in message
    assert "[contract path /stamp/code]" in message


def test_all_of_that_contains_itself_is_unprovable() -> None:
    """``Loop = allOf(obj, oneOf[Leaf, Wrapper])``, ``Wrapper = allOf($ref Loop)`` — бесконечная развёртка."""
    definitions: dict[str, SchemaNode] = {
        "Loop": all_of(obj(prop("kind", string())), one_of(ref("Leaf"), ref("Wrapper"))),
        "Leaf": obj(prop("kind", enum("LEAF"))),
        "Wrapper": all_of(obj(), all_of(ref("Loop"))),
    }

    with pytest.raises(UnsupportedConstructError, match="ссылается сам на себя"):
        to_d42(ref("Loop"), definitions)


def test_missing_definition_is_a_ref_resolution_error() -> None:
    with pytest.raises(RefResolutionError, match="Ghost"):
        to_d42(all_of(obj(), ref("Ghost")), {})


def test_too_many_branches_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intersection, "MAX_BRANCHES", 3)
    node = all_of(one_of(enum("A"), enum("B")), one_of(string(), string(max_length=1)))

    with pytest.raises(UnprovableIntersectionError, match="больше чем в 3 веток"):
        distribute(node, {})


# ============================================================== редкие пути


def test_all_branches_of_a_union_empty_names_each_branch() -> None:
    definitions = {
        "Heart": obj(prop("kind", enum("HEART"))),
        "Star": obj(prop("kind", enum("STAR"))),
    }
    node = all_of(obj(prop("kind", enum("LIKE"))), one_of(ref("Heart"), ref("Star")))

    result = distribute(node, definitions).node

    assert isinstance(result, Empty)
    assert result.reason.startswith("ни одна ветка не допускает значений (Heart: ")
    assert "; Star: " in result.reason


def test_union_without_compatible_variants_keeps_only_a_shared_null() -> None:
    """Объединение внутри свойства пересекается вариант за вариантом."""
    union = one_of(enum("A"), enum("B"), nullable=True)

    both_null = meet(
        obj(prop("v", union)), obj(prop("v", StringNode(origin=ORIGIN, nullable=True, enum=("C",))))
    )
    assert isinstance(both_null, ObjectNode)
    assert isinstance(both_null.properties[0].schema, NullNode)

    empty = meet(obj(prop("v", union)), obj(prop("v", enum("C"))))
    assert isinstance(empty, Empty)
    assert empty.reason.startswith("обязательное свойство 'v': ни один вариант oneOf не совместим")


def test_inline_all_of_inside_a_property_is_distributed_before_meeting() -> None:
    inline = all_of(obj(prop("a", string())), obj(prop("b", string(), required=False)))

    result = meet(obj(prop("x", inline)), obj(prop("x", obj(prop("c", string())))))

    assert to_d42(result, {}) == schema.dict(
        {
            "x": schema.dict(
                {"a": schema.str, optional("b"): schema.str, "c": schema.str, ...: ...}
            ),
            ...: ...,
        }
    )


def test_reference_to_a_reference_is_followed() -> None:
    definitions: dict[str, SchemaNode] = {
        "Alias": ref("Target"),
        "Target": obj(prop("id", string())),
    }

    result = meet(
        ref("Alias"), obj(prop("id", string()), prop("n", IntegerNode(origin=ORIGIN))), definitions
    )

    assert to_d42(result, definitions) == schema.dict({"id": schema.str, "n": schema.int, ...: ...})


def test_structurally_equal_recursive_definitions_are_unprovable() -> None:
    """``Tree ∧ Forest`` с разными самоссылками разворачивался бы бесконечно."""
    definitions: dict[str, SchemaNode] = {
        "Tree": obj(prop("name", string()), prop("child", ref("Tree"), required=False)),
        "Forest": obj(
            prop("name", string(max_length=5)), prop("child", ref("Forest"), required=False)
        ),
    }

    with pytest.raises(UnprovableIntersectionError, match="Tree и Forest ссылается само на себя"):
        meet(ref("Tree"), ref("Forest"), definitions)


def test_number_enum_is_filtered_by_bounds() -> None:
    result = meet(
        NumberNode(origin=ORIGIN, maximum=1.0), NumberNode(origin=ORIGIN, enum=(0.5, 1.5))
    )

    assert result == NumberNode(origin=ORIGIN, enum=(0.5,))
    with pytest.raises(UnprovableIntersectionError, match="enum у number рядом с multipleOf"):
        meet(NumberNode(origin=ORIGIN, multiple_of=0.5), NumberNode(origin=ORIGIN, enum=(0.5,)))


def test_contradicting_property_counts_are_empty() -> None:
    left = ObjectNode(origin=ORIGIN, min_properties=3)
    right = ObjectNode(origin=ORIGIN, max_properties=2)

    assert meet(left, right) == Empty("minProperties=3 больше maxProperties=2")


# ============================================================== required без схемы


def test_required_key_without_schema_meets_a_declared_property() -> None:
    """Локальная часть наследования требует ключ, другая часть даёт ему схему."""
    local = ObjectNode(origin=ORIGIN, required_undeclared=("caption",))

    result = meet(local, obj(prop("caption", string(), required=False)))

    assert to_d42(result, {}) == schema.dict({"caption": schema.str, ...: ...})


def test_required_key_without_schema_meets_a_closed_object() -> None:
    local = ObjectNode(origin=ORIGIN, required_undeclared=("caption",))

    result = meet(local, closed(prop("id", string())))

    assert isinstance(result, Empty)
    assert "обязательное свойство 'caption'" in result.reason


def test_required_key_without_schema_on_one_side_only_stays_required() -> None:
    result = meet(ObjectNode(origin=ORIGIN, required_undeclared=("a",)), obj(prop("b", string())))

    assert isinstance(result, ObjectNode)
    assert result.required_undeclared == ("a",)
    assert result.required_names == ("a", "b")


# ============================================================== discriminator


#: Варианты, которые сами поле-дискриминатор не сужают: вид задаёт только ``mapping``.
PETS = {
    "Cat": obj(prop("meow", BooleanNode(origin=ORIGIN))),
    "Dog": obj(prop("bark", string())),
}


def pet(*kinds: str, required: bool = True, mapping: tuple[tuple[str, str], ...] = ()) -> AllOfNode:
    union = UnionNode(
        origin=ORIGIN,
        kind=UnionKind.ONE_OF,
        variants=(ref("Cat"), ref("Dog")),
        discriminator=Discriminator(
            property_name="petType",
            mapping=mapping or (("cat", "Cat"), ("dog", "Dog")),
        ),
    )
    base = obj(prop("petType", enum(*kinds) if kinds else string(), required=required))
    return all_of(base, union)


def test_discriminator_mapping_narrows_the_enum_of_each_branch() -> None:
    """``if petType == dog then Dog`` + ``oneOf``: в ветке Cat значение ``dog`` недопустимо.

    Без этого ``fake()`` собирал ``{petType: dog, meow: true}`` — ветку Cat с меткой
    Dog, — и примерно половина фикстур не проходила контракт.
    """
    converted = to_d42(pet("cat", "dog"), PETS)

    assert converted == schema.any(
        schema.dict({"meow": schema.bool, "petType": schema.str("cat"), ...: ...}),
        schema.dict({"bark": schema.str, "petType": schema.str("dog"), ...: ...}),
    )


def test_discriminator_value_outside_the_mapping_stays_in_every_branch() -> None:
    """Значение без ``mapping`` не включает ни одного ``if``: подходит любой вариант."""
    converted = to_d42(pet("cat", "dog", "fox"), PETS)

    assert converted == schema.any(
        schema.dict(
            {
                "meow": schema.bool,
                "petType": schema.any(schema.str("cat"), schema.str("fox")),
                ...: ...,
            }
        ),
        schema.dict(
            {
                "bark": schema.str,
                "petType": schema.any(schema.str("dog"), schema.str("fox")),
                ...: ...,
            }
        ),
    )


def test_discriminator_property_is_required_in_every_branch() -> None:
    """JSON Schema объединения с ``discriminator`` требует поле — d42 тоже."""
    converted = to_d42(pet("cat", "dog", required=False), PETS)

    assert converted == schema.any(
        schema.dict({"meow": schema.bool, "petType": schema.str("cat"), ...: ...}),
        schema.dict({"bark": schema.str, "petType": schema.str("dog"), ...: ...}),
    )


def test_variant_left_without_discriminator_values_is_dropped_and_named() -> None:
    """Все значения ``enum`` отданы другим вариантам: ветка не допускает ни одного значения."""
    distribution = distribute(pet("cat", mapping=(("cat", "Cat"),)), PETS)

    assert [item.branch for item in distribution.dropped] == ["Dog"]
    assert "discriminator" in distribution.dropped[0].reason
    assert to_d42(distribution.node, PETS) == schema.dict(
        {"meow": schema.bool, "petType": schema.str("cat"), ...: ...}
    )


def test_discriminator_without_enum_only_requires_the_property() -> None:
    """Исключить значения строки без ``enum`` d42 не умеет: ветка остаётся шире, как раньше."""
    converted = to_d42(pet(required=False), PETS)

    assert converted == schema.any(
        schema.dict({"meow": schema.bool, "petType": schema.str, ...: ...}),
        schema.dict({"bark": schema.str, "petType": schema.str, ...: ...}),
    )


# ============================================================== регулярки


def test_pattern_python_cannot_parse_is_unprovable_for_enum_literals() -> None:
    """ECMA-паттерн вроде ``\\p{Lu}`` не должен ронять генерацию сырым ``re.error``."""
    base = obj(prop("code", string(pattern=r"^\p{Lu}+$")))
    variant = obj(prop("code", enum("ABC")))

    with pytest.raises(UnprovableIntersectionError, match=r"не разбирается"):
        meet(base, variant)
    with pytest.raises(UnsupportedConstructError, match=r"allOf не выражается в d42"):
        to_d42(all_of(base, variant), {})

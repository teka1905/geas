"""d42 для рекурсивных контрактов: отсечка цикла и что умеет отсечённый узел.

Фикстура — ``features/recursive_d42.yaml``:

* ``main.createFolderTree`` — нерекурсивный запрос и ответ с обязательным
  рекурсивным массивом ``FolderTree.children``;
* ``main.getRule`` — цикл через объединение ``Rule = oneOf[AllOf, AnyOf, Flag]``,
  выход из цикла — вариант ``Flag``;
* ``main.createReport`` — ответ невыразим в d42 (``pattern`` вместе с
  ``minLength``), запрос выразим;
* ``main.getChain`` — цикл только через обязательное поле, конечного значения нет.

Контракт проверки такой: всё до места отсечки — обычная generated-схема; узел
отсечки проверяет значение по JSON Schema определения, ``%`` уходит вглубь
рекурсивной части, ``fake()`` даёт минимальный валидный экземпляр или понятную
ошибку. Направления операции независимы.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from d42 import ValidationException, fake, schema, validate_or_fail
from d42.declaration.types import AnySchema, DictSchema, ListSchema
from d42.substitution.errors import SubstitutionError
from d42.utils import make_required

from geas import Direction
from geas.errors import (
    ContractOverlayError,
    OperationLookupError,
    RecursiveSchemaError,
    ValidationFailedError,
)
from geas.integrations.d42 import (
    RecursionContract,
    RecursiveRefSchema,
    build_fixture,
    overlay_generators,
    render_module,
    to_d42,
)
from geas.models import ArrayNode, ObjectNode, Origin, PropertySpec, RefNode, UnionKind, UnionNode
from support import Project, make_project, spec

FOLDER_ID = "0b6f3f2e-7c1d-4a5e-9f3b-2d8c1e4a6b7f"
OTHER_ID = "5a1e9c3d-2b4f-4d6a-8e7c-9f0b1a2c3d4e"


def build_project(root: Path) -> Project:
    project = make_project(root, package="recursive_contracts")
    project.write_spec("api/spec.yaml", spec("features", "recursive_d42.yaml"))
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})
    return project


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    built = build_project(tmp_path_factory.mktemp("recursive_d42"))
    built.update()
    return built


@pytest.fixture(scope="module")
def operations(project: Project) -> Iterator[Any]:
    with project.importable() as generated:
        yield generated.operations


@pytest.fixture(scope="module")
def tree(operations: Any) -> Any:
    return operations.main.create_folder_tree.d42_schema(Direction.RESPONSE)


@pytest.fixture(scope="module")
def rule(operations: Any) -> Any:
    return operations.main.get_rule.d42_schema(Direction.RESPONSE)


def folder_tree(*children: dict[str, Any], folder_id: str = FOLDER_ID) -> dict[str, Any]:
    return {"folder": {"id": folder_id, "name": "Отчёты"}, "children": list(children)}


# ------------------------------------------------------------------ отсечка


def test_cycles_are_cut_where_they_close(project: Project) -> None:
    """Каждая операция режет цикл ровно на ссылке, которая возвращает обход назад."""
    artifacts = project.render()

    assert artifacts.d42_cycle_cuts == (
        ("main.createFolderTree", "response", "FolderTree", "FolderTree"),
        ("main.getChain", "response", "Chain", "Chain"),
        ("main.getRule", "response", "AllOfRule", "Rule"),
        ("main.getRule", "response", "AnyOfRule", "Rule"),
    )


def test_cycle_through_a_union_keeps_the_entry_definition_typed(rule: Any) -> None:
    """``Rule`` остаётся типизированным объединением; отсечены ``rules`` у обоих вариантов.

    Обход идёт от корня операции, поэтому And/Or-подобные варианты получают
    одинаковую отсечку, а не асимметричную по алфавиту.
    """
    assert isinstance(rule, AnySchema)
    variants = rule.props.types
    assert [type(item) for item in variants] == [DictSchema, DictSchema, DictSchema]
    for variant in variants[:2]:
        rules, _ = variant.props.keys["rules"]
        assert isinstance(rules, ListSchema)
        assert isinstance(rules.props.type, RecursiveRefSchema)
        assert rules.props.type.name == "Rule"


def test_generated_module_equals_the_in_memory_conversion(project: Project) -> None:
    """``exec`` generated-модуля и ``to_d42`` дают равные схемы и для рекурсии."""
    built = project.build().by_key("main.getRule")
    definitions = dict(built.response_definitions)
    root = built.contract.responses[0].body
    assert isinstance(root, RefNode)

    source = render_module(
        module_docstring="Проверка эквивалентности.",
        definitions=definitions,
        exports={},
        roots=[root],
    )
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)

    assert namespace["GeneratedRuleSchema"] == to_d42(root, definitions)


def test_recursion_contract_holds_only_cut_definitions_and_their_closure(project: Project) -> None:
    """В модуль встраивается JSON Schema отсечённых определений и того, что из них достижимо."""
    source = (project.output_dir / "_d42" / "main__create_folder_tree_response.py").read_text(
        encoding="utf-8"
    )

    assert "_RECURSION = RecursionContract({" in source
    assert '"children": schema.list(_RECURSION.ref("FolderTree")),' in source
    assert '_RECURSION.bind({\n    "FolderTree": GeneratedFolderTreeSchema,\n})' in source
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)
    assert sorted(namespace["_RECURSION"].definitions) == ["Folder", "FolderTree"]


# --------------------------------------------------------- направления


def test_request_without_recursion_is_generated_independently(project: Project) -> None:
    """Рекурсия ответа не влияет на запрос: у запроса обычный модуль без контракта рекурсии."""
    document = project.contract_document("main__create_folder_tree")
    assert document["d42"]["recursive"] is True
    assert document["d42"]["request_module"] == "main__create_folder_tree_request"
    assert document["d42"]["response_module"] == "main__create_folder_tree_response"

    source = (project.output_dir / "_d42" / "main__create_folder_tree_request.py").read_text(
        encoding="utf-8"
    )
    assert "RecursionContract" not in source


def test_d42_failure_in_one_direction_keeps_the_other(project: Project, operations: Any) -> None:
    """Невыразимый в d42 ответ не отнимает d42 у запроса."""
    document = project.contract_document("main__create_report")
    assert document["d42"]["enabled"] is True
    assert document["d42"]["request_module"] == "main__create_report_request"
    assert document["d42"]["response_module"] is None
    assert "pattern" in document["d42"]["response_reason"]
    assert "request_reason" not in document["d42"]
    assert any(item.startswith("d42 (response): ") for item in document["unsupported"])

    handle = operations.main.create_report
    request_schema = handle.d42_schema(Direction.REQUEST)
    validate_or_fail(request_schema, {"title": "Квартал"})
    with pytest.raises(OperationLookupError, match="pattern"):
        handle.d42_schema(Direction.RESPONSE)


def test_update_reports_cuts_and_per_direction_failures(project: Project) -> None:
    """``geas update`` называет место отсечки и направление, в котором d42 отключён."""
    result = project.cli("update")

    assert result.returncode == 0, result.stderr
    assert (
        "main.createFolderTree (response): цикл отсечён на ссылке FolderTree -> FolderTree"
        in result.stdout
    )
    assert "main.createReport: d42-схемы не сгенерированы — response: pattern" in result.stdout
    assert "контракт рекурсивен, d42-схемы не генерируются" not in result.stdout


# -------------------------------------------------------------- проверка


def test_cut_node_accepts_a_deep_valid_tree(tree: Any, operations: Any) -> None:
    value = folder_tree(folder_tree(folder_tree(folder_id=OTHER_ID)))

    validate_or_fail(tree, value)
    operations.main.create_folder_tree.validate_response(value, status=200)


def test_cut_node_checks_the_recursive_part_by_json_schema(tree: Any) -> None:
    """Глубоко вложенное нарушение ловится с точным путём, а не пропускается."""
    broken = folder_tree(folder_tree(folder_tree({"folder": {"id": FOLDER_ID, "name": 7}})))

    with pytest.raises(ValidationException) as info:
        validate_or_fail(tree, broken)

    message = str(info.value)
    assert "['children'][0]['children'][0]['children'][0]['folder']['name']" in message
    assert "JSON Schema of 'FolderTree'" in message


def test_cut_node_requires_what_the_contract_requires(tree: Any) -> None:
    with pytest.raises(ValidationException, match="'children' is a required property"):
        validate_or_fail(tree, folder_tree({"folder": {"id": FOLDER_ID, "name": "x"}}))


def test_cut_node_enforces_one_of_and_discriminator(rule: Any) -> None:
    """Семантику ``oneOf``/``discriminator`` рекурсивной части держит JSON Schema листа."""
    validate_or_fail(rule, {"kind": "ALL", "rules": [{"kind": "FLAG", "enabled": True}]})

    with pytest.raises(ValidationException, match="Rule"):
        validate_or_fail(rule, {"kind": "ALL", "rules": [{"kind": "ANY", "rules": []}]})
    with pytest.raises(ValidationException, match="Rule"):
        validate_or_fail(rule, {"kind": "ALL", "rules": [{"kind": "FLAG", "rules": [1]}]})


def test_cut_node_does_not_check_format_like_the_rest_of_d42(tree: Any, operations: Any) -> None:
    """``format`` на d42-пути не проверяется ни на верхнем уровне, ни в рекурсии.

    Его держит JSON-Schema-путь ``validate_response``: он видит рекурсию целиком.
    """
    value = folder_tree(folder_tree(folder_id="not-a-uuid"))

    validate_or_fail(tree, value)
    with pytest.raises(ValidationFailedError, match="uuid"):
        operations.main.create_folder_tree.validate_response(value, status=200)


# ------------------------------------------------------------ подстановка


def test_substitution_reaches_into_the_recursive_part(tree: Any) -> None:
    """``%`` частичный и уходит вглубь: закрепляется только то, что передано."""
    pinned = tree % {"children": [{"folder": {"name": "Архив"}, "children": []}]}

    value = fake(pinned)

    assert value["children"][0]["folder"]["name"] == "Архив"
    assert value["children"][0]["children"] == []
    assert isinstance(value["children"][0]["folder"]["id"], str)
    validate_or_fail(pinned, value)
    with pytest.raises(ValidationException):
        validate_or_fail(pinned, {**value, "children": [folder_tree()]})


def test_substitution_rejects_a_value_outside_the_contract(tree: Any) -> None:
    with pytest.raises(SubstitutionError):
        tree % {"children": [{"folder": {"name": 1}}]}


def test_make_required_works_on_the_generated_dict(operations: Any) -> None:
    """``make_required`` поверхностный и у d42; на результате отсечки он ведёт себя так же."""
    schema_ = operations.main.create_folder_tree.d42_schema(Direction.RESPONSE)
    required = make_required(schema_)

    assert all(not is_optional for _, (_, is_optional) in required.props.keys.items())
    validate_or_fail(required, folder_tree(folder_tree()))


# -------------------------------------------------------------- генерация


def test_fake_generates_minimal_instances_of_the_cut_node(tree: Any, operations: Any) -> None:
    """Обязательный рекурсивный массив: каждый вложенный узел — минимальный экземпляр."""
    for _ in range(20):
        value = fake(tree)
        for child in value["children"]:
            assert child["children"] == []
            assert set(child) == {"folder", "children"}
        validate_or_fail(tree, value)
        # format d42 не генерирует — поэтому uuid подставляется явно, как и без рекурсии.
        fixed = {
            "folder": {**value["folder"], "id": FOLDER_ID},
            "children": [
                {**child, "folder": {**child["folder"], "id": OTHER_ID}}
                for child in value["children"]
            ],
        }
        operations.main.create_folder_tree.validate_response(fixed, status=200)


def test_fake_takes_the_first_union_variant_that_leaves_the_cycle(rule: Any) -> None:
    """Вложенное правило — ``Flag``: у ``AllOf``/``AnyOf`` нет выхода из цикла короче."""
    value = build_fixture(rule)

    assert value["kind"] == "ALL"
    assert value["rules"]
    assert {item["kind"] for item in value["rules"]} == {"FLAG"}
    validate_or_fail(rule, value)


def test_build_fixture_is_deterministic_on_recursive_schemas(tree: Any, rule: Any) -> None:
    assert build_fixture(tree, seed=7) == build_fixture(tree, seed=7)
    assert build_fixture(rule, seed=7) == build_fixture(rule, seed=7)


def test_fake_explains_a_cycle_without_a_finite_value(operations: Any) -> None:
    """``Chain.next`` обязателен и ведёт в ``Chain``: конечного значения нет — ошибка, а не зависание."""
    chain = operations.main.get_chain.d42_schema(Direction.RESPONSE)

    with pytest.raises(RecursiveSchemaError, match="нет конечного значения") as info:
        fake(chain)
    assert "Chain" in str(info.value)
    with pytest.raises(RecursiveSchemaError):
        build_fixture(chain)


def test_fake_explains_when_the_minimal_value_breaks_the_contract() -> None:
    """Минимальный экземпляр проверяется по JSON Schema; неоднозначный ``oneOf`` — понятная ошибка.

    Корень входит в цикл через ``Deeper``, поэтому отсечена ссылка ``Choice → Deeper``.
    Минимальный ``Deeper`` — это ``{"next": {}}``: в ``Choice = oneOf[Deeper, Left, Right]``
    первый вариант с выходом из цикла — ``Left`` без полей, а ``{}`` подходит и под
    ``Right``.
    """
    origin = Origin(source="demo.yaml", pointer="/components/schemas/Choice")

    def ref(name: str) -> RefNode:
        return RefNode(origin=origin, name=name)

    def object_(*properties: PropertySpec) -> ObjectNode:
        return ObjectNode(origin=origin, properties=properties)

    definitions = {
        "Choice": UnionNode(
            origin=origin,
            kind=UnionKind.ONE_OF,
            variants=(ref("Deeper"), ref("Left"), ref("Right")),
        ),
        "Deeper": object_(PropertySpec(name="next", schema=ref("Choice"), required=True)),
        "Left": object_(),
        "Right": object_(),
    }
    root = ObjectNode(
        origin=origin,
        properties=(
            PropertySpec(
                name="items",
                schema=ArrayNode(origin=origin, items=ref("Deeper"), min_items=1),
                required=True,
            ),
        ),
    )
    converted = to_d42(root, definitions)

    with pytest.raises(
        ValidationFailedError, match="минимальное значение определения 'Deeper'"
    ) as info:
        build_fixture(converted)
    assert "pointer=/next" in str(info.value)


# ------------------------------------------------------------- overlays


def test_overlay_works_on_the_typed_part(tree: Any) -> None:
    overlaid = overlay_generators(tree, {("folder", "name"): schema.str("Входящие")})

    value = build_fixture(overlaid)

    assert value["folder"]["name"] == "Входящие"
    validate_or_fail(tree, value)


def test_overlay_does_not_cross_a_cut_node(tree: Any) -> None:
    """Путь внутрь рекурсивной части — понятная ошибка с подсказкой про ``%``."""
    from geas.integrations.d42 import EACH

    with pytest.raises(ContractOverlayError, match="отсечён цикл") as info:
        overlay_generators(tree, {("children", EACH, "folder", "name"): schema.str("x")})
    assert "%" in str(info.value)

    with pytest.raises(ContractOverlayError, match="отсечён цикл"):
        overlay_generators(tree, {("children", EACH): schema.dict})


# ------------------------------------------------------ контракт рекурсии


def test_recursion_contract_equality_ignores_bindings() -> None:
    """Лист сравнивается по имени и JSON Schema: связь с d42-схемой в равенство не входит."""
    definitions = {"Node": {"type": "object"}}
    first, second = RecursionContract(definitions), RecursionContract(definitions)
    first_ref, second_ref = first.ref("Node"), second.ref("Node")
    first.bind({"Node": schema.dict({"a": first_ref})})
    second.bind({"Node": schema.dict({"b": second_ref})})

    assert first == second
    assert first_ref == second_ref
    assert first.ref("Node") is first_ref


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (lambda contract: contract.ref("Missing"), "определения 'Missing' нет"),
        (lambda contract: contract.bind({}), "не переданы d42-схемы"),
        (lambda contract: contract.target("Node"), "bind\\(\\) не вызывался"),
    ],
)
def test_recursion_contract_misuse_is_explained(action: Any, message: str) -> None:
    contract = RecursionContract({"Node": {"type": "object"}})
    contract.ref("Node")

    with pytest.raises(RecursiveSchemaError, match=message):
        action(contract)


def test_recursion_contract_binds_once() -> None:
    contract = RecursionContract({"Node": {"type": "object"}})
    contract.bind({"Node": schema.dict})

    with pytest.raises(RecursiveSchemaError, match="уже связан"):
        contract.bind({"Node": schema.dict})
    with pytest.raises(RecursiveSchemaError, match="уже связан"):
        contract.ref("Node")

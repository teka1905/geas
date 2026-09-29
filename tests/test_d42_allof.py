"""d42 для полиморфного ``allOf``: распределение пересечения по объединению.

Фикстура — ``features/allof_polymorphic.yaml`` (springdoc-стиль: у схемы
одновременно ``properties``, ``oneOf`` и ``discriminator``):

* ``main.getBlock`` — ``Block``: вариант, равный своему пересечению с базой
  (``TextBlock``), вариант без свойств базы (``ImageBlock``), рекурсия через вариант
  (``ListBlock``), пустой открытый объект в базе против конкретного у варианта и
  ссылка базы на полиморфный DTO против его варианта;
* ``main.pinBlock`` — наследование ``PinnedBlock: allOf: [$ref Block]``;
* ``main.captionBlock`` — наследование ``CaptionedBlock``, у которого ``required``
  называет ключ ``caption`` без схемы в своей части (её задаёт другая часть ``allOf``);
* ``main.addReaction`` — у ``Reaction`` ветка ``Heart`` пуста по самому контракту;
* ``main.stampDocument`` — пересечение ветки ``Stamp`` не доказуемо (два разных
  ``pattern``): запрос без d42, ответ с d42.

Контракт проверки: JSON Schema не меняется (``allOf`` остаётся), d42 принимает
ровно то же, что принимают база и хотя бы один вариант, — не уже и не шире (кроме
эксклюзивности ``oneOf``, которую держит JSON Schema, как у любого ``oneOf``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from d42 import ValidationException, fake, validate_or_fail
from d42.declaration.types import AnySchema, ListSchema

from geas import Direction
from geas.errors import OperationLookupError, ValidationFailedError
from geas.integrations.d42 import RecursiveRefSchema, build_fixture, render_module, to_d42
from geas.models import AllOfNode, RefNode
from geas.runtime.validation import json_schema_validator
from support import Project, make_project, spec


def build_project(root: Path) -> Project:
    project = make_project(root, package="polymorphic_contracts")
    project.write_spec("api/spec.yaml", spec("features", "allof_polymorphic.yaml"))
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})
    return project


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    built = build_project(tmp_path_factory.mktemp("allof_polymorphic"))
    built.update()
    return built


@pytest.fixture(scope="module")
def operations(project: Project) -> Iterator[Any]:
    with project.importable() as generated:
        yield generated.operations


@pytest.fixture(scope="module")
def block(operations: Any) -> Any:
    return operations.main.get_block.d42_schema(Direction.RESPONSE)


def module_source(project: Project, name: str) -> str:
    return (project.output_dir / "_d42" / f"{name}.py").read_text(encoding="utf-8")


def text_block(**extra: Any) -> dict[str, Any]:
    return {"kind": "TEXT", "text": "Привет", **extra}


def image_block(**extra: Any) -> dict[str, Any]:
    return {"kind": "IMAGE", "url": "https://example.invalid/a.png", **extra}


def list_block(*items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"kind": "LIST", "items": list(items), **extra}


# ------------------------------------------------------------ JSON Schema


def test_json_schema_keeps_the_all_of(project: Project) -> None:
    """Распределение — только d42-проекция: документ контракта хранит ``allOf`` как есть."""
    schema = project.contract_document("main__get_block")["responses"][0]["schema"]

    base, union = schema["$defs"]["Block"]["allOf"]
    assert base["properties"]["kind"]["enum"] == ["TEXT", "IMAGE", "LIST"]
    assert [item["$ref"] for item in union["oneOf"]] == [
        "#/$defs/TextBlock",
        "#/$defs/ImageBlock",
        "#/$defs/ListBlock",
    ]


# ------------------------------------------------------------ распределение


def test_every_provable_direction_gets_d42(project: Project) -> None:
    documents = {
        slug: project.contract_document(slug)["d42"]
        for slug in (
            "main__get_block",
            "main__pin_block",
            "main__add_reaction",
            "main__stamp_document",
        )
    }

    assert documents["main__get_block"]["response_module"] == "main__get_block_response"
    assert documents["main__pin_block"]["request_module"] == "main__pin_block_request"
    assert documents["main__pin_block"]["response_module"] == "main__pin_block_response"
    assert documents["main__add_reaction"]["request_module"] == "main__add_reaction_request"
    assert documents["main__stamp_document"]["response_module"] == ("main__stamp_document_response")
    assert documents["main__stamp_document"]["request_module"] is None
    for slug in ("main__get_block", "main__pin_block", "main__add_reaction"):
        assert "reason" not in documents[slug], slug


def test_branches_that_equal_their_variant_stay_references(project: Project) -> None:
    """``base ∧ TextBlock`` равно ``TextBlock``: в модуле ссылка, а не инлайн-копия.

    ``ImageBlock`` не объявляет ``meta`` и ``style`` базы, поэтому его ветка честно
    отличается от самого ``ImageBlock`` и печатается словарём со свойствами базы.
    """
    source = module_source(project, "main__get_block_response")

    assert (
        "GeneratedBlockSchema = schema.any(\n"
        "    GeneratedTextBlockSchema,\n"
        "    schema.dict({\n"
        '        "kind": schema.str("IMAGE"),\n'
        '        optional("meta"): GeneratedBlockMetaSchema,\n'
        '        optional("style"): GeneratedStyleSchema,\n'
        '        "url": schema.str,\n'
        "        ...: ...,\n"
        "    }),\n"
        "    GeneratedListBlockSchema,\n"
        ")\n"
    ) in source


def test_polymorphic_reference_collapses_to_its_single_branch(project: Project) -> None:
    """``Style = allOf({name: string}, oneOf[PlainStyle])`` равен ``PlainStyle``.

    Поэтому ``Block.style ∧ TextBlock.style`` (ссылка на полиморфный DTO против
    ссылки на его вариант) — это ``PlainStyle``, и ``TextBlock`` остаётся ссылкой.
    """
    source = module_source(project, "main__get_block_response")

    assert "GeneratedStyleSchema = GeneratedPlainStyleSchema\n" in source
    assert 'optional("style"): GeneratedPlainStyleSchema,' in source


def test_inheritance_through_all_of_ref_is_an_alias(project: Project) -> None:
    """``PinnedBlock: allOf: [$ref Block]`` не добавляет ограничений — это тот же ``Block``."""
    source = module_source(project, "main__pin_block_request")

    assert "GeneratedPinnedBlockSchema = GeneratedBlockSchema\n" in source


def test_recursion_through_a_branch_is_cut_by_the_usual_mechanism(
    project: Project, block: Any
) -> None:
    """Распределение идёт до отсечки: ``ListBlock.items → Block`` — обычный цикл."""
    artifacts = project.render()

    assert ("main.getBlock", "response", "ListBlock", "Block") in artifacts.d42_cycle_cuts
    assert isinstance(block, AnySchema)
    items, _ = block.props.types[2].props.keys["items"]
    assert isinstance(items, ListSchema)
    assert isinstance(items.props.type, RecursiveRefSchema)
    assert items.props.type.name == "Block"


def test_generated_module_equals_the_in_memory_conversion(project: Project) -> None:
    """``exec`` generated-модуля и ``to_d42`` дают равные схемы и для распределения."""
    built = project.build().by_key("main.pinBlock")
    definitions = dict(built.request_definitions)
    root = built.contract.request.bodies[0].schema
    assert isinstance(root, RefNode)
    assert isinstance(definitions["PinnedBlock"], AllOfNode)

    source = render_module(
        module_docstring="Проверка эквивалентности.",
        definitions=definitions,
        exports={},
        roots=[root],
    )
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)

    assert namespace["GeneratedPinnedBlockSchema"] == to_d42(root, definitions)
    assert namespace["GeneratedBlockSchema"] == to_d42(
        RefNode(origin=root.origin, name="Block"), definitions
    )


# ------------------------------------------------------------ точность


VALID_BLOCKS = [
    text_block(),
    text_block(meta={"wordCount": 3, "lang": "ru"}, style={"name": "plain", "color": "red"}),
    image_block(),
    image_block(meta={"any": ["thing"]}, style={"name": "plain"}, caption="свободное поле"),
    list_block(),
    list_block(
        text_block(), image_block(), list_block(text_block(meta={})), style={"name": "plain"}
    ),
]


@pytest.mark.parametrize("value", VALID_BLOCKS, ids=lambda value: value["kind"])
def test_d42_accepts_everything_the_contract_accepts(
    value: dict[str, Any], block: Any, operations: Any
) -> None:
    """Пересечение не сужает контракт: валидное по JSON Schema проходит d42."""
    operations.main.get_block.validate_response(value, status=200)
    validate_or_fail(block, value)


INVALID_BLOCKS = {
    "meta базы не объект": image_block(meta="x"),
    "style базы чужого вида": image_block(style={"name": "bold"}),
    "meta варианта нарушает TextMeta": text_block(meta={"wordCount": -1}),
    "вид вне базы и вариантов": {"kind": "VIDEO", "url": "https://example.invalid/v"},
    "LIST без items": {"kind": "LIST"},
    "пустой text": text_block(text=""),
    "вложенный блок нарушает контракт": list_block(image_block(meta=1)),
}


@pytest.mark.parametrize("value", INVALID_BLOCKS.values(), ids=list(INVALID_BLOCKS))
def test_d42_rejects_what_the_base_or_the_variant_forbids(
    value: dict[str, Any], block: Any, operations: Any
) -> None:
    """Ограничения базы не теряются: d42 отклоняет то же, что и JSON Schema."""
    with pytest.raises(ValidationFailedError):
        operations.main.get_block.validate_response(value, status=200)
    with pytest.raises(ValidationException):
        validate_or_fail(block, value)


def test_generated_values_satisfy_the_contract(project: Project, block: Any) -> None:
    """``build_fixture`` и ``fake()`` по любой ветке дают значения, валидные по контракту.

    ``format`` d42 не генерирует (шапка конвертера), поэтому контракт проверяется
    без него — ровно как делает лист отсечки цикла.
    """
    schema = project.contract_document("main__get_block")["responses"][0]["schema"]
    validator = json_schema_validator(schema, check_formats=False)

    values = [build_fixture(block, seed=seed) for seed in range(5)]
    values += [fake(block) for _ in range(100)]

    assert {value["kind"] for value in values} == {"TEXT", "IMAGE", "LIST"}
    for value in values:
        assert not list(validator.iter_errors(value)), value


def test_substitution_selects_a_branch(block: Any) -> None:
    """``%`` работает на распределённом объединении как на любом ``schema.any``."""
    pinned = block % image_block()

    assert fake(pinned)["url"] == "https://example.invalid/a.png"
    validate_or_fail(pinned, image_block())


# ------------------------------------------------------------ пустые ветки


def test_empty_branch_is_dropped_without_changing_the_meaning(
    project: Project, operations: Any
) -> None:
    """``Heart.kind = HEART`` не входит в ``enum`` базы: ветка не допускает значений.

    Выбросить её точно — объединение без неё допускает те же значения.
    """
    source = module_source(project, "main__add_reaction_request")
    assert "GeneratedReactionSchema = GeneratedLikeSchema\n" in source

    reaction = operations.main.add_reaction.d42_schema(Direction.REQUEST)
    validate_or_fail(reaction, {"kind": "LIKE", "member": "anna"})
    for value in ({"kind": "HEART"}, {"kind": "DISLIKE"}):
        with pytest.raises(ValidationException):
            validate_or_fail(reaction, value)
        with pytest.raises(ValidationFailedError):
            operations.main.add_reaction.validate_request_body(value)


def test_update_reports_dropped_branches_as_a_spec_bug(project: Project) -> None:
    artifacts = project.render()
    reason = "обязательное свойство 'kind': enum ['LIKE', 'DISLIKE'] и ['HEART'] не пересекаются"

    assert artifacts.d42_dropped_branches == (
        ("main.addReaction", "request", "Reaction:/", "Heart", reason),
        ("main.stampDocument", "response", "Reaction:/", "Heart", reason),
    )
    result = project.cli("update")
    assert result.returncode == 0, result.stderr
    assert (
        f"main.addReaction (request): ветка Heart в allOf Reaction:/ выброшена из d42 — {reason}"
        in result.stdout
    )


# ------------------------------------------------------------ fail closed


def test_unprovable_branch_fails_closed_in_its_direction_only(
    project: Project, operations: Any
) -> None:
    """Два разных ``pattern`` d42 не выражает; причина называет ветку и contract path."""
    document = project.contract_document("main__stamp_document")
    reason = document["d42"]["request_reason"]

    assert "allOf не выражается в d42" in reason
    assert "ветки DigitStamp" in reason
    assert "pattern" in reason
    assert "[contract path Stamp:/code]" in reason
    handle = operations.main.stamp_document
    with pytest.raises(OperationLookupError, match="DigitStamp"):
        handle.d42_schema(Direction.REQUEST)
    validate_or_fail(handle.d42_schema(Direction.RESPONSE), {"kind": "LIKE"})


# ------------------------------------------------------------ required без схемы


def test_required_key_without_schema_is_kept_in_the_contract(project: Project) -> None:
    """``required: [caption, kind]`` рядом с ``allOf`` не теряется в JSON Schema."""
    document = project.contract_document("main__caption_block")
    captioned = document["request"]["bodies"][0]["schema"]["$defs"]["CaptionedBlock"]

    assert captioned["allOf"][0] == {"required": ["caption", "kind"], "type": "object"}


def test_required_key_without_schema_is_required_in_every_branch(operations: Any) -> None:
    handle = operations.main.caption_block
    captioned = handle.d42_schema(Direction.REQUEST)

    for value in (text_block(caption="c"), image_block(caption="c"), list_block(caption="c")):
        handle.validate_request_body(value)
        validate_or_fail(captioned, value)
    for value in (text_block(), image_block(), list_block()):
        with pytest.raises(ValidationFailedError, match="caption"):
            handle.validate_request_body(value)
        with pytest.raises(ValidationException):
            validate_or_fail(captioned, value)


def test_describe_shows_required_keys_without_schema(operations: Any) -> None:
    text = operations.main.caption_block.describe_request_body(max_depth=2)

    assert "├── allOf[0] — object; дополнительные свойства разрешены" in text
    assert "│   ├── caption — required; тип не указан" in text


#: springdoc-DTO, у которого вид задаёт только ``discriminator.mapping``: варианты
#: поле ``petType`` не сужают, ``enum`` есть лишь у базы.
_MAPPED_PETS = """\
openapi: "3.0.3"
info: {title: Mapped pets, version: "1.0.0"}
paths:
  /pets:
    get:
      operationId: getPet
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Pet"
components:
  schemas:
    Pet:
      type: object
      required: [petType]
      properties:
        petType: {type: string, enum: [cat, dog]}
      discriminator:
        propertyName: petType
        mapping:
          cat: "#/components/schemas/Cat"
          dog: "#/components/schemas/Dog"
      oneOf:
        - $ref: "#/components/schemas/Cat"
        - $ref: "#/components/schemas/Dog"
    Cat:
      type: object
      required: [meow]
      properties:
        meow: {type: boolean}
    Dog:
      type: object
      required: [bark]
      properties:
        bark: {type: string}
"""


def test_discriminator_mapping_keeps_generated_values_inside_the_contract(
    tmp_path: Path,
) -> None:
    """``fake()`` не собирает ветку одного варианта с меткой другого.

    ``if petType == dog then Dog`` вместе с ``oneOf`` запрещает ``{petType: dog,
    meow: true}``. Без учёта ``mapping`` d42 принимал и генерировал такое значение, и
    около половины фикстур мок отклонял при регистрации.
    """
    project = make_project(tmp_path, package="mapped_pets")
    project.write("api/spec.yaml", _MAPPED_PETS)
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})
    project.update()

    with project.importable() as generated:
        handle = generated.operations.main.get_pet
        pet = handle.d42_schema(Direction.RESPONSE)

        values = [build_fixture(pet, seed=seed) for seed in range(5)]
        values += [fake(pet) for _ in range(200)]
        for value in values:
            handle.validate_response(value, status=200)
        assert {value["petType"] for value in values} == {"cat", "dog"}

        with pytest.raises(ValidationException):
            validate_or_fail(pet, {"petType": "dog", "meow": True})

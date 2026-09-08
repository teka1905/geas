"""Рендер IR → исходник модуля со схемами d42.

Главное утверждение файла — **анти-дрейф**: текст, который печатает
:func:`~openapi_contracts.integrations.d42.renderer.render_module`, после ``exec``
даёт схемы, равные (``==``) результату
:func:`~openapi_contracts.integrations.d42.converter.to_d42` для тех же узлов IR.
Если однажды в рендере заведётся ветка «только для текста», закоммиченный
артефакт разойдётся с тем, что библиотека проверяет в рантайме, и тест обязан
упасть первым.

Остальное — детерминизм. Артефакт лежит в репозитории потребителя и попадает в
diff ревью, поэтому перегенерация без изменения контракта обязана давать
байт-в-байт тот же файл: порядок определений, порядок ключей, кавычки, ``__all__``,
переводы строк.
"""

from __future__ import annotations

import itertools
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from d42 import optional, schema

from openapi_contracts.errors import (
    ArtifactError,
    NamespaceCollisionError,
    RefResolutionError,
)
from openapi_contracts.integrations.d42.converter import to_d42
from openapi_contracts.integrations.d42.renderer import (
    LINE_LENGTH,
    render_expression,
    render_module,
)
from openapi_contracts.models import (
    AdditionalProperties,
    ArrayNode,
    BooleanNode,
    IntegerNode,
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
from support import SRC_ROOT

ORIGIN = Origin(source="demo.yaml", pointer="/components/schemas/Demo")


def prop(name: str, node: SchemaNode, *, required: bool = True) -> PropertySpec:
    return PropertySpec(name=name, schema=node, required=required)


def obj(*properties: PropertySpec, closed: bool = False, nullable: bool = False) -> ObjectNode:
    return ObjectNode(
        origin=ORIGIN,
        nullable=nullable,
        properties=properties,
        additional_properties=(
            AdditionalProperties.FORBIDDEN if closed else AdditionalProperties.ALLOWED
        ),
    )


def rich_sample() -> tuple[dict[str, SchemaNode], dict[str, SchemaNode]]:
    """Насыщенный бандл: все виды узлов, ссылки, вложенность и длинные строки.

    Специально без литералов, похожих на дату или путь: по нему проверяется, что
    в артефакте нет ничего «самодвижущегося».
    """
    member = obj(
        prop("identifier", StringNode(origin=ORIGIN, min_length=8, max_length=64)),
        prop("nickname", StringNode(origin=ORIGIN, pattern="^[a-z0-9_]+$"), required=False),
        prop(
            "role",
            StringNode(origin=ORIGIN, enum=("viewer", "editor", "owner")),
            required=False,
        ),
        prop("active", BooleanNode(origin=ORIGIN), required=False),
        closed=True,
    )
    section = obj(
        prop("heading", StringNode(origin=ORIGIN, max_length=200)),
        prop("weight", IntegerNode(origin=ORIGIN, minimum=0, maximum=999), required=False),
        prop(
            "kind",
            UnionNode(
                origin=ORIGIN,
                kind=UnionKind.ONE_OF,
                variants=(
                    StringNode(origin=ORIGIN, enum=("plain",)),
                    IntegerNode(origin=ORIGIN, exclusive_minimum=0),
                ),
            ),
            required=False,
        ),
    )
    page = obj(
        prop("owner", RefNode(origin=ORIGIN, name="Member")),
        prop(
            "sections",
            ArrayNode(
                origin=ORIGIN,
                items=RefNode(origin=ORIGIN, name="Section"),
                min_items=1,
                max_items=4,
            ),
        ),
        prop(
            "keywords",
            ArrayNode(
                origin=ORIGIN,
                items=StringNode(origin=ORIGIN, min_length=1),
                unique_items=True,
            ),
            required=False,
        ),
        prop("ratio", NumberNode(origin=ORIGIN, minimum=0, maximum=1)),
        prop("cursor", StringNode(origin=ORIGIN, nullable=True), required=False),
        prop(
            "matrix",
            ArrayNode(
                origin=ORIGIN,
                items=ArrayNode(
                    origin=ORIGIN,
                    items=obj(
                        prop(
                            "quite_long_cell_name_alpha", StringNode(origin=ORIGIN, max_length=32)
                        ),
                        prop(
                            "quite_long_cell_name_bravo", StringNode(origin=ORIGIN, max_length=32)
                        ),
                        prop(
                            "quite_long_cell_name_delta", StringNode(origin=ORIGIN, max_length=32)
                        ),
                        closed=True,
                    ),
                ),
            ),
            required=False,
        ),
    )
    definitions: dict[str, SchemaNode] = {"Member": member, "Section": section, "Page": page}
    exports: dict[str, SchemaNode] = {
        "DemoResponseSchema": obj(
            prop("page", RefNode(origin=ORIGIN, name="Page")),
            prop("total", IntegerNode(origin=ORIGIN, minimum=0)),
            closed=True,
        ),
        "AnotherResponseSchema": RefNode(origin=ORIGIN, name="Member", nullable=True),
    }
    return definitions, exports


def execute(source: str) -> dict[str, Any]:
    """Выполнить сгенерированный модуль и вернуть его namespace."""
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)
    return namespace


# ================================================================== анти-дрейф


def test_rendered_module_executes_into_schemas_equal_to_to_d42() -> None:
    """Ключевой инвариант: текст артефакта и объект в памяти — одно и то же."""
    definitions, exports = rich_sample()
    namespace = execute(
        render_module(
            module_docstring="Схемы демо-операции.", definitions=definitions, exports=exports
        )
    )

    for name, node in definitions.items():
        variable = f"Generated{name}Schema"
        assert namespace[variable] == to_d42(node, definitions), name
    for name, node in exports.items():
        assert namespace[name] == to_d42(node, definitions), name


def test_rendered_module_matches_to_d42_for_wrapped_expressions() -> None:
    """Разложенное по строкам выражение остаётся тем же объектом, что и однострочное."""
    definitions, _ = rich_sample()
    node = definitions["Page"]
    source = render_module(
        module_docstring="Многострочный случай.",
        definitions={"Member": definitions["Member"], "Section": definitions["Section"]},
        exports={"PageSchema": node},
    )

    assert "schema.dict({\n" in source, "выражение обязано было разложиться по строкам"
    assert execute(source)["PageSchema"] == to_d42(node, definitions)


def test_exotic_property_names_survive_the_round_trip() -> None:
    """Кавычки и не-ASCII в именах не должны ломать ни литерал, ни ``exec``."""
    node = obj(
        prop("it's", StringNode(origin=ORIGIN)),
        prop('say "hi"', IntegerNode(origin=ORIGIN)),
        prop("название", BooleanNode(origin=ORIGIN), required=False),
        closed=True,
    )
    source = render_module(module_docstring="Экзотика.", definitions={}, exports={"S": node})

    assert execute(source)["S"] == to_d42(node, {})


def test_closed_and_open_dicts_render_differently() -> None:
    closed = render_module(module_docstring="d", definitions={}, exports={"S": obj(closed=True)})
    opened = render_module(module_docstring="d", definitions={}, exports={"S": obj()})

    assert "S = schema.dict({})" in closed
    assert "...: ..." not in closed
    assert "S = schema.dict({...: ...})" in opened
    assert execute(closed)["S"] != execute(opened)["S"]


# ================================================================== упорядочение


def test_definitions_are_printed_after_their_dependencies() -> None:
    definitions = {
        "Alpha": obj(prop("z", RefNode(origin=ORIGIN, name="Zulu")), closed=True),
        "Zulu": obj(prop("v", StringNode(origin=ORIGIN)), closed=True),
    }
    source = render_module(module_docstring="d", definitions=definitions, exports={})

    assert source.index("GeneratedZuluSchema = ") < source.index("GeneratedAlphaSchema = ")


def test_independent_definitions_are_printed_alphabetically() -> None:
    definitions = {
        name: obj(prop("v", StringNode(origin=ORIGIN)), closed=True)
        for name in ("Zulu", "Mike", "Alpha")
    }
    source = render_module(module_docstring="d", definitions=definitions, exports={})
    printed = re.findall(r"^(Generated\w+Schema) = ", source, flags=re.MULTILINE)

    assert printed == ["GeneratedAlphaSchema", "GeneratedMikeSchema", "GeneratedZuluSchema"]


def test_exports_are_printed_after_definitions_and_alphabetically() -> None:
    definitions, exports = rich_sample()
    source = render_module(module_docstring="d", definitions=definitions, exports=exports)
    # ``__all__ = [`` тоже подходит под шаблон присваивания — его надо отбросить,
    # иначе «последние два присваивания» это экспорт и __all__.
    assignments = [
        name for name in re.findall(r"^(\w+) = ", source, flags=re.MULTILINE) if name != "__all__"
    ]

    assert assignments[-2:] == ["AnotherResponseSchema", "DemoResponseSchema"]
    assert set(assignments[:-2]) == {
        "GeneratedMemberSchema",
        "GeneratedPageSchema",
        "GeneratedSectionSchema",
    }


def test_reordering_the_input_does_not_move_a_single_line() -> None:
    """Перестановка ключей в спецификации не обязана трогать diff артефакта."""
    definitions, exports = rich_sample()
    expected = render_module(module_docstring="d", definitions=definitions, exports=exports)

    for order in itertools.permutations(definitions.items()):
        for export_order in itertools.permutations(exports.items()):
            actual = render_module(
                module_docstring="d", definitions=dict(order), exports=dict(export_order)
            )
            assert actual == expected


def test_mapping_and_sequence_inputs_are_equivalent() -> None:
    definitions, exports = rich_sample()

    assert render_module(
        module_docstring="d", definitions=list(definitions.items()), exports=list(exports.items())
    ) == render_module(module_docstring="d", definitions=definitions, exports=exports)


# =================================================================== детерминизм


def test_rendering_twice_gives_the_same_text() -> None:
    definitions, exports = rich_sample()

    assert render_module(
        module_docstring="Демо.", definitions=definitions, exports=exports
    ) == render_module(module_docstring="Демо.", definitions=definitions, exports=exports)


@pytest.mark.parametrize("hash_seed", ["0", "1", "17", "random"])
def test_rendering_is_stable_across_hash_seeds(hash_seed: str, tmp_path: Path) -> None:
    """Порядок обхода не должен зависеть от рандомизации хэшей строк.

    Проверяется в подпроцессе: ``PYTHONHASHSEED`` читается только при старте
    интерпретатора, поэтому внутри текущего процесса это не воспроизвести.
    """
    code = textwrap.dedent(
        """
        from openapi_contracts.integrations.d42.renderer import render_module
        from openapi_contracts.models import (
            AdditionalProperties, ArrayNode, IntegerNode, ObjectNode, Origin,
            PropertySpec, RefNode, StringNode,
        )

        origin = Origin(source="demo.yaml", pointer="/x")

        def prop(name, node, required=True):
            return PropertySpec(name=name, schema=node, required=required)

        def obj(*props):
            return ObjectNode(
                origin=origin,
                properties=props,
                additional_properties=AdditionalProperties.FORBIDDEN,
            )

        definitions = {
            "Zulu": obj(prop("value", StringNode(origin=origin, min_length=1))),
            "Alpha": obj(prop("zulu", RefNode(origin=origin, name="Zulu"))),
            "Mike": obj(prop("count", IntegerNode(origin=origin, minimum=0))),
            "Kilo": obj(
                prop("items", ArrayNode(origin=origin, items=RefNode(origin=origin, name="Mike"))),
                prop("alpha", RefNode(origin=origin, name="Alpha")),
            ),
        }
        exports = {"OneSchema": RefNode(origin=origin, name="Kilo"), "TwoSchema": obj()}
        print(render_module(module_docstring="d", definitions=definitions, exports=exports))
        """
    )
    environment = {**os.environ, "PYTHONPATH": str(SRC_ROOT), "PYTHONHASHSEED": hash_seed}
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
            env=environment,
        ).stdout
        for _ in range(2)
    }

    assert len(outputs) == 1
    assert "GeneratedZuluSchema" in next(iter(outputs))


# ====================================================================== формат


def test_module_layout_is_stable() -> None:
    definitions, exports = rich_sample()
    source = render_module(module_docstring="Демо.", definitions=definitions, exports=exports)

    assert source.startswith('"""Демо.\n')
    assert "Файл сгенерирован автоматически" in source
    assert "from __future__ import annotations" in source
    assert "from d42 import optional, schema" in source


def test_text_uses_lf_and_exactly_one_trailing_newline() -> None:
    definitions, exports = rich_sample()
    source = render_module(module_docstring="Демо.", definitions=definitions, exports=exports)

    assert "\r" not in source
    assert source.endswith("\n")
    assert not source.endswith("\n\n")
    assert source.encode("utf-8").decode("utf-8") == source


def test_no_line_has_trailing_whitespace() -> None:
    definitions, exports = rich_sample()
    source = render_module(module_docstring="Демо.", definitions=definitions, exports=exports)

    assert [line for line in source.split("\n") if line != line.rstrip()] == []


def test_all_is_sorted_and_lists_every_public_name() -> None:
    definitions, exports = rich_sample()
    source = render_module(module_docstring="d", definitions=definitions, exports=exports)
    names = re.search(r"__all__ = \[\n(.*?)\n\]", source, flags=re.DOTALL)

    assert names is not None
    listed = re.findall(r'"(\w+)"', names.group(1))
    assert listed == sorted(listed)
    # ``exec`` возвращает и служебные имена модуля: их не должно быть в __all__.
    service_names = {"__builtins__", "__doc__", "__all__", "annotations", "optional", "schema"}
    assert set(listed) == set(execute(source)) - service_names


def test_empty_bundle_still_declares_a_typed_all() -> None:
    source = render_module(module_docstring="", definitions={}, exports={})

    assert "__all__: list[str] = []" in source
    assert execute(source)["__all__"] == []


def test_output_contains_no_timestamps_paths_or_versions(tmp_path: Path) -> None:
    """В артефакте не должно быть ничего, что меняется само по себе."""
    definitions, exports = rich_sample()
    source = render_module(module_docstring="Демо.", definitions=definitions, exports=exports)

    assert re.search(r"\d{4}-\d{2}-\d{2}", source) is None
    assert re.search(r"\d{2}:\d{2}:\d{2}", source) is None
    assert str(tmp_path) not in source
    assert str(Path.cwd()) not in source
    assert sys.version.split()[0] not in source
    assert "/Users/" not in source
    assert "\\Users\\" not in source


def test_lines_fit_the_limit_when_the_expression_is_splittable() -> None:
    definitions, exports = rich_sample()
    source = render_module(module_docstring="Демо.", definitions=definitions, exports=exports)

    assert [line for line in source.split("\n") if len(line) > LINE_LENGTH] == []


def test_unsplittable_atom_is_allowed_to_overflow() -> None:
    """Как и ``black``, рендер не рвёт атом ради колонки: длинное имя остаётся строкой."""
    name = "a" * (LINE_LENGTH + 20)
    node = obj(prop(name, StringNode(origin=ORIGIN, min_length=1, max_length=2)), closed=True)
    source = render_module(module_docstring="d", definitions={}, exports={"S": node})
    long_lines = [line for line in source.split("\n") if len(line) > LINE_LENGTH]

    assert len(long_lines) == 1
    assert name in long_lines[0]
    assert execute(source)["S"] == to_d42(node, {})


def test_string_literals_prefer_double_quotes() -> None:
    node = obj(prop("visibility", StringNode(origin=ORIGIN, enum=("private",))), closed=True)
    source = render_module(module_docstring="d", definitions={}, exports={"S": node})

    assert 'schema.str("private")' in source
    assert "'private'" not in source


# ============================================================ render_expression


def test_render_expression_uses_the_supplied_variable_names() -> None:
    definitions = {"Label": obj(prop("text", StringNode(origin=ORIGIN)), closed=True)}
    node = obj(prop("label", RefNode(origin=ORIGIN, name="Label")), closed=True)

    assert (
        render_expression(node, definitions, refs={"Label": "MyLabelSchema"})
        == 'schema.dict({"label": MyLabelSchema})'
    )


def test_render_expression_rejects_an_unknown_ref() -> None:
    definitions = {"Label": obj(prop("text", StringNode(origin=ORIGIN)), closed=True)}

    with pytest.raises(RefResolutionError) as info:
        render_expression(RefNode(origin=ORIGIN, name="Label"), definitions, refs={})

    assert "Label" in str(info.value)


# ========================================================================= отказы


def test_definitions_that_collapse_to_one_variable_name_collide() -> None:
    body = obj(prop("v", StringNode(origin=ORIGIN)), closed=True)

    with pytest.raises(NamespaceCollisionError) as info:
        render_module(
            module_docstring="d",
            definitions={"TicketQueue": body, "ticket_queue": body},
            exports={},
        )

    assert "GeneratedTicketQueueSchema" in str(info.value)


def test_export_colliding_with_a_definition_variable_is_rejected() -> None:
    definitions = {"Label": obj(prop("text", StringNode(origin=ORIGIN)), closed=True)}

    with pytest.raises(NamespaceCollisionError) as info:
        render_module(
            module_docstring="d",
            definitions=definitions,
            exports={"GeneratedLabelSchema": StringNode(origin=ORIGIN)},
        )

    assert "числовой суффикс не добавляется автоматически" in str(info.value)


@pytest.mark.parametrize("name", ["import", "2fa", "_private", ""])
def test_export_name_must_be_a_public_identifier(name: str) -> None:
    with pytest.raises(NamespaceCollisionError):
        render_module(
            module_docstring="d", definitions={}, exports={name: StringNode(origin=ORIGIN)}
        )


def test_docstring_with_triple_quotes_is_rejected() -> None:
    with pytest.raises(ArtifactError):
        render_module(module_docstring='ломает """ строку', definitions={}, exports={})


def test_docstring_ending_with_a_backslash_is_rejected() -> None:
    with pytest.raises(ArtifactError):
        render_module(module_docstring="хвост \\", definitions={}, exports={})


def test_typed_additional_properties_render_and_execute() -> None:
    """Generated-модуль использует точный custom d42-тип для динамических ключей."""
    from d42 import ValidationException, validate_or_fail

    node = ObjectNode(origin=ORIGIN, additional_properties=StringNode(origin=ORIGIN))
    source = render_module(module_docstring="d", definitions={}, exports={"S": node})
    rendered = execute(source)["S"]

    assert "from openapi_contracts.integrations.d42.typed_dict import typed_dict" in source
    assert rendered == to_d42(node, {})
    validate_or_fail(rendered, {"dynamic": "ok"})
    with pytest.raises(ValidationException):
        validate_or_fail(rendered, {"dynamic": 1})


def test_optional_import_is_actually_used_by_generated_modules() -> None:
    """Заголовок объявляет ``optional`` — значит он обязан быть нужен хотя бы иногда."""
    node = obj(prop("note", StringNode(origin=ORIGIN), required=False), closed=True)
    source = render_module(module_docstring="d", definitions={}, exports={"S": node})

    assert 'optional("note")' in source
    assert execute(source)["S"] == schema.dict({optional("note"): schema.str})

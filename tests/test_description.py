"""Описание контракта: указатели, форматтер схемы и координаты артефактов.

Три независимых блока.

1. **Указатели RFC 6901.** Экранирование сегментов и разрешение указателя — это
   то, чем описание находит цель локального ``$ref``. Ошибка здесь молча
   показала бы не ту схему, поэтому ``~0``/``~1`` проверяются отдельно.
2. **Форматтер.** Гоняется на рукописных схемах через
   :func:`geas.runtime.description.describe_contract`: так проверяется ровно
   отображение, без генерации артефактов. Главное требование — ничего не
   ослаблять: неизвестное ключевое слово обязано быть названо, а не
   превращено в «любое значение».
3. **Координаты артефактов.** Проверяются на настоящем собранном проекте: view
   обязан знать свой файл контракта, JSON Pointer, d42-модуль, имя схемы и путь
   к её исходнику — иначе от alias-файла до generated-схемы снова придётся идти
   поиском по каталогу.

Ни один тест не привязан к абсолютным путям машины: пути сравниваются с теми,
что отдал временный проект.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from geas.models import Direction
from geas.runtime import RequestBodyView, ResponseView
from geas.runtime.description import (
    describe_contract,
    escape_pointer_token,
    join_pointer,
    resolve_json_pointer,
    unescape_pointer_token,
)
from support import Project, make_project, run_isolated, spec

# --------------------------------------------------------------- помощники


def describe(schema: Mapping[str, Any] | None, **kwargs: Any) -> str:
    """Описание ответа 200 без координат артефактов."""
    return describe_contract(
        direction=Direction.RESPONSE,
        status=200,
        content_type="application/json",
        json_schema=schema,
        **kwargs,
    )


def tree(schema: Mapping[str, Any], **kwargs: Any) -> list[str]:
    """Только дерево полей: заголовок и подвал проверяются отдельными тестами."""
    lines = describe(schema, **kwargs).splitlines()
    start = lines.index("") + 1
    return lines[start : lines.index("", start)]


def line(schema: Mapping[str, Any], name: str, **kwargs: Any) -> str:
    """Строка дерева про поле ``name`` — без ветвей и отступов."""
    marker = f"{name} — "
    matches = [item for item in tree(schema, **kwargs) if marker in item]
    assert matches, f"в описании нет поля {name!r}:\n" + "\n".join(tree(schema, **kwargs))
    return matches[0].split("── ", 1)[-1]


def obj(properties: Mapping[str, Any], **rest: Any) -> dict[str, Any]:
    """Объект с перечисленными свойствами."""
    return {"type": "object", "properties": dict(properties), **rest}


# ------------------------------------------------------- указатели RFC 6901


def test_escape_pointer_token_encodes_tilde_and_slash() -> None:
    """``~`` и ``/`` экранируются, причём ``~`` — первым."""
    assert escape_pointer_token("a/b~c") == "a~1b~0c"
    assert escape_pointer_token("~/") == "~0~1"


def test_unescape_pointer_token_is_inverse() -> None:
    """Обратное преобразование возвращает исходный сегмент."""
    for token in ("a/b~c", "~/", "plain", "~01"):
        assert unescape_pointer_token(escape_pointer_token(token)) == token


def test_join_pointer_escapes_every_segment() -> None:
    """Указатель собирается из сегментов, каждый экранируется."""
    assert join_pointer("responses", 0, "schema") == "/responses/0/schema"
    assert join_pointer("$defs", "a/b") == "/$defs/a~1b"


def test_resolve_json_pointer_reads_escaped_segments() -> None:
    """Имя определения со слэшем находится по ``~1``, а не по сырому ``/``."""
    document = {"$defs": {"a/b": {"type": "string"}, "c~d": {"type": "integer"}}}

    assert resolve_json_pointer(document, "#/$defs/a~1b") == {"type": "string"}
    assert resolve_json_pointer(document, "#/$defs/c~0d") == {"type": "integer"}


def test_resolve_json_pointer_walks_arrays_and_returns_default() -> None:
    """Индексы массивов разрешаются; отсутствующая цель даёт значение по умолчанию."""
    document = {"responses": [{"schema": {"type": "null"}}]}

    assert resolve_json_pointer(document, "/responses/0/schema") == {"type": "null"}
    assert resolve_json_pointer(document, "/responses/1/schema") is None
    assert resolve_json_pointer(document, "/responses/x") is None
    assert resolve_json_pointer(document, "относительный") is None
    assert resolve_json_pointer(document, "") is document


# ----------------------------------------------------------------- объекты


def test_object_lists_properties_with_requirement() -> None:
    """Простой объект: корень, поля и их обязательность."""
    schema = obj({"rc": {"type": "string"}, "note": {"type": "string"}}, required=["rc"])

    assert tree(schema) == [
        "object; дополнительные свойства разрешены",
        "├── rc — required; string",
        "└── note — optional; string",
    ]


def test_requirement_comes_only_from_required_array() -> None:
    """Ни ``default``, ни ``const``, ни присутствие в ``properties`` не делают поле обязательным."""
    schema = obj(
        {
            "withDefault": {"type": "string", "default": "x"},
            "withConst": {"const": "fixed"},
            "listed": {"type": "string"},
        },
        required=["listed"],
    )

    assert "withDefault — optional;" in line(schema, "withDefault")
    assert "withConst — optional;" in line(schema, "withConst")
    assert "listed — required;" in line(schema, "listed")


def test_nested_object_is_indented_under_its_parent() -> None:
    """Вложенный объект раскрывается со сдвигом — видно, чей это ребёнок."""
    schema = obj({"owner": obj({"email": {"type": "string"}}, required=["email"])})

    assert tree(schema) == [
        "object; дополнительные свойства разрешены",
        "└── owner — optional; object; дополнительные свойства разрешены",
        "    └── email — required; string",
    ]


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "object"}, "дополнительные свойства разрешены"),
        ({"type": "object", "additionalProperties": True}, "дополнительные свойства разрешены"),
        ({"type": "object", "additionalProperties": False}, "дополнительные свойства запрещены"),
        (
            {"type": "object", "additionalProperties": {"type": "string"}},
            "дополнительные свойства: string",
        ),
    ],
)
def test_additional_properties_keeps_draft_semantics(schema: dict[str, Any], expected: str) -> None:
    """Отсутствующий ``additionalProperties`` — это «разрешены», а не «запрещены».

    Показать открытый объект как закрытый значило бы ужесточить чужой контракт:
    читатель решил бы, что лишнее поле запрещено, хотя оно законно.
    """
    assert expected in tree(schema)[0]


def test_typed_additional_properties_value_is_expanded() -> None:
    """Значение ``additionalProperties`` со структурой показывается отдельным узлом."""
    schema = {"type": "object", "additionalProperties": obj({"id": {"type": "integer"}})}

    assert tree(schema) == [
        "object; дополнительные свойства: object",
        "└── <дополнительные свойства> — object; дополнительные свойства разрешены",
        "    └── id — optional; integer",
    ]


# ------------------------------------------------------------------ массивы


def test_array_items_are_described() -> None:
    """У массива показываются ограничения и структура элемента."""
    schema = obj(
        {
            "groups": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "uniqueItems": True,
                "items": obj({"id": {"type": "integer"}, "title": {"type": "string"}}),
            }
        }
    )

    assert tree(schema) == [
        "object; дополнительные свойства разрешены",
        "└── groups — optional; array; minItems 1; maxItems 10; uniqueItems",
        "    └── items — object; дополнительные свойства разрешены",
        "        ├── id — optional; integer",
        "        └── title — optional; string",
    ]


def test_tuple_items_are_numbered() -> None:
    """Кортежная форма ``items`` показывается позиционно."""
    schema = {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}

    assert tree(schema) == [
        "array",
        "├── items[0] — string",
        "└── items[1] — integer",
    ]


# ------------------------------------------------- перечисления и константы


def test_enum_and_const_are_shown_verbatim() -> None:
    """Значения ``enum`` и ``const`` печатаются как есть."""
    schema = obj({"rc": {"type": "string", "enum": ["OK", "ERROR"]}, "kind": {"const": 7}})

    assert line(schema, "rc") == 'rc — optional; string; enum["OK", "ERROR"]'
    assert line(schema, "kind") == "kind — optional; const 7"


def test_long_enum_is_truncated_with_total() -> None:
    """Длинный ``enum`` усекается, но говорит, сколько значений всего."""
    values = [f"V{index}" for index in range(20)]

    assert "… всего 20]" in line(obj({"code": {"enum": values}}), "code")


# ------------------------------------------------------------------ nullable


@pytest.mark.parametrize(
    "field",
    [
        {"type": ["string", "null"]},
        {"type": "string", "nullable": True},
        {"type": "string", "x-nullable": True},
    ],
)
def test_nullable_is_shown_as_union(field: dict[str, Any]) -> None:
    """Три записи одной и той же возможности ``null`` печатаются одинаково."""
    assert line(obj({"note": field}), "note") == "note — optional; string | null"


# --------------------------------------------------------------- комбинаторы


def test_combinators_are_summarised() -> None:
    """``oneOf``/``anyOf``/``allOf`` показываются свёрткой по веткам."""
    one_of = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    any_of = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
    all_of = {"allOf": [{"type": "object"}, {"type": "object"}]}
    schema = obj({"a": one_of, "b": any_of, "c": all_of})

    assert line(schema, "a") == "a — optional; oneOf(string, integer)"
    assert line(schema, "b") == "b — optional; anyOf(boolean, null)"
    assert line(schema, "c").startswith("c — optional; allOf(object, object)")


def test_structured_combinator_branches_are_expanded() -> None:
    """Ветка со структурой раскрывается — иначе поля ветки нигде не видно."""
    schema = {
        "oneOf": [
            {"type": "string"},
            obj({"id": {"type": "integer"}}, required=["id"]),
        ]
    }

    assert tree(schema) == [
        "oneOf(string, object)",
        "└── oneOf[1] — object; дополнительные свойства разрешены",
        "    └── id — required; integer",
    ]


def test_combinator_next_to_type_is_kept() -> None:
    """Комбинатор рядом с объявленным ``type`` не теряется."""
    schema = {"type": "string", "anyOf": [{"format": "uuid"}, {"format": "email"}]}

    assert tree(schema)[0].startswith("string; anyOf(")


# --------------------------------------------------------------------- $ref


@pytest.mark.parametrize("section", ["$defs", "definitions"])
def test_local_ref_is_expanded(section: str) -> None:
    """Локальная ссылка раскрывается: и ``$defs``, и ``definitions``."""
    schema = {
        section: {"Item": obj({"id": {"type": "integer"}}, required=["id"])},
        "$ref": f"#/{section}/Item",
    }

    assert tree(schema) == [
        f"$ref #/{section}/Item; object; дополнительные свойства разрешены",
        "└── id — required; integer",
    ]


def test_recursive_ref_is_marked_and_stops() -> None:
    """Рекурсия называется явно и не уводит обход в бесконечность."""
    schema = {
        "$defs": {
            "Node": obj({"child": {"$ref": "#/$defs/Node"}, "name": {"type": "string"}}),
        },
        "$ref": "#/$defs/Node",
    }

    assert tree(schema) == [
        "$ref #/$defs/Node; object; дополнительные свойства разрешены",
        "├── child — optional; $ref #/$defs/Node (рекурсия)",
        "└── name — optional; string",
    ]


def test_mutually_recursive_refs_stop_too() -> None:
    """Взаимный цикл ``Alpha → Beta → Alpha`` тоже обрывается маркером."""
    schema = {
        "$defs": {
            "Alpha": obj({"beta": {"$ref": "#/$defs/Beta"}}),
            "Beta": obj({"alpha": {"$ref": "#/$defs/Alpha"}}),
        },
        "$ref": "#/$defs/Alpha",
    }

    assert tree(schema)[-1].endswith("$ref #/$defs/Alpha (рекурсия)")


def test_external_ref_is_named_but_not_resolved() -> None:
    """Внешняя ссылка только называется: описание не ходит ни в сеть, ни на диск."""
    schema = obj({"remote": {"$ref": "https://example.test/schema.json#/Item"}})

    assert line(schema, "remote") == (
        "remote — optional; $ref https://example.test/schema.json#/Item "
        "(внешняя ссылка не раскрывается)"
    )


def test_unresolvable_local_ref_is_reported() -> None:
    """Битая локальная ссылка не молчит и не притворяется пустым объектом."""
    assert "не найден в контракте" in line(obj({"x": {"$ref": "#/$defs/Missing"}}), "x")


# ------------------------------------------------------------- ограничения


def test_string_constraints_are_listed() -> None:
    """Строковые ограничения перечисляются в фиксированном порядке."""
    field = {
        "type": "string",
        "format": "uuid",
        "pattern": "^[a-z-]+$",
        "minLength": 3,
        "maxLength": 64,
    }

    assert line(obj({"slug": field}), "slug") == (
        "slug — optional; string; format uuid; pattern ^[a-z-]+$; minLength 3; maxLength 64"
    )


def test_long_pattern_is_truncated() -> None:
    """Длинный ``pattern`` усекается, чтобы не разносить строку описания."""
    field = {"type": "string", "pattern": "^" + "a" * 200 + "$"}

    rendered = line(obj({"slug": field}), "slug")
    assert rendered.endswith("…")
    assert len(rendered) < 120


def test_numeric_bounds_in_both_forms() -> None:
    """Границы печатаются и в форме 2020-12, и в булевой форме draft-4."""
    modern = {"type": "integer", "minimum": 1, "maximum": 100}
    exclusive = {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1}
    legacy = {"type": "integer", "minimum": 0, "exclusiveMinimum": True}
    schema = obj({"a": modern, "b": exclusive, "c": legacy})

    assert line(schema, "a") == "a — optional; integer; minimum 1; maximum 100"
    assert line(schema, "b") == "b — optional; number; exclusiveMinimum 0; exclusiveMaximum 1"
    assert line(schema, "c") == "c — optional; integer; exclusiveMinimum 0"


def test_integer_format_range_is_not_repeated() -> None:
    """Границы, дословно повторяющие ``int64``, не печатаются вторым разом."""
    full = {"type": "integer", "format": "int64", "minimum": -(2**63), "maximum": 2**63 - 1}
    narrow = {"type": "integer", "format": "int64", "minimum": 1, "maximum": 2**63 - 1}
    schema = obj({"id": full, "positive": narrow})

    assert line(schema, "id") == "id — optional; integer; format int64"
    assert line(schema, "positive") == (
        "positive — optional; integer; format int64; minimum 1; maximum 9223372036854775807"
    )


def test_unknown_keyword_is_named_not_dropped() -> None:
    """Неподдержанное ключевое слово названо, а узел не выдан за «любое значение»."""
    schema = obj({"amount": {"type": "integer", "multipleOf": 5, "not": {"const": 0}}})

    rendered = line(schema, "amount")
    assert "ещё: multipleOf, not" in rendered
    assert "any" not in rendered


def test_node_without_type_is_not_called_any() -> None:
    """Узел без ``type`` описывается честно, а не как «что угодно»."""
    assert line(obj({"payload": {"description": "чем угодно"}}), "payload") == (
        "payload — optional; тип не указан"
    )


def test_annotations_do_not_leak_into_constraints() -> None:
    """Аннотации (``title``, ``description``, ``example``) в ограничения не попадают."""
    field = {"type": "string", "title": "Имя", "description": "текст", "example": "x"}

    assert line(obj({"name": field}), "name") == "name — optional; string"


# ------------------------------------------------------- глубина и стабильность


def test_max_depth_truncates_with_a_marker() -> None:
    """Слишком глубокая вложенность обрезается понятным маркером."""
    schema = obj({"a": obj({"b": obj({"c": {"type": "string"}})})})

    rendered = tree(schema, max_depth=2)

    assert rendered[-1].endswith("… (вложенность глубже 2; полная схема — по пути ниже)")
    assert not any(" c — " in item for item in rendered)


def test_description_is_deterministic() -> None:
    """Повторный вызов даёт байт-в-байт тот же текст."""
    schema = obj(
        {"b": {"type": "string"}, "a": {"type": "integer"}, "c": {"enum": [2, 1]}},
        required=["a"],
    )

    assert describe(schema) == describe(schema)


# ---------------------------------------------------------- шапка и подвал


def test_header_names_operation_direction_status_and_content_type() -> None:
    """По первым строкам видно, что именно показано."""
    text = describe_contract(
        operation_key="ws2.addTicket",
        direction=Direction.RESPONSE,
        status=200,
        content_type="application/json",
        json_schema={"type": "object"},
    )

    assert text.splitlines()[:2] == ["ws2.addTicket", "Response 200 application/json"]


def test_request_header_shows_whether_body_is_required() -> None:
    """У запроса вместо статуса — обязательность тела."""
    required = describe_contract(
        direction=Direction.REQUEST,
        content_type="application/json",
        required=True,
        json_schema={"type": "object"},
    )
    optional = describe_contract(
        direction=Direction.REQUEST,
        content_type="application/json",
        required=False,
        json_schema={"type": "object"},
    )

    assert required.splitlines()[0] == "Request application/json (тело обязательно)"
    assert optional.splitlines()[0] == "Request application/json (тело необязательно)"


def test_response_without_body_is_stated_explicitly() -> None:
    """Вариант без тела не выглядит как пустой объект."""
    text = describe_contract(direction=Direction.RESPONSE, status=204, json_schema=None)

    assert text.splitlines()[0] == "Response 204 без тела"
    assert "тело отсутствует" in text


def test_footer_prints_paths_when_they_are_known() -> None:
    """В подвале — точный адрес JSON Schema и generated d42."""
    text = describe_contract(
        direction=Direction.RESPONSE,
        status=200,
        content_type="application/json",
        json_schema={"type": "object"},
        contract_path="/tmp/generated/contracts/x.json#/responses/0/schema",
        d42_reference="pkg.generated._d42.x_response:GeneratedXSchema",
        d42_source_path=Path("/tmp/generated/_d42/x_response.py"),
    )

    assert "JSON Schema:\n  /tmp/generated/contracts/x.json#/responses/0/schema" in text
    assert "d42:\n  pkg.generated._d42.x_response:GeneratedXSchema" in text
    assert "d42 source:\n  /tmp/generated/_d42/x_response.py" in text


def test_footer_says_when_d42_was_not_generated() -> None:
    """Отсутствие d42 названо прямо, а не показано пустой строкой."""
    text = describe({"type": "object"})

    assert "d42:\n  схемы не сгенерированы" in text
    assert "d42 source:" not in text


# -------------------------------------------------- координаты артефактов


def build_project(root: Path, *, package: str) -> Project:
    """Проект-потребитель, покрывающий все интересные сочетания d42 и вариантов.

    ``content.replaceContent`` — два тела запроса и d42 в обоих направлениях;
    ``api.deleteDocument`` — вариант без тела рядом с вариантом с d42;
    ``health.getHealth`` — единственный вариант со статусом ``default``;
    ``tree.getTree`` — рекурсивный контракт, для которого d42 не генерируется.
    """
    project = make_project(root, package=package)
    project.write_spec("api/openapi.yaml", spec("basic", "openapi30.yaml"))
    project.write_spec("api/content.yaml", spec("features", "multi_content_types.yaml"))
    project.write_spec("api/health.yaml", spec("features", "default_response.yaml"))
    project.write_spec("api/recursive.yaml", spec("features", "recursive.yaml"))
    project.write_manifest(
        sources={
            "main": {"path": "api/openapi.yaml", "selection": "explicit", "root": "api"},
            "content": {"path": "api/content.yaml", "selection": "explicit", "root": "api"},
            "health": {"path": "api/health.yaml", "selection": "explicit", "root": "api"},
            "tree": {"path": "api/recursive.yaml", "selection": "explicit", "root": "api"},
        },
        operations={
            "api.deleteDocument": {
                "source": "main",
                "operation_id": "deleteDocument",
                "python_path": ["api", "delete_document"],
            },
            "content.replaceContent": {
                "source": "content",
                "operation_id": "replaceDocumentContent",
                "python_path": ["content", "replace_content"],
            },
            "health.getHealth": {
                "source": "health",
                "operation_id": "getHealth",
                "python_path": ["health", "get_health"],
            },
            "tree.getTree": {
                "source": "tree",
                "operation_id": "getTree",
                "python_path": ["tree", "get_tree"],
            },
        },
    )
    project.update()
    return project


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    """Собранный проект. Один на модуль: сборка дорогая, чтение — нет."""
    return build_project(tmp_path_factory.mktemp("describe"), package="describe_contracts")


@pytest.fixture(scope="module")
def operations(project: Project) -> Iterator[Any]:
    """Корень generated namespace собранного проекта."""
    with project.importable() as module:
        yield module.operations


def test_response_view_knows_its_place_in_the_contract_file(
    project: Project, operations: Any
) -> None:
    """Вариант ответа знает файл контракта и указатель на свою схему."""
    view = operations.api.delete_document.response(status=400)

    assert view.contract_file == project.output_dir / "contracts" / "api__delete_document.json"
    assert view.contract_file.exists()
    assert view.json_pointer == "/responses/1/schema"
    assert view.contract_path == f"{view.contract_file}#/responses/1/schema"


def test_request_body_view_knows_its_place_in_the_contract_file(
    project: Project, operations: Any
) -> None:
    """У каждого тела запроса свой указатель — они не сливаются в один."""
    handle = operations.content.replace_content
    first = handle.request.body("application/json")
    second = handle.request.body("application/merge-patch+json")

    assert first.contract_file == project.output_dir / "contracts" / "content__replace_content.json"
    assert first.json_pointer == "/request/bodies/0/schema"
    assert second.json_pointer == "/request/bodies/1/schema"
    assert second.contract_path.endswith("#/request/bodies/1/schema")


def test_views_know_the_generated_d42_module_export_and_source(
    project: Project, operations: Any
) -> None:
    """d42-координаты собираются в одну ссылку и в путь к исходнику."""
    view = operations.content.replace_content.response(status=200)

    assert view.d42_module == "describe_contracts.generated._d42.content__replace_content_response"
    assert view.d42_export == "GeneratedContentReplaceContentResponse200Schema"
    assert view.d42_reference == f"{view.d42_module}:{view.d42_export}"
    assert view.d42_source_path == (
        project.output_dir / "_d42" / "content__replace_content_response.py"
    )
    assert view.d42_source_path.is_file()


def test_variant_without_its_own_d42_schema_has_no_d42_coordinates(operations: Any) -> None:
    """Вариант без своей generated-схемы не показывает чужой модуль.

    У ``deleteDocument`` d42 есть только у варианта 400. Если бы 204 наследовал
    модуль направления, путь вёл бы к схеме соседнего варианта.
    """
    empty = operations.api.delete_document.response(status=204)

    assert empty.d42_export is None
    assert empty.d42_module is None
    assert empty.d42_reference is None
    assert empty.d42_source_path is None


def test_recursive_contract_has_json_schema_but_no_d42(operations: Any) -> None:
    """Рекурсивный контракт описывается по JSON Schema; d42 честно назван отсутствующим."""
    view = operations.tree.get_tree.response(status=200)

    assert view.contract_file.exists()
    assert view.d42_module is None
    assert view.d42_reference is None
    text = view.describe()
    assert "$ref #/$defs/Node (рекурсия)" in text
    assert "d42:\n  схемы не сгенерированы" in text


def test_directly_built_views_have_no_coordinates() -> None:
    """View, собранный вручную, не выдумывает путей: все координаты — ``None``."""
    response = ResponseView(
        status=200, content_type=None, json_schema=None, headers=(), d42_export=None
    )
    body = RequestBodyView(
        content_type="application/json", required=True, json_schema={}, d42_export=None
    )

    for view in (response, body):
        assert view.contract_file is None
        assert view.json_pointer is None
        assert view.contract_path is None
        assert view.d42_module is None
        assert view.d42_reference is None
        assert view.d42_source_path is None


def test_operation_describe_methods_delegate_to_views(operations: Any) -> None:
    """``describe_*`` на операции и ``describe()`` на view дают один и тот же текст."""
    handle = operations.content.replace_content

    assert handle.describe_response(status=200) == handle.response(status=200).describe()
    assert handle.describe_request_body(content_type="application/json") == (
        handle.request.body("application/json").describe()
    )


def test_describe_names_operation_and_prints_real_paths(operations: Any) -> None:
    """В описании из реестра есть и ключ операции, и настоящие пути артефактов."""
    view = operations.content.replace_content.response(status=200)
    text = view.describe()

    assert text.splitlines()[0] == "content.replaceContent"
    assert str(view.contract_file) in text
    assert view.d42_reference in text
    assert str(view.d42_source_path) in text


def test_default_status_variant_is_describable(operations: Any) -> None:
    """Статус ``default`` — такой же вариант, как числовой."""
    text = operations.health.get_health.describe_response(status="default")

    assert text.splitlines()[1] == "Response default application/json"
    assert "status — required; string" in text


def test_describe_request_body_requires_choice_when_ambiguous(operations: Any) -> None:
    """Несколько тел запроса — выбор обязателен и здесь: молча брать первое нельзя."""
    from geas.errors import ResponseVariantError

    with pytest.raises(ResponseVariantError, match="несколько тел запроса"):
        operations.content.replace_content.describe_request_body()


# --------------------------------------------------------- работа без d42


def test_describe_works_without_the_d42_extra(project: Project) -> None:
    """Просмотр контракта не требует d42: он читает JSON Schema, а не схемы d42.

    Проверяется в чистом подпроцессе с запретом импорта ``d42`` и ``jj``:
    ``monkeypatch`` не годится, оба модуля стоят в dev-окружении.
    """
    result = run_isolated(
        "import sys\n"
        f"sys.path.insert(0, {str(project.root)!r})\n"
        "from describe_contracts.generated import operations\n"
        "print(operations.tree.get_tree.describe_response(status=200))\n"
        "print('EXPORT', operations.content.replace_content.response(status=200).d42_export)\n",
        block=("d42", "jj"),
    )

    assert result.returncode == 0, result.stderr
    assert "$ref #/$defs/Node (рекурсия)" in result.stdout
    assert "name — required; string" in result.stdout
    # Имя generated d42-схемы читается из контракта, а не импортом модуля.
    assert "EXPORT GeneratedContentReplaceContentResponse200Schema" in result.stdout

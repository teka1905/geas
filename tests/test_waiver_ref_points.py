"""Точечные waiver'ы на ``$ref``-точке контракта.

Мотивация — реальный случай ``ws2.updateField``: свойство ``spec`` записано как
``{$ref: JsonNode, readOnly: true}``, фронтенд отправляет ``spec: null``, и
``allow_null`` на ``/body/spec`` выписать было невозможно. Нормализатор сверял
waiver дважды на одном contract path — сначала на узле ``$ref``, потом на его
развёрнутой цели, — и отпечатки у них разные. Какой ни поставь в
``expected_source``, генерация требовала другой. ``extend_enum`` и
``ignore_discriminator`` на такой точке не работали вовсе: патч применялся к узлу
``$ref``, где нет ни ``enum``, ни ``discriminator``.

Правила ``allow_null``, ``extend_enum`` и ``ignore_discriminator`` действуют на
фактическую схему в точке — цель ``$ref`` с наложенными соседями — и сверяются с
ней ровно один раз. ``replace_schema`` закрепляется тем же отпечатком. Отсюда два
свойства, которые проверяются ниже: вынос фрагмента в ``$ref`` отпечаток не
меняет, а правка цели делает waiver устаревшим.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from geas.errors import UnsupportedConstructError, WaiverError
from geas.fingerprints import semantic_source_digest
from support import Project, make_project

#: Отпечаток-заглушка. Настоящий печатает сама библиотека — см. :func:`settle`.
PLACEHOLDER = "0" * 64

#: Swagger 2: ``$ref``-свойства на объект, на enum, на цепочку ссылок и на
#: рекурсивную схему. Две операции делят одну модель запроса — послабление на
#: одной не имеет права ослабить вторую.
SWAGGER = """swagger: "2.0"
info: {title: Ref points, version: "1.0.0"}
basePath: /api
paths:
  /fields/{id}:
    put:
      operationId: updateField
      consumes: [application/json]
      produces: [application/json]
      parameters:
        - {name: id, in: path, required: true, type: integer}
        - in: body
          name: body
          required: true
          schema: {$ref: "#/definitions/FieldTemplate"}
      responses:
        "200": {description: ok, schema: {type: string}}
  /fields:
    post:
      operationId: createField
      consumes: [application/json]
      produces: [application/json]
      parameters:
        - in: body
          name: body
          required: true
          schema: {$ref: "#/definitions/FieldTemplate"}
      responses:
        "200": {description: ok, schema: {type: string}}
definitions:
  JsonNode: {type: object}
  State: {type: string, enum: [ACTIVE, INACTIVE]}
  StateAlias: {$ref: "#/definitions/State"}
  Tree:
    type: object
    properties:
      parent: {$ref: "#/definitions/Tree"}
  FieldTemplate:
    type: object
    required: [code, state]
    properties:
      code: {type: string}
      state: {$ref: "#/definitions/State"}
      aliasState: {$ref: "#/definitions/StateAlias"}
      spec: {$ref: "#/definitions/JsonNode"}
      tree: {$ref: "#/definitions/Tree"}
"""

#: Случай ``ws2.updateField``: вся модель запроса помечена ``readOnly``, а
#: ``spec`` — это ``$ref`` с соседом ``readOnly``.
SWAGGER_READ_ONLY = """swagger: "2.0"
info: {title: Read-only template, version: "1.0.0"}
basePath: /api
paths:
  /fields/{id}:
    put:
      operationId: updateField
      consumes: [application/json]
      produces: [application/json]
      parameters:
        - {name: id, in: path, required: true, type: integer}
        - in: body
          name: body
          required: true
          schema: {$ref: "#/definitions/FieldTemplate"}
      responses:
        "200": {description: ok, schema: {type: string}}
definitions:
  JsonNode: {type: object}
  FieldTemplate:
    type: object
    required: [code]
    properties:
      code: {type: string, readOnly: true}
      spec: {$ref: "#/definitions/JsonNode", readOnly: true}
"""

#: OpenAPI 3.0: ``$ref`` на объект и на полиморфную схему с ``discriminator``.
OPENAPI30 = """openapi: "3.0.3"
info: {title: Ref points 3.0, version: "1.0.0"}
paths:
  /pets:
    post:
      operationId: addPet
      requestBody:
        required: true
        content:
          application/json:
            schema: {$ref: "#/components/schemas/Envelope"}
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {type: string}
components:
  schemas:
    JsonNode: {type: object}
    Envelope:
      type: object
      properties:
        spec: {$ref: "#/components/schemas/JsonNode"}
        pet: {$ref: "#/components/schemas/Pet"}
    Pet:
      oneOf:
        - $ref: "#/components/schemas/Cat"
        - $ref: "#/components/schemas/Dog"
      discriminator:
        propertyName: kind
        mapping:
          cat: "#/components/schemas/Cat"
          dog: "#/components/schemas/Dog"
    Cat:
      type: object
      required: [kind, meow]
      properties:
        kind: {type: string}
        meow: {type: boolean}
    Dog:
      type: object
      required: [kind, bark]
      properties:
        kind: {type: string}
        bark: {type: boolean}
"""

SWAGGER_OPERATIONS = {
    "ws.updateField": {"source": "main", "operation_id": "updateField"},
    "ws.createField": {"source": "main", "operation_id": "createField"},
}
OPENAPI30_OPERATIONS = {"api.addPet": {"source": "main", "operation_id": "addPet"}}

#: Отпечаток ``JsonNode`` — ровно тот же, что у такого же фрагмента, записанного inline.
JSON_NODE_DIGEST = semantic_source_digest({"type": "object"})
#: Отпечаток ``State``.
STATE_DIGEST = semantic_source_digest({"type": "string", "enum": ["ACTIVE", "INACTIVE"]})


def in_days(days: int) -> dt.date:
    return dt.date.today() + dt.timedelta(days=days)


def ref_project(root: Path, *, source: str, operations: dict[str, Any]) -> Project:
    """Изолированный проект с одной спецификацией."""
    project = make_project(root)
    project.write_spec("api/openapi.yaml", source)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "selection": "explicit"}},
        operations=operations,
    )
    return project


def ref_waiver(
    *,
    pointer: str,
    rule: str,
    expected: str = PLACEHOLDER,
    operation: str = "ws.updateField",
    **extra: Any,
) -> dict[str, Any]:
    """Полный waiver на запрос."""
    return {
        "operation": operation,
        "direction": "request",
        "json_pointer": pointer,
        "rule": rule,
        "reason": "исходная спецификация неверно описывает фактический запрос",
        "owner": "team-api",
        "issue": "BUG-7",
        "expires_at": in_days(30),
        "expected_source": expected,
        **extra,
    }


_STALE = re.compile(r"waiver \S+ / \w+ / (?P<pointer>\S+) / (?P<rule>\w+)")
_ACTUAL = re.compile(r"фактический:\s+(?P<digest>[0-9a-f]{64})")


def settle(project: Project, waivers: list[dict[str, Any]]) -> tuple[Any, int]:
    """Пройти путь человека: собрать, вписать ``фактический`` отпечаток, повторить.

    Возвращает результат сборки и число исправлений. Каждый waiver обязан
    сойтись с первого исправления — иначе ``expected_source`` невозможно
    заполнить по подсказке библиотеки, и это и есть дефект, ради которого
    написан модуль.
    """
    waivers = [dict(item) for item in waivers]
    fixes = 0
    for _ in range(len(waivers) + 1):
        project.write_waivers(waivers)
        try:
            return project.build(), fixes
        except WaiverError as error:
            message = str(error)
            stale, actual = _STALE.search(message), _ACTUAL.search(message)
            if "устарел" not in message or stale is None or actual is None:
                raise
            for item in waivers:
                if (item["json_pointer"], item["rule"]) == (stale["pointer"], stale["rule"]):
                    item["expected_source"] = actual["digest"]
            fixes += 1
    pytest.fail(f"отпечатки не сошлись за {len(waivers)} исправлений: waiver'ы {waivers}")


def request_schema(result: Any, key: str) -> dict[str, Any]:
    document: dict[str, Any] = result.by_key(key).document
    return document["request"]["bodies"][0]["schema"]


def accepts(schema: dict[str, Any], value: Any) -> bool:
    return jsonschema.Draft202012Validator(schema).is_valid(value)


# ------------------------------------------------------------------ исходное состояние


def test_fixtures_build_without_waivers(tmp_path: Path) -> None:
    """Фикстуры сами по себе корректны: все падения ниже — про waiver'ы."""
    swagger = ref_project(tmp_path / "swagger", source=SWAGGER, operations=SWAGGER_OPERATIONS)
    openapi = ref_project(tmp_path / "openapi", source=OPENAPI30, operations=OPENAPI30_OPERATIONS)

    update = request_schema(swagger.build(), "ws.updateField")
    pets = request_schema(openapi.build(), "api.addPet")

    assert not accepts(update, {"code": "c", "state": "ACTIVE", "spec": None})
    assert not accepts(pets, {"pet": {"kind": "dog", "meow": True}})


# ------------------------------------------------------------------ allow_null


@pytest.mark.parametrize(
    ("source", "operations", "operation", "body"),
    [
        (SWAGGER, SWAGGER_OPERATIONS, "ws.updateField", {"code": "c", "state": "ACTIVE"}),
        (OPENAPI30, OPENAPI30_OPERATIONS, "api.addPet", {}),
    ],
    ids=["swagger2", "openapi30"],
)
def test_allow_null_on_ref_point_settles_after_one_fix(
    tmp_path: Path, source: str, operations: dict[str, Any], operation: str, body: dict[str, Any]
) -> None:
    """Отпечаток из сообщения об ошибке сразу подходит — без бесконечной чехарды."""
    project = ref_project(tmp_path / "project", source=source, operations=operations)

    result, fixes = settle(
        project, [ref_waiver(pointer="/body/spec", rule="allow_null", operation=operation)]
    )

    assert fixes == 1
    schema = request_schema(result, operation)
    assert accepts(schema, {**body, "spec": None})
    assert accepts(schema, {**body, "spec": {"any": "thing"}})
    assert not accepts(schema, {**body, "spec": "не объект"})


def test_ref_point_digest_equals_digest_of_the_inlined_fragment(tmp_path: Path) -> None:
    """Вынос фрагмента в ``$ref`` отпечаток не меняет: он считается по цели."""
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)
    project.write_waivers(
        [ref_waiver(pointer="/body/spec", rule="allow_null", expected=JSON_NODE_DIGEST)]
    )

    schema = request_schema(project.build(), "ws.updateField")

    assert accepts(schema, {"code": "c", "state": "ACTIVE", "spec": None})


def test_allow_null_on_read_only_ref_under_subtree_waiver(tmp_path: Path) -> None:
    """Случай ``ws2.updateField``: subtree ``ignore_read_only`` плюс ``allow_null``."""
    project = ref_project(
        tmp_path / "project",
        source=SWAGGER_READ_ONLY,
        operations={"ws.updateField": SWAGGER_OPERATIONS["ws.updateField"]},
    )

    result, fixes = settle(
        project,
        [
            ref_waiver(pointer="/body", rule="ignore_read_only", scope="subtree"),
            ref_waiver(pointer="/body/spec", rule="allow_null"),
        ],
    )

    assert fixes == 2
    schema = request_schema(result, "ws.updateField")
    assert accepts(schema, {"code": "c", "spec": None})
    assert not accepts(schema, {"spec": None}), "code снова обязателен в запросе"
    assert not accepts(schema, {"code": "c", "spec": "не объект"})


@pytest.mark.parametrize(
    "source",
    [
        SWAGGER.replace("JsonNode: {type: object}", "JsonNode: {type: object, x-nullable: true}"),
        SWAGGER.replace(
            'spec: {$ref: "#/definitions/JsonNode"}',
            'spec: {$ref: "#/definitions/JsonNode", x-nullable: true}',
        ),
    ],
    ids=["nullable-target", "nullable-sibling"],
)
def test_allow_null_on_ref_point_is_redundant_when_source_allows_null(
    tmp_path: Path, source: str
) -> None:
    """Null, разрешённый в цели или рядом с ``$ref``, делает waiver лишним."""
    project = ref_project(tmp_path / "project", source=source, operations=SWAGGER_OPERATIONS)
    project.write_waivers(
        [
            ref_waiver(
                pointer="/body/spec",
                rule="allow_null",
                expected=semantic_source_digest({"type": "object", "x-nullable": True}),
            )
        ]
    )

    with pytest.raises(UnsupportedConstructError, match="allow_null больше не нужен"):
        project.build()


def test_allow_null_on_recursive_ref_point_keeps_nested_references_strict(
    tmp_path: Path,
) -> None:
    """Рекурсивная цель: послабление только в точке, вложенный ``parent`` строгий."""
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)
    tree = yaml.safe_load(SWAGGER)["definitions"]["Tree"]
    project.write_waivers(
        [ref_waiver(pointer="/body/tree", rule="allow_null", expected=semantic_source_digest(tree))]
    )

    schema = request_schema(project.build(), "ws.updateField")

    body = {"code": "c", "state": "ACTIVE"}
    assert accepts(schema, {**body, "tree": None})
    assert accepts(schema, {**body, "tree": {"parent": {"parent": {}}}})
    assert not accepts(schema, {**body, "tree": {"parent": None}})


# ------------------------------------------------------------------ extend_enum


@pytest.mark.parametrize("pointer", ["/body/state", "/body/aliasState"], ids=["ref", "ref-chain"])
def test_extend_enum_on_ref_point_extends_the_target_enum(tmp_path: Path, pointer: str) -> None:
    """``enum`` живёт в цели ``$ref`` — там его и расширяет waiver."""
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)
    project.write_waivers(
        [
            ref_waiver(
                pointer=pointer, rule="extend_enum", expected=STATE_DIGEST, values=["INVISIBLE"]
            )
        ]
    )

    schema = request_schema(project.build(), "ws.updateField")

    field = pointer.removeprefix("/body/")
    body = {"code": "c", "state": "ACTIVE"}
    assert accepts(schema, {**body, field: "INVISIBLE"})
    assert accepts(schema, {**body, field: "INACTIVE"})
    assert not accepts(schema, {**body, field: "UNKNOWN"})


def test_extend_enum_on_ref_point_settles_after_one_fix(tmp_path: Path) -> None:
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)

    _, fixes = settle(
        project,
        [ref_waiver(pointer="/body/state", rule="extend_enum", values=["INVISIBLE"])],
    )

    assert fixes == 1


def test_extend_enum_on_ref_point_does_not_weaken_the_other_operation(tmp_path: Path) -> None:
    """Общая ``State`` остаётся строгой у операции без waiver'а."""
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)
    project.write_waivers(
        [
            ref_waiver(
                pointer="/body/state",
                rule="extend_enum",
                expected=STATE_DIGEST,
                values=["INVISIBLE"],
            )
        ]
    )

    result = project.build()

    body = {"code": "c", "state": "INVISIBLE"}
    assert accepts(request_schema(result, "ws.updateField"), body)
    assert not accepts(request_schema(result, "ws.createField"), body)


def test_ref_point_waiver_goes_stale_when_the_target_changes(tmp_path: Path) -> None:
    """Отпечаток держит цель: бэкенд поправил ``State`` — waiver надо пересмотреть."""
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)
    project.write_waivers(
        [
            ref_waiver(
                pointer="/body/state",
                rule="extend_enum",
                expected=STATE_DIGEST,
                values=["INVISIBLE"],
            )
        ]
    )
    project.build()

    project.write_spec(
        "api/openapi.yaml",
        SWAGGER.replace("enum: [ACTIVE, INACTIVE]", "enum: [ACTIVE, INACTIVE, INVISIBLE]"),
    )

    with pytest.raises(WaiverError, match="устарел"):
        project.build()


# ------------------------------------------------------------------ ignore_discriminator


def test_ignore_discriminator_on_ref_point_removes_the_target_discriminator(
    tmp_path: Path,
) -> None:
    """``discriminator`` живёт в цели ``$ref``; ``oneOf`` при этом остаётся в силе."""
    project = ref_project(tmp_path / "project", source=OPENAPI30, operations=OPENAPI30_OPERATIONS)
    pet = yaml.safe_load(OPENAPI30)["components"]["schemas"]["Pet"]
    project.write_waivers(
        [
            ref_waiver(
                pointer="/body/pet",
                rule="ignore_discriminator",
                operation="api.addPet",
                expected=semantic_source_digest(pet),
            )
        ]
    )

    schema = request_schema(project.build(), "api.addPet")

    assert accepts(schema, {"pet": {"kind": "dog", "meow": True}})
    assert not accepts(schema, {"pet": {"kind": "dog"}})


def test_ignore_discriminator_on_ref_point_settles_after_one_fix(tmp_path: Path) -> None:
    project = ref_project(tmp_path / "project", source=OPENAPI30, operations=OPENAPI30_OPERATIONS)

    _, fixes = settle(
        project,
        [ref_waiver(pointer="/body/pet", rule="ignore_discriminator", operation="api.addPet")],
    )

    assert fixes == 1


# ------------------------------------------------------------------ replace_schema

NULLABLE_OBJECT = {"type": "object", "x-nullable": True}


@pytest.mark.parametrize(
    ("pointer", "expected", "replacement", "value"),
    [
        ("/body/spec", JSON_NODE_DIGEST, NULLABLE_OBJECT, None),
        ("/body/state", STATE_DIGEST, {"type": "string", "enum": ["DRAFT"]}, "DRAFT"),
        ("/body/aliasState", STATE_DIGEST, {"type": "string", "enum": ["DRAFT"]}, "DRAFT"),
    ],
    ids=["ref", "ref-enum", "ref-chain"],
)
def test_replace_schema_on_ref_point_is_pinned_by_the_target(
    tmp_path: Path, pointer: str, expected: str, replacement: dict[str, Any], value: Any
) -> None:
    """Отпечаток ``replace_schema`` на ``$ref``-точке — тот же, что у патчей: по цели."""
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)
    project.write_waivers(
        [
            ref_waiver(
                pointer=pointer, rule="replace_schema", expected=expected, replacement=replacement
            )
        ]
    )

    schema = request_schema(project.build(), "ws.updateField")

    field = pointer.removeprefix("/body/")
    assert accepts(schema, {"code": "c", "state": "ACTIVE", field: value})


def test_replace_schema_on_ref_point_settles_after_one_fix(tmp_path: Path) -> None:
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)

    result, fixes = settle(
        project,
        [ref_waiver(pointer="/body/spec", rule="replace_schema", replacement=NULLABLE_OBJECT)],
    )

    assert fixes == 1
    assert accepts(
        request_schema(result, "ws.updateField"), {"code": "c", "state": "ACTIVE", "spec": None}
    )


def test_replace_schema_on_ref_point_goes_stale_when_the_target_changes(tmp_path: Path) -> None:
    """Замена целиком тем более обязана заметить, что бэкенд переписал цель."""
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)
    project.write_waivers(
        [
            ref_waiver(
                pointer="/body/spec",
                rule="replace_schema",
                expected=JSON_NODE_DIGEST,
                replacement=NULLABLE_OBJECT,
            )
        ]
    )
    project.build()

    project.write_spec(
        "api/openapi.yaml",
        SWAGGER.replace("JsonNode: {type: object}", "JsonNode: {type: object, x-nullable: true}"),
    )

    with pytest.raises(WaiverError, match="устарел"):
        project.build()


def test_replace_schema_pinned_to_the_bare_ref_reports_the_new_digest(tmp_path: Path) -> None:
    """Путь миграции: отпечаток узла ``$ref`` из прежних версий устарел, новый напечатан."""
    project = ref_project(tmp_path / "project", source=SWAGGER, operations=SWAGGER_OPERATIONS)
    legacy = semantic_source_digest({"$ref": "#/definitions/JsonNode"})
    project.write_waivers(
        [
            ref_waiver(
                pointer="/body/spec",
                rule="replace_schema",
                expected=legacy,
                replacement=NULLABLE_OBJECT,
            )
        ]
    )

    with pytest.raises(WaiverError, match="устарел") as info:
        project.build()

    message = str(info.value)
    assert legacy in message
    assert JSON_NODE_DIGEST in message

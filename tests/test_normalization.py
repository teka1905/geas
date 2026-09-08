"""Тесты нормализации Schema Object в IR.

Файл держит три инварианта модуля ``normalization``.

**Position-awareness.** Ключевое слово распознаётся только там, где по структуре
Schema Object обязана стоять схема. Свойство, названное ``type`` или ``items``, —
это обычное поле, а не ключевое слово. Регрессия на этом — самый дорогой класс
ошибок: она портит не одну спецификацию, а все сразу и молча.

**Fail closed.** Всё, что нельзя выразить точно, — исключение с координатами, а не
«принимает что угодно». Поэтому каждая ошибка проверяется не только по тексту, но и
по четырём координатам: источник, ключ операции, направление, JSON Pointer.

**Изоляция направления.** ``readOnly``/``writeOnly`` применяются к копии, а не к
общей схеме: оба направления строятся в одном прогоне и обязаны быть корректны
одновременно.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from geas.contracts import BuiltOperation
from geas.errors import RefResolutionError, UnsupportedConstructError
from support import Project, make_project, spec

#: Каркас операции, у которой единственный ответ описан схемой из теста.
_RESPONSE_TEMPLATE = """\
openapi: "3.0.3"
info: {title: Normalization fixture, version: "1.0.0"}
paths:
  /probe:
    get:
      operationId: getProbe
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
"""

#: Каркас операции с одним query-параметром, описанным схемой из теста.
_QUERY_TEMPLATE = """\
openapi: "3.0.3"
info: {title: Parameter fixture, version: "1.0.0"}
paths:
  /probe:
    get:
      operationId: getProbe
      parameters:
        - name: filter
          in: query
          required: false
          schema:
"""

_QUERY_TAIL = """\
      responses:
        "204":
          description: ok
"""


def build_source(
    tmp_path: Path,
    text: str,
    *,
    policies: dict[str, Any] | None = None,
    suffix: str = ".yaml",
) -> Any:
    """Собрать контракты источника, записанного во временный проект."""
    project = make_project(tmp_path / "project")
    project.write(f"api/spec{suffix}", text)
    project.write_manifest(
        sources={"main": {"path": f"api/spec{suffix}", "selection": "all"}},
        policies=policies,
    )
    return project.build()


def build_response(
    tmp_path: Path, schema: str, *, policies: dict[str, Any] | None = None
) -> BuiltOperation:
    """Собрать операцию, ответ 200 которой описан схемой ``schema``."""
    body = textwrap.indent(textwrap.dedent(schema).strip("\n"), " " * 16)
    return build_source(tmp_path, _RESPONSE_TEMPLATE + body + "\n", policies=policies).by_key(
        "main.getProbe"
    )


def build_query_parameter(tmp_path: Path, schema: str, *, extra: str = "") -> BuiltOperation:
    """Собрать операцию с единственным query-параметром ``filter``."""
    body = textwrap.indent(textwrap.dedent(schema).strip("\n"), " " * 12)
    text = _QUERY_TEMPLATE + body + "\n" + extra + _QUERY_TAIL
    return build_source(tmp_path, text).by_key("main.getProbe")


def response_schema(built: BuiltOperation, index: int = 0) -> dict[str, Any]:
    """JSON Schema тела ответа собранной операции."""
    schema = built.document["responses"][index]["schema"]
    assert schema is not None
    return schema


def fixture_project(
    tmp_path: Path,
    name: str,
    operation_id: str,
    *,
    key: str = "api.probe",
    policies: dict[str, Any] | None = None,
    folder: str = "features",
) -> Project:
    """Проект с одной операцией фикстуры: файлы с несколькими операциями проверяются по одной."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec(folder, name))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={key: {"source": "main", "operation_id": operation_id}},
        policies=policies,
    )
    return project


def assert_coordinates(
    error: Any,
    *,
    operation_key: str,
    direction: str,
    pointer_suffix: str,
    source: str = "spec.yaml",
) -> None:
    """Проверить, что ошибка несёт все четыре координаты, а не только текст."""
    assert error.source == source
    assert error.operation_key == operation_key
    assert error.direction == direction
    assert error.json_pointer is not None
    assert error.json_pointer.endswith(pointer_suffix), error.json_pointer


# ------------------------------------------------------ поддержанная матрица


def test_objects_arrays_and_scalar_constraints_survive(tmp_path: Path) -> None:
    """Базовая матрица: вложенные объекты, массивы, массивы массивов и все границы."""
    built = build_response(
        tmp_path,
        """
        type: object
        required: [name, matrix]
        minProperties: 1
        maxProperties: 12
        properties:
          name:
            type: string
            pattern: "^[a-z]+$"
            minLength: 2
            maxLength: 32
          nested:
            type: object
            properties:
              depth:
                type: integer
                minimum: 0
                maximum: 10
                multipleOf: 2
          matrix:
            type: array
            minItems: 1
            maxItems: 4
            uniqueItems: true
            items:
              type: array
              items:
                type: number
                minimum: 0.5
                maximum: 9.5
        """,
    )

    schema = response_schema(built)
    assert schema["required"] == ["matrix", "name"]
    assert schema["minProperties"] == 1
    assert schema["maxProperties"] == 12
    assert schema["properties"]["name"] == {
        "type": "string",
        "pattern": "^[a-z]+$",
        "minLength": 2,
        "maxLength": 32,
    }
    assert schema["properties"]["nested"]["properties"]["depth"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 10,
        "multipleOf": 2,
    }
    matrix = schema["properties"]["matrix"]
    assert matrix["minItems"] == 1
    assert matrix["maxItems"] == 4
    assert matrix["uniqueItems"] is True
    # Массив массивов внутри тела поддержан — в отличие от массива массивов в параметре.
    assert matrix["items"]["items"] == {"type": "number", "minimum": 0.5, "maximum": 9.5}


def test_boolean_exclusive_bounds_become_numeric(tmp_path: Path) -> None:
    """Булев ``exclusiveMinimum`` OpenAPI 3.0 разворачивается в числовой Draft 2020-12."""
    built = build_response(
        tmp_path,
        """
        type: object
        properties:
          low:
            type: integer
            minimum: 3
            exclusiveMinimum: true
          high:
            type: number
            maximum: 7.5
            exclusiveMaximum: true
          plain:
            type: integer
            minimum: 1
            exclusiveMinimum: false
        """,
    )

    properties = response_schema(built)["properties"]
    assert properties["low"] == {"type": "integer", "exclusiveMinimum": 3}
    assert properties["high"] == {"type": "number", "exclusiveMaximum": 7.5}
    # exclusiveMinimum: false — это обычная включающая граница.
    assert properties["plain"] == {"type": "integer", "minimum": 1}


def test_exclusive_flag_without_its_bound_is_rejected(tmp_path: Path) -> None:
    """``exclusiveMinimum: true`` без ``minimum`` не имеет смысла и отклоняется."""
    with pytest.raises(UnsupportedConstructError) as info:
        build_response(
            tmp_path,
            """
            type: object
            properties:
              a:
                type: integer
                exclusiveMinimum: true
            """,
        )

    assert "задан без minimum" in str(info.value)
    assert_coordinates(
        info.value,
        operation_key="main.getProbe",
        direction="response",
        pointer_suffix="/schema/properties/a",
    )


def test_enums_of_every_scalar_kind(tmp_path: Path) -> None:
    """``enum`` сохраняется точно и проверяется на однородность."""
    built = build_response(
        tmp_path,
        """
        type: object
        properties:
          text:
            type: string
            enum: [draft, review]
          number:
            type: integer
            enum: [1, 2, 3]
          flag:
            type: boolean
            enum: [true]
        """,
    )

    properties = response_schema(built)["properties"]
    assert properties["text"]["enum"] == ["draft", "review"]
    assert properties["number"]["enum"] == [1, 2, 3]
    assert properties["flag"]["enum"] == [True]


@pytest.mark.parametrize(
    ("schema", "fragment"),
    [
        ("type: string\nenum: [a, 1]", "enum обязан состоять из строк"),
        ("type: string\nenum: [a, a]", "повторяющиеся значения"),
        ("type: string\nenum: []", "непустым списком"),
    ],
)
def test_broken_enums_are_rejected(tmp_path: Path, schema: str, fragment: str) -> None:
    """Разнородный, пустой и дублирующий ``enum`` — ошибки, а не «примерно строки»."""
    with pytest.raises(UnsupportedConstructError) as info:
        build_response(tmp_path, schema)

    assert fragment in str(info.value)


def test_additional_properties_boolean_and_typed(tmp_path: Path) -> None:
    """``additionalProperties`` работает и как флаг, и как схема."""
    built = build_response(
        tmp_path,
        """
        type: object
        properties:
          closed:
            type: object
            additionalProperties: false
            properties:
              a: {type: string}
          open:
            type: object
            additionalProperties: true
            properties:
              a: {type: string}
          dictionary:
            type: object
            additionalProperties:
              type: integer
              minimum: 0
        """,
    )

    properties = response_schema(built)["properties"]
    assert properties["closed"]["additionalProperties"] is False
    # additionalProperties: true — это дефолт OpenAPI, в контракте он не пишется.
    assert "additionalProperties" not in properties["open"]
    assert properties["dictionary"]["additionalProperties"] == {
        "type": "integer",
        "minimum": 0,
    }


def test_nullable_applies_to_scalars_enums_and_refs(tmp_path: Path) -> None:
    """``nullable`` разворачивается по-разному в зависимости от вида узла."""
    built = build_response(
        tmp_path,
        """
        type: object
        properties:
          scalar:
            type: string
            nullable: true
          choice:
            type: string
            enum: [a, b]
            nullable: true
          collection:
            type: array
            nullable: true
            items: {type: string}
        """,
    )

    properties = response_schema(built)["properties"]
    assert properties["scalar"] == {"type": ["string", "null"]}
    assert properties["choice"] == {"type": ["string", "null"], "enum": ["a", "b", None]}
    assert properties["collection"]["type"] == ["array", "null"]


def test_provable_all_of_is_merged_into_one_object(tmp_path: Path) -> None:
    """``allOf`` из ``$ref`` и непересекающегося inline-объекта сливается доказуемо."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("basic", "openapi30.yaml"))
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})

    schema = project.build().by_key("main.createDocument").document["responses"][0]["schema"]

    document = schema["$defs"]["Document"]
    assert "allOf" not in document
    assert document["type"] == "object"
    # Свойства обеих частей на месте, обязательность объединена.
    assert {"id", "revision", "title", "slug", "owner"} <= set(document["properties"])
    assert document["required"] == ["id", "slug", "title"]


def test_unmergeable_all_of_stays_a_composition(tmp_path: Path) -> None:
    """Конфликт одноимённых свойств делает слияние недоказуемым — композиция сохраняется."""
    project = fixture_project(tmp_path, "allof_unmergeable.yaml", "getDocumentQuota")

    schema = project.build().by_key("api.probe").document["responses"][0]["schema"]

    assert list(schema) == ["$schema", "allOf"]
    assert schema["allOf"][0]["properties"]["size"] == {"type": "integer"}
    assert schema["allOf"][1]["properties"]["size"] == {"type": "string"}


def test_all_of_with_siblings_keeps_both_parts(tmp_path: Path) -> None:
    """Локальная часть рядом с ``allOf`` не вытесняется композицией и наоборот."""
    project = fixture_project(tmp_path, "allof_with_siblings.yaml", "getDocumentSummary")

    schema = project.build().by_key("api.probe").document["responses"][0]["schema"]

    local, composed = schema["allOf"]
    assert local["required"] == ["id"]
    assert local["properties"]["id"] == {"type": "string", "format": "uuid"}
    assert sorted(composed["properties"]) == ["createdAt", "updatedAt"]


def test_one_of_with_siblings_keeps_both_parts(tmp_path: Path) -> None:
    """``oneOf`` рядом с собственными свойствами даёт пересечение локального и союза."""
    project = fixture_project(tmp_path, "oneof_with_siblings.yaml", "getBlock")

    schema = project.build().by_key("api.probe").document["responses"][0]["schema"]

    local, composed = schema["allOf"]
    assert local["required"] == ["kind"]
    assert composed["oneOf"] == [{"$ref": "#/$defs/TextBlock"}, {"$ref": "#/$defs/ImageBlock"}]


def test_any_of_is_not_downgraded(tmp_path: Path) -> None:
    """Вид композиции сохраняется точно: ``anyOf`` остаётся ``anyOf``."""
    project = fixture_project(tmp_path, "anyof.yaml", "getLabel")

    schema = project.build().by_key("api.probe").document["responses"][0]["schema"]

    assert "oneOf" not in schema
    assert schema["anyOf"][0] == {"type": "string", "minLength": 1}
    assert schema["anyOf"][1]["required"] == ["id"]


def test_discriminator_with_mapping_becomes_real_constraints(tmp_path: Path) -> None:
    """``discriminator`` с ``mapping`` — это ограничение валидации, а не подсказка.

    Проверяется главное: ``oneOf`` не подменяется на ``anyOf`` (эксклюзивность —
    часть контракта), поле-дискриминатор становится обязательным, а каждая запись
    ``mapping`` разворачивается в свою пару ``if``/``then``.
    """
    project = fixture_project(tmp_path, "oneof_discriminator.yaml", "getBlock")

    schema = project.build().by_key("api.probe").document["responses"][0]["schema"]

    assert "anyOf" not in schema
    assert schema["oneOf"] == [{"$ref": "#/$defs/TextBlock"}, {"$ref": "#/$defs/ImageBlock"}]
    assert schema["required"] == ["kind"]
    assert schema["allOf"] == [
        {
            "if": {"required": ["kind"], "properties": {"kind": {"const": "image"}}},
            "then": {"$ref": "#/$defs/ImageBlock"},
        },
        {
            "if": {"required": ["kind"], "properties": {"kind": {"const": "text"}}},
            "then": {"$ref": "#/$defs/TextBlock"},
        },
    ]


def test_discriminator_without_mapping_adds_only_required(tmp_path: Path) -> None:
    """Без ``mapping`` соответствие значений вариантам неизвестно — ``if``/``then`` не выдумывается."""
    project = fixture_project(tmp_path, "oneof_no_mapping.yaml", "getBlock")

    schema = project.build().by_key("api.probe").document["responses"][0]["schema"]

    assert schema["required"] == ["kind"]
    assert "allOf" not in schema
    assert schema["oneOf"] == [{"$ref": "#/$defs/TextBlock"}, {"$ref": "#/$defs/ImageBlock"}]


def test_discriminator_mapping_must_point_at_a_declared_variant(tmp_path: Path) -> None:
    """``mapping`` на схему, которой нет среди вариантов, — ошибка, а не игнор."""
    with pytest.raises(UnsupportedConstructError) as info:
        build_source(
            tmp_path,
            """
            openapi: "3.0.3"
            info: {title: T, version: "1.0.0"}
            paths:
              /probe:
                get:
                  operationId: getProbe
                  responses:
                    "200":
                      description: ok
                      content:
                        application/json:
                          schema:
                            oneOf:
                              - $ref: "#/components/schemas/TextBlock"
                            discriminator:
                              propertyName: kind
                              mapping:
                                image: "#/components/schemas/ImageBlock"
            components:
              schemas:
                TextBlock:
                  type: object
                  required: [kind]
                  properties:
                    kind: {type: string}
                ImageBlock:
                  type: object
                  required: [kind]
                  properties:
                    kind: {type: string}
            """,
        )

    assert "которого нет среди вариантов" in str(info.value)


def test_cookie_parameter_is_supported(tmp_path: Path) -> None:
    """Параметр в cookie живёт в собственном пространстве имён со стилем ``form``."""
    project = fixture_project(tmp_path, "cookie_params.yaml", "getSession")

    document = project.build().by_key("api.probe").document

    (parameter,) = document["request"]["parameters"]
    assert parameter["in"] == "cookie"
    assert parameter["name"] == "session_id"
    assert parameter["required"] is True
    assert (parameter["style"], parameter["explode"]) == ("form", True)
    assert parameter["schema"]["minLength"] == 8


def test_media_type_parameters_are_normalised_away(tmp_path: Path) -> None:
    """``application/json;charset=UTF-8`` и вариант с пробелом — это ``application/json``."""
    project = fixture_project(tmp_path, "charset_media_type.yaml", "createLabel")

    document = project.build().by_key("api.probe").document

    assert [body["content_type"] for body in document["request"]["bodies"]] == ["application/json"]
    assert [item["content_type"] for item in document["responses"]] == ["application/json"]


def test_default_only_response_is_supported(tmp_path: Path) -> None:
    """Операция, у которой есть только ``default``, разбирается со статусом ``"default"``."""
    project = fixture_project(tmp_path, "default_response.yaml", "getHealth")

    (response,) = project.build().by_key("api.probe").document["responses"]

    assert response["status"] == "default"
    assert response["schema"]["properties"]["status"]["enum"] == ["ok", "degraded"]


def test_multiple_content_types_are_kept_apart(tmp_path: Path) -> None:
    """У каждого JSON-совместимого content type свой контракт; бинарный помечается непредставимым."""
    project = fixture_project(tmp_path, "multi_content_types.yaml", "replaceDocumentContent")

    built = project.build().by_key("api.probe")

    bodies = {item["content_type"]: item for item in built.document["request"]["bodies"]}
    assert sorted(bodies) == ["application/json", "application/merge-patch+json"]
    assert bodies["application/json"]["schema"]["required"] == ["body"]
    assert "required" not in bodies["application/merge-patch+json"]["schema"]
    assert [item["content_type"] for item in built.document["responses"]] == ["application/json"]
    assert built.unsupported == (
        "response 200:application/pdf: media type 'application/pdf' не является "
        "JSON-совместимым, структурный контракт тела для него не строится",
    )


# --------------------------------------------------- position-awareness


def test_properties_named_like_keywords_survive(tmp_path: Path) -> None:
    """Регрессия на position-awareness — самый важный тест файла.

    Свойства с именами ``type``, ``items``, ``required``, ``properties``,
    ``description``, ``enum``, ``allOf`` обязаны остаться обычными свойствами.
    Обход, который смотрит на имя ключа без учёта позиции, ломает их все разом.
    """
    project = fixture_project(tmp_path, "keyword_named_properties.yaml", "getSettings")

    schema = project.build().by_key("api.probe").document["responses"][0]["schema"]

    assert sorted(schema["properties"]) == [
        "allOf",
        "description",
        "enum",
        "items",
        "properties",
        "required",
        "type",
    ]
    assert schema["required"] == ["enum", "type"]
    assert schema["properties"]["type"] == {"type": "string"}
    assert schema["properties"]["items"] == {"type": "array", "items": {"type": "integer"}}
    assert schema["properties"]["required"] == {"type": "boolean"}
    assert schema["properties"]["properties"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
    assert schema["properties"]["description"] == {"type": "string"}
    assert schema["properties"]["enum"] == {"type": "string", "enum": ["compact", "full"]}
    assert schema["properties"]["allOf"] == {"type": "integer", "minimum": 0}


def test_properties_named_like_rejected_keywords_survive(tmp_path: Path) -> None:
    """Даже имена отклоняемых ключевых слов — законные имена свойств.

    ``not``, ``const``, ``$ref`` в позиции Schema Object останавливают генерацию;
    в позиции имени свойства они не значат ничего особенного.
    """
    built = build_response(
        tmp_path,
        """
        type: object
        required: ["$ref", "not"]
        properties:
          "$ref":
            type: string
          "not":
            type: integer
          "const":
            type: boolean
          "nullable":
            type: string
          "oneOf":
            type: array
            items: {type: string}
          "discriminator":
            type: string
          "additionalProperties":
            type: string
        """,
    )

    properties = response_schema(built)["properties"]
    assert properties["$ref"] == {"type": "string"}
    assert properties["not"] == {"type": "integer"}
    assert properties["const"] == {"type": "boolean"}
    assert properties["nullable"] == {"type": "string"}
    assert properties["oneOf"] == {"type": "array", "items": {"type": "string"}}
    assert properties["discriminator"] == {"type": "string"}
    assert properties["additionalProperties"] == {"type": "string"}


# ------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    ("operation_id", "route", "fragment"),
    [
        ("getNot", "~1not", "ключевое слово 'not' не поддерживается"),
        ("getConst", "~1const", "ключевое слово 'const' не поддерживается"),
        (
            "getPatternProperties",
            "~1pattern-properties",
            "ключевое слово 'patternProperties' не поддерживается",
        ),
        ("getConditional", "~1conditional", "ключевое слово 'if' не поддерживается"),
        ("getBooleanSchema", "~1boolean-schema", "булева схема (true/false)"),
        ("getTypeArray", "~1type-array", "type в виде списка появился только в OpenAPI 3.1"),
    ],
)
def test_unsupported_keywords_fail_closed(
    tmp_path: Path, operation_id: str, route: str, fragment: str
) -> None:
    """Каждая конструкция вне OpenAPI 3.0 отклоняется своей диагностикой и координатами."""
    project = fixture_project(tmp_path, "unsupported_keywords.yaml", operation_id)

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    assert fragment in str(info.value)
    assert_coordinates(
        info.value,
        operation_key="api.probe",
        direction="response",
        pointer_suffix=f"/paths/{route}/get/responses/200/content/application~1json/schema",
    )


def test_schema_without_type_or_constraints_is_rejected(tmp_path: Path) -> None:
    """Схема без ``type`` и без ограничений не превращается молча в «что угодно»."""
    project = fixture_project(tmp_path, "no_type.yaml", "getNote")

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    assert "не подставляет 'любое значение' молча" in str(info.value)
    assert "contract path /body/payload" in str(info.value)
    assert_coordinates(
        info.value,
        operation_key="api.probe",
        direction="response",
        pointer_suffix="/schema/properties/payload",
    )


def test_unknown_format_is_rejected_by_default(tmp_path: Path) -> None:
    """При политике по умолчанию неизвестный ``format`` останавливает генерацию."""
    project = fixture_project(tmp_path, "unknown_format.yaml", "getDocumentTag")

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    assert "format 'my-custom-tag' неизвестен" in str(info.value)
    assert "policies.unknown_formats: annotate" in str(info.value)
    assert_coordinates(
        info.value,
        operation_key="api.probe",
        direction="response",
        pointer_suffix="/schema/properties/tag",
    )


def test_unknown_format_is_annotated_under_relaxed_policy(tmp_path: Path) -> None:
    """``policies.unknown_formats: annotate`` сохраняет ``format`` как аннотацию."""
    project = fixture_project(
        tmp_path,
        "unknown_format.yaml",
        "getDocumentTag",
        policies={"unknown_formats": "annotate"},
    )

    schema = project.build().by_key("api.probe").document["responses"][0]["schema"]

    assert schema["properties"]["tag"] == {"type": "string", "format": "my-custom-tag"}


@pytest.mark.parametrize(
    ("operation_id", "route", "fragment"),
    [
        (
            "searchDocuments",
            "~1documents~1search",
            "описан как ObjectNode; поддержаны только скаляры и массивы скаляров",
        ),
        (
            "searchDocumentsScalar",
            "~1documents~1search-scalar",
            "style=deepObject, explode=True для scalar не поддержано",
        ),
    ],
)
def test_deep_object_parameters_are_rejected(
    tmp_path: Path, operation_id: str, route: str, fragment: str
) -> None:
    """``style: deepObject`` и параметр-объект не сериализуются однозначно."""
    project = fixture_project(tmp_path, "deep_object.yaml", operation_id)

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    assert fragment in str(info.value)
    assert_coordinates(
        info.value,
        operation_key="api.probe",
        direction="request",
        pointer_suffix=f"/paths/{route}/get/parameters/0",
    )


def test_object_valued_query_parameter_is_rejected(tmp_path: Path) -> None:
    """Объект в query отклоняется даже со стилем по умолчанию."""
    with pytest.raises(UnsupportedConstructError) as info:
        build_query_parameter(
            tmp_path,
            """
            type: object
            properties:
              title: {type: string}
            """,
        )

    assert "поддержаны только скаляры и массивы скаляров" in str(info.value)
    assert_coordinates(
        info.value,
        operation_key="main.getProbe",
        direction="request",
        pointer_suffix="/paths/~1probe/get/parameters/0",
    )


def test_array_of_arrays_in_parameter_is_rejected(tmp_path: Path) -> None:
    """Внутри тела массив массивов допустим, а внутри параметра — нет."""
    with pytest.raises(UnsupportedConstructError) as info:
        build_query_parameter(
            tmp_path,
            """
            type: array
            items:
              type: array
              items: {type: string}
            """,
        )

    assert "вложенные массивы и объекты внутри массива" in str(info.value)
    assert_coordinates(
        info.value,
        operation_key="main.getProbe",
        direction="request",
        pointer_suffix="/paths/~1probe/get/parameters/0",
    )


def test_numeric_exclusive_minimum_is_rejected(tmp_path: Path) -> None:
    """Числовой ``exclusiveMinimum`` — это 3.1; читать его правилами 3.0 нельзя."""
    with pytest.raises(UnsupportedConstructError) as info:
        build_response(
            tmp_path,
            """
            type: object
            properties:
              n:
                type: integer
                exclusiveMinimum: 0
            """,
        )

    assert "числовой exclusiveMinimum появился в OpenAPI 3.1" in str(info.value)
    assert_coordinates(
        info.value,
        operation_key="main.getProbe",
        direction="response",
        pointer_suffix="/schema/properties/n",
    )


def test_rejection_message_carries_a_ready_made_waiver(tmp_path: Path) -> None:
    """Сообщение об ошибке содержит готовый черновик waiver с contract path и отпечатком."""
    project = fixture_project(tmp_path, "unsupported_keywords.yaml", "getConst")

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    message = str(info.value)
    assert "operation: api.probe" in message
    assert "direction: response" in message
    assert "json_pointer: /body" in message
    assert "rule: allow_any" in message
    assert "expected_source:" in message


@pytest.mark.parametrize(
    ("keyword", "value"),
    [("minLength", "'3'"), ("minItems", "1.5"), ("maxProperties", "true")],
)
def test_non_integer_bounds_report_their_location(tmp_path: Path, keyword: str, value: str) -> None:
    """Нецелая граница — ошибка с координатами, а не безымянное исключение.

    Регрессия: раньше такая ошибка приходила вообще без источника, операции,
    направления и указателя, и найти её в большой спецификации было нечем.
    """
    container = (
        "array" if keyword == "minItems" else ("object" if keyword == "maxProperties" else "string")
    )
    # Отступ обязан совпасть с соседними строками f-строки ниже (16 пробелов):
    # блок проходит через dedent, и «почти правильный» отступ ломает сам YAML.
    extra = "\n" + " " * 16 + "items: {type: string}" if container == "array" else ""
    with pytest.raises(UnsupportedConstructError) as info:
        build_response(
            tmp_path,
            f"""
            type: object
            properties:
              a:
                type: {container}
                {keyword}: {value}{extra}
            """,
        )

    assert f"{keyword}: ожидалось целое число" in str(info.value)
    assert_coordinates(
        info.value,
        operation_key="main.getProbe",
        direction="response",
        pointer_suffix="/schema/properties/a",
    )


def test_semantic_sibling_of_ref_is_rejected(tmp_path: Path) -> None:
    """Ключ рядом с ``$ref``, который OpenAPI 3.0 игнорирует, останавливает генерацию."""
    project = fixture_project(tmp_path, "ref_siblings.yaml", "getIllegalCard")

    with pytest.raises(RefResolutionError) as info:
        project.build()

    assert "minLength" in str(info.value)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Дефект normalization/schemas.py: у объекта без 'properties' список 'required' "
        "молча теряется вместо fail closed"
    ),
)
def test_required_without_properties_is_not_silently_dropped(tmp_path: Path) -> None:
    """``required`` без ``properties`` — знаемое ограничение, терять его нельзя.

    Проверка `dangling` в ``_normalize_object`` выключена, когда ``properties``
    пуст, поэтому ``{"type": "object", "required": ["ghost"]}`` превращается в
    объект вообще без ограничений. Это молчаливая потеря контракта: fail closed
    требует либо сохранить требование, либо отказаться его разбирать.
    """
    built = build_response(
        tmp_path,
        """
        type: object
        required: [ghost]
        """,
    )

    assert response_schema(built).get("required") == ["ghost"]


# ---------------------------------------------------------- направления


def test_read_only_and_write_only_do_not_leak_between_directions(tmp_path: Path) -> None:
    """Общая схема даёт два разных контракта, и оба верны в одном прогоне.

    ``Item`` используется и как тело запроса, и как тело ответа. Если бы
    нормализация вырезала поля из общего документа, второе направление получило
    бы уже испорченную схему — поэтому обе половины проверяются вместе.
    """
    text = """
    openapi: "3.0.3"
    info: {title: Direction fixture, version: "1.0.0"}
    paths:
      /items:
        post:
          operationId: upsertItem
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  $ref: "#/components/schemas/Item"
          responses:
            "200":
              description: ok
              content:
                application/json:
                  schema:
                    $ref: "#/components/schemas/Item"
    components:
      schemas:
        Item:
          type: object
          required: [name, createdAt, password]
          properties:
            name:
              type: string
            createdAt:
              type: string
              format: date-time
              readOnly: true
            password:
              type: string
              minLength: 8
              writeOnly: true
    """
    built = build_source(tmp_path, textwrap.dedent(text)).by_key("main.upsertItem")

    request = built.document["request"]["bodies"][0]["schema"]["$defs"]["Item"]
    response = built.document["responses"][0]["schema"]["$defs"]["Item"]

    # readOnly вырезано из запроса, writeOnly — из ответа.
    assert sorted(request["properties"]) == ["name", "password"]
    assert sorted(response["properties"]) == ["createdAt", "name"]
    # required подрезан вместе со свойствами, а не оставлен висеть.
    assert request["required"] == ["name", "password"]
    assert response["required"] == ["createdAt", "name"]
    # Определения направлений изолированы: это два разных бандла с одним именем.
    assert dict(built.request_definitions)["Item"] != dict(built.response_definitions)["Item"]


def test_direction_pruning_is_stable_across_builds(tmp_path: Path) -> None:
    """Повторная сборка того же источника даёт тот же документ — исходник не мутирует."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("basic", "openapi30.yaml"))
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})

    first = project.build().by_key("main.createDocument").document
    second = project.build().by_key("main.createDocument").document

    assert first == second
    request = first["request"]["bodies"][0]["schema"]["$defs"]["DocumentCreate"]
    response = first["responses"][0]["schema"]["$defs"]["Document"]
    assert "createdAt" not in request["properties"]
    assert "draftSecret" in request["properties"]
    assert "createdAt" in response["properties"]


def test_write_only_is_ignored_by_swagger2(tmp_path: Path) -> None:
    """В Swagger 2.0 нет ``writeOnly``, поэтому диалект его не применяет."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.json", spec("basic", "swagger2.json"))
    project.write_manifest(sources={"main": {"path": "api/spec.json", "selection": "all"}})

    document = project.build().by_key("main.createDocument").document

    request = document["request"]["bodies"][0]["schema"]["$defs"]["DocumentCreate"]
    # readOnly работает в обоих диалектах...
    assert "createdAt" not in request["properties"]
    # ...а draftSecret остаётся: в Swagger 2.0 пометить его writeOnly нечем.
    assert "draftSecret" in request["properties"]


# ------------------------------------------------------------- рекурсия


@pytest.mark.parametrize(
    ("key", "definitions"),
    [("main.getTree", ["Node"]), ("main.getCycle", ["Alpha", "Beta", "Gamma"])],
)
def test_recursive_schemas_build_but_disable_d42(
    tmp_path: Path, key: str, definitions: list[str]
) -> None:
    """Рекурсия выразима в JSON Schema через ``$defs``, но не выразима в d42."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("features", "recursive.yaml"))
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})

    built = project.build().by_key(key)

    assert built.recursive is True
    assert sorted(name for name, _ in built.response_definitions) == definitions
    schema = built.document["responses"][0]["schema"]
    assert sorted(schema["$defs"]) == definitions
    assert built.document["d42"] == {
        "enabled": False,
        "recursive": True,
        "request_module": None,
        "response_module": None,
    }


def test_no_d42_module_is_rendered_for_recursive_operations(tmp_path: Path) -> None:
    """Для рекурсивных операций d42-модули не рендерятся вовсе."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("features", "recursive.yaml"))
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})

    artifacts = project.render()

    paths = [item.path for item in artifacts.files]
    assert not [path for path in paths if path.startswith("_d42/")]
    assert "contracts/main__get_tree.json" in paths


def test_self_referential_schema_keeps_the_cycle_in_defs(tmp_path: Path) -> None:
    """Самоссылка сохраняется как ссылка на определение, а не разворачивается вглубь."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("features", "recursive.yaml"))
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})

    schema = project.build().by_key("main.getTree").document["responses"][0]["schema"]

    assert schema["$ref"] == "#/$defs/Node"
    assert schema["$defs"]["Node"]["properties"]["children"]["items"] == {"$ref": "#/$defs/Node"}

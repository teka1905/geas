"""Текстовые тела и ответы (``text/*``) и тела, которые контракт не проверяет.

``text/plain`` и другие ``text/*`` — это строка целиком, а не JSON-литерал. Для них
строится контракт строки (``type: string``), тело запроса декодируется по
``charset`` из ``Content-Type``, а мок отдаёт строку как есть. Media type, для
которого контракта нет (``multipart/*``, ``application/octet-stream``), больше не
выдаётся за отсутствие тела: он назван в документе, в CLI и в ошибках.

Три слоя, как и везде: сборка контракта, runtime без сети, живой обмен с JJ.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import yaml

from geas.errors import (
    ContractMockError,
    RequestContractError,
    ResponseContractError,
    ResponseVariantError,
    UnsupportedConstructError,
)
from geas.media import (
    MediaKind,
    declared_charset,
    is_text_media_type,
    media_kind,
    media_priority,
    media_range_specificity,
    unchecked_reason,
)
from geas.naming import slugify_key
from jj_server import JJServer, jj_server  # noqa: F401 - сессионная фикстура
from support import Project, make_project, spec

#: Операции ``text_bodies.yaml`` общего проекта модуля. Остальные (закреплённые
#: варианты, waiver'ы, коллизии имён) собираются в отдельных проектах своих тестов.
GOOD_OPERATIONS = (
    "textPreview",
    "compilePattern",
    "renderFragment",
    "jsonOrText",
    "uploadAttachment",
    "mixedBodies",
    "anyContent",
    "swashbuckleDefaults",
    "xmlAndJson",
    "textDefault",
    "anyRequest",
    "imageUpload",
    "stringCompositions",
    "downloadWithDefault",
    "jsonAndWildcard",
    "events",
    "swashbuckleRequest",
    "problemAndWildcard",
    "objectAsText",
    "wildcardDiffers",
)


def text_project(root: Path, operation_ids: tuple[str, ...], *, package: str) -> Project:
    """Проект с выбранными операциями ``text_bodies.yaml``."""
    project = make_project(root, package=package)
    project.write_spec("api/spec.yaml", spec("features", "text_bodies.yaml"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={
            f"api.{operation_id}": {
                "source": "main",
                "operation_id": operation_id,
                "python_path": ["api", _snake(operation_id)],
            }
            for operation_id in operation_ids
        },
    )
    return project


def _snake(name: str) -> str:
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in name)


@pytest.fixture(scope="module")
def operations(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Generated namespace проекта с текстовыми телами."""
    project = text_project(
        tmp_path_factory.mktemp("text"), GOOD_OPERATIONS, package="text_contracts"
    )
    project.update()
    with project.importable() as module:
        yield module.operations


def build_one(tmp_path: Path, operation_id: str) -> Project:
    project = text_project(tmp_path / "project", (operation_id,), package="single_contracts")
    return project


# ------------------------------------------------------------ media type


@pytest.mark.parametrize(
    "value, kind",
    [
        ("application/json", MediaKind.JSON),
        ("application/json;charset=UTF-8", MediaKind.JSON),
        ("application/problem+json", MediaKind.JSON),
        ("*/*", MediaKind.JSON),
        ("text/plain", MediaKind.TEXT),
        ("TEXT/HTML; charset=utf-8", MediaKind.TEXT),
        ("text/csv", MediaKind.TEXT),
        ("text/json", MediaKind.JSON),
        ("text/xml", MediaKind.OTHER),
        ("text/yaml", MediaKind.OTHER),
        ("application/vnd.example+yaml", MediaKind.OTHER),
        ("text/event-stream", MediaKind.OTHER),
        ("application/atom+xml", MediaKind.OTHER),
        ("text/vnd.example+xml", MediaKind.OTHER),
        ("text/*", MediaKind.OTHER),
        ("application/*", MediaKind.OTHER),
        ("application/xml", MediaKind.OTHER),
        ("application/octet-stream", MediaKind.OTHER),
        ("multipart/form-data", MediaKind.OTHER),
        ("image/png", MediaKind.OTHER),
    ],
)
def test_media_kind(value: str, kind: MediaKind) -> None:
    """Контракт строится для JSON и ``text/*``; всё прочее объявлено без проверки."""
    assert media_kind(value) is kind
    assert (unchecked_reason(value) is None) == (kind is not MediaKind.OTHER)


def test_text_wildcard_is_not_a_text_type() -> None:
    """``text/*`` не называет формат, и у мока не было бы честного ``Content-Type``."""
    assert is_text_media_type("text/plain")
    assert not is_text_media_type("text/*")
    assert unchecked_reason("text/*") is not None


@pytest.mark.parametrize(
    "declared, actual, score",
    [
        ("application/json", "application/json", 2),
        ("application/json", "Application/JSON; charset=utf-8", 2),
        ("image/*", "image/png", 1),
        ("*/*", "application/json", 0),
        ("*/*", "image/png", 0),
        ("image/*", "text/plain", None),
        ("application/json", "application/problem+json", None),
    ],
)
def test_media_range_specificity(declared: str, actual: str, score: int | None) -> None:
    """Спецификация объявляет диапазон, клиент шлёт конкретный тип."""
    assert media_range_specificity(declared, actual) == score


def test_media_priority_puts_aliases_after_concrete_json() -> None:
    """``*/*`` и ``text/json`` второстепенны: базовое имя и выбор достаются конкретному типу."""
    ordered = sorted(
        ["text/plain", "*/*", "application/vnd.foo+json", "text/json", "application/json"],
        key=media_priority,
    )
    assert ordered == [
        "application/json",
        "application/vnd.foo+json",
        "*/*",
        "text/json",
        "text/plain",
    ]


@pytest.mark.parametrize(
    "value",
    ["text/yaml", "text/xml", "text/event-stream", "image/*", "application/pdf"],
)
def test_unchecked_reason_keeps_the_wording_of_previous_versions(value: str) -> None:
    """Причина входит в ``unsupported`` и в семантику: её смена дала бы ложный ``geas diff``."""
    assert unchecked_reason(value) == (
        f"media type {value!r} не является JSON-совместимым, "
        f"структурный контракт тела для него не строится"
    )


@pytest.mark.parametrize(
    "header, charset",
    [
        ("text/plain", None),
        ("text/plain; charset=utf-8", "utf-8"),
        ('text/plain; charset="Windows-1251"', "windows-1251"),
        ("text/plain;CHARSET=KOI8-R", "koi8-r"),
    ],
)
def test_declared_charset(header: str, charset: str | None) -> None:
    assert declared_charset(header) == charset


# ----------------------------------------------------------- сборка контракта


def test_text_plain_with_string_schema_becomes_a_string_contract(operations: Any) -> None:
    """Ограничения строки остаются в JSON Schema тела запроса и ответа."""
    handle = operations.api.text_preview

    request = handle.request.body()
    response = handle.response(status=200)

    assert request.content_type == "text/plain"
    assert request.required is True
    assert request.json_schema["type"] == "string"
    assert (request.json_schema["minLength"], request.json_schema["maxLength"]) == (1, 40)
    assert response.content_type == "text/plain"
    assert response.json_schema["maxLength"] == 200
    assert handle.unchecked == ()
    assert handle.unsupported == ()


def test_text_variant_with_charset_in_spec_is_normalized(operations: Any) -> None:
    """``text/html;charset=UTF-8`` в спецификации — вариант ``text/html``."""
    variant = operations.api.render_fragment.response(status=200)

    assert variant.content_type == "text/html"
    assert variant.json_schema["minLength"] == 1


def test_text_body_by_ref_to_a_named_string(operations: Any) -> None:
    """Ссылка на именованную строку разворачивается как у любого тела."""
    variant = operations.api.compile_pattern.response(status=200)

    assert variant.json_schema["$ref"] == "#/$defs/CompiledPattern"
    assert variant.json_schema["$defs"]["CompiledPattern"] == {
        "pattern": "^[^<]*$",
        "type": "string",
    }


def test_json_and_text_variants_of_one_status_have_separate_d42_schemas(
    operations: Any,
) -> None:
    """Общее имя затёрло бы JSON-вариант строкой: мок проверялся бы чужой схемой."""
    pytest.importorskip("d42")
    handle = operations.api.json_or_text
    as_json = handle.response(status=200, content_type="application/json")
    as_text = handle.response(status=200, content_type="text/plain")

    assert as_json.d42_export != as_text.d42_export
    # JSON-вариант сохраняет прежнее имя: появление text/plain рядом его не переименовывает.
    assert as_json.d42_export == "GeneratedApiJsonOrTextResponse200Schema"
    assert as_text.d42_export == "GeneratedApiJsonOrTextResponse200TextPlainSchema"

    from d42 import validate_or_fail

    validate_or_fail(handle.d42_schema(_response(), export=as_text.d42_export), "строка")
    validate_or_fail(handle.d42_schema(_response(), export=as_json.d42_export), {"text": "строка"})
    with pytest.raises(Exception):  # noqa: B017 - класс ошибки d42 не публичен
        validate_or_fail(handle.d42_schema(_response(), export=as_json.d42_export), "строка")


def _response() -> Any:
    from geas.models import Direction

    return Direction.RESPONSE


def test_request_bodies_with_different_schemas_get_separate_d42_names(tmp_path: Path) -> None:
    """До исправления ``merge-patch+json`` затирал d42-схему ``application/json``."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("features", "multi_content_types.yaml"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={"api.probe": {"source": "main", "operation_id": "replaceDocumentContent"}},
    )
    project.update()

    bodies = {
        item["content_type"]: item["d42"]
        for item in project.contract_document("api__probe")["request"]["bodies"]
    }
    assert bodies == {
        "application/json": "GeneratedApiProbeRequestSchema",
        "application/merge-patch+json": "GeneratedApiProbeRequestApplicationMergePatchJsonSchema",
    }
    module = (project.output_dir / "_d42" / "api__probe_request.py").read_text(encoding="utf-8")
    assert 'GeneratedApiProbeRequestSchema = schema.dict({"body": schema.str' in module
    assert "GeneratedApiProbeRequestApplicationMergePatchJsonSchema = schema.dict({" in module


def test_variants_with_equal_schemas_share_one_d42_name(tmp_path: Path) -> None:
    """Одна схема под двумя JSON-типами (``produces`` Swagger 2.0) — одно имя, без суффикса."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.json", spec("features", "text_bodies_swagger2.json"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.json", "selection": "explicit"}},
        operations={"api.two": {"source": "main", "operation_id": "twoJsonTypes"}},
    )

    document = project.build().by_key("api.two").document

    assert [(item["content_type"], item["d42"]) for item in document["responses"]] == [
        ("application/json", "GeneratedApiTwoResponse200Schema"),
        ("application/problem+json", "GeneratedApiTwoResponse200Schema"),
    ]


def pinned_project(tmp_path: Path, operation_id: str, entry: dict[str, Any]) -> Project:
    """Проект с одной операцией ``text_bodies.yaml`` и закреплёнными вариантами."""
    project = text_project(tmp_path / "project", (operation_id,), package="pinned_contracts")
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={
            f"api.{operation_id}": {"source": "main", "operation_id": operation_id, **entry}
        },
    )
    return project


def test_pinned_object_schema_under_text_plain_is_rejected_with_a_reason(tmp_path: Path) -> None:
    """Закреплённый ``text/plain`` со схемой не ``string`` — явная ошибка генерации."""
    project = pinned_project(tmp_path, "objectAsText", {"request": {"content_type": "text/plain"}})

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    error = info.value
    assert "тело запроса text/plain — строка" in error.base_message
    assert "схема описывает object" in error.base_message
    assert "request.content_type" in error.base_message
    assert error.operation_key == "api.objectAsText"
    assert error.direction == "request"
    assert error.json_pointer is not None
    assert error.json_pointer.endswith("/requestBody/content/text~1plain/schema")


def test_pinned_nullable_string_under_text_plain_is_rejected(tmp_path: Path) -> None:
    """У текстового тела нет ``null``: nullable-строка не годится."""
    project = pinned_project(
        tmp_path, "nullableText", {"responses": [{"status": 200, "content_type": "text/plain"}]}
    )

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    assert "ответ 200 text/plain — строка" in info.value.base_message
    assert "допускает null" in info.value.base_message
    assert info.value.direction == "response"


@pytest.mark.parametrize(
    "operation_id, direction, content_type, problem",
    [
        ("objectAsText", "request", "text/plain", "схема описывает object"),
        ("nullableText", "response", "text/plain", "допускает null"),
        ("csvReport", "response", "text/csv", "схема описывает array"),
    ],
)
def test_unpinned_non_string_text_variant_is_unchecked_with_a_reason(
    tmp_path: Path, operation_id: str, direction: str, content_type: str, problem: str
) -> None:
    """Незакреплённый вариант генерацию не валит: он назван в ``unchecked`` с причиной.

    На ``main`` такие спецификации (springdoc для ``text/csv`` со списком, Swashbuckle)
    генерировались, и падение всего источника было бы регрессией.
    """
    project = build_one(tmp_path, operation_id)

    built = project.build().by_key(f"api.{operation_id}")

    (unchecked,) = built.unchecked
    assert (unchecked.direction.value, unchecked.content_type) == (direction, content_type)
    assert problem in unchecked.reason
    assert "закреплённый в manifest вариант с такой схемой — ошибка генерации" in unchecked.reason


def test_swagger2_text_plain_body_and_response(tmp_path: Path) -> None:
    """``consumes``/``produces`` с ``text/plain`` дают контракт строки и в Swagger 2.0."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.json", spec("features", "text_bodies_swagger2.json"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.json", "selection": "explicit"}},
        operations={"api.textPreview": {"source": "main", "operation_id": "textPreview"}},
    )

    document = project.build().by_key("api.textPreview").document

    (body,) = document["request"]["bodies"]
    (response,) = document["responses"]
    assert (body["content_type"], body["schema"]["type"]) == ("text/plain", "string")
    assert (response["content_type"], response["schema"]["maxLength"]) == ("text/plain", 200)
    assert "unchecked" not in document


def swagger2_project(tmp_path: Path, operations: dict[str, Any]) -> Project:
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.json", spec("features", "text_bodies_swagger2.json"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.json", "selection": "explicit"}},
        operations=operations,
    )
    return project


def test_swagger2_text_in_produces_next_to_the_same_object_schema_is_unchecked(
    tmp_path: Path,
) -> None:
    """Лишний ``text/plain`` из общего ``produces`` при схеме объекта — артефакт генератора.

    Такая спецификация валидна, и падение генерации было бы регрессией: вариант уходит
    в ``unchecked`` с причиной, JSON-вариант строится как обычно.
    """
    project = swagger2_project(
        tmp_path, {"api.both": {"source": "main", "operation_id": "jsonAndText"}}
    )

    built = project.build().by_key("api.both")

    assert [item["content_type"] for item in built.document["responses"]] == ["application/json"]
    (unchecked,) = built.unchecked
    assert (unchecked.status, unchecked.content_type) == (200, "text/plain")
    assert "схема описывает object, как и у application/json" in unchecked.reason
    assert "артефакт генератора" in unchecked.reason


def test_pinned_text_variant_with_an_object_schema_is_rejected(tmp_path: Path) -> None:
    """Закреплённый в manifest вариант просили явно — для него исключения нет."""
    project = swagger2_project(
        tmp_path,
        {
            "api.both": {
                "source": "main",
                "operation_id": "jsonAndText",
                "responses": [{"status": 200, "content_type": "text/plain"}],
            }
        },
    )

    with pytest.raises(UnsupportedConstructError, match="ответ 200 text/plain — строка"):
        project.build()


def test_pinning_the_json_variant_skips_the_text_one(tmp_path: Path) -> None:
    """Вариант, исключённый из выбора manifest, не проверяется и не попадает в ``unchecked``."""
    project = swagger2_project(
        tmp_path,
        {
            "api.both": {
                "source": "main",
                "operation_id": "jsonAndText",
                "responses": [{"status": 200, "content_type": "application/json"}],
            }
        },
    )

    built = project.build().by_key("api.both")

    assert [item["content_type"] for item in built.document["responses"]] == ["application/json"]
    assert built.unchecked == ()


def test_swashbuckle_defaults_keep_json_and_drop_text_plain(operations: Any) -> None:
    """``text/plain`` + ``application/json`` + ``text/json`` с одним ``$ref`` на объект."""
    handle = operations.api.swashbuckle_defaults

    assert [variant.content_type for variant in handle.responses] == [
        "application/json",
        "text/json",
    ]
    (unchecked,) = handle.unchecked
    assert unchecked.content_type == "text/plain"
    assert "как и у application/json, text/json" in unchecked.reason
    handle.validate_response({"login": "robot"}, status=200, content_type="text/json")


@pytest.mark.parametrize("key", ["api.swashbuckleDefaults", "api.jsonAndWildcard"])
def test_variants_with_one_schema_need_no_content_type(operations: Any, key: str) -> None:
    """``application/json`` рядом с ``text/json`` или ``*/*`` той же схемы — один вариант.

    До поддержки ``text/json`` и ``*/*`` в OpenAPI 3 вариант был один, и
    ``response(status=200)`` не должен начать падать с неоднозначностью.
    """
    handle = operations.by_key(key)

    assert handle.response(status=200).content_type == "application/json"
    assert handle.validate_response({"login": "robot"}, status=200).content_type == (
        "application/json"
    )
    assert handle.mock(response={"login": "robot"}, status=200).variant.content_type == (
        "application/json"
    )
    assert "application/json" in handle.describe_response(status=200)


def test_variants_with_different_schemas_still_need_content_type(operations: Any) -> None:
    """JSON-объект и строка — разные варианты: молча брать первый нельзя."""
    with pytest.raises(ResponseVariantError, match="не выбран однозначно"):
        operations.api.json_or_text.response(status=200)


def test_swagger2_text_produces_with_an_error_object_is_unchecked(tmp_path: Path) -> None:
    """``produces: [text/plain]`` размножается и на ``400`` с объектом ошибки — это не ошибка."""
    project = swagger2_project(
        tmp_path, {"api.text": {"source": "main", "operation_id": "textWithErrors"}}
    )

    built = project.build().by_key("api.text")

    assert [(item["status"], item["content_type"]) for item in built.document["responses"]] == [
        (200, "text/plain")
    ]
    (unchecked,) = built.unchecked
    assert (unchecked.status, unchecked.content_type) == (400, "text/plain")
    assert "общего consumes/produces Swagger 2.0" in unchecked.reason


def test_swagger2_pinned_text_error_response_is_rejected(tmp_path: Path) -> None:
    project = swagger2_project(
        tmp_path,
        {
            "api.text": {
                "source": "main",
                "operation_id": "textWithErrors",
                "responses": [{"status": 400, "content_type": "text/plain"}],
            }
        },
    )

    with pytest.raises(UnsupportedConstructError, match="ответ 400 text/plain — строка"):
        project.build()


def test_event_stream_is_unchecked(operations: Any) -> None:
    """SSE — поток событий: схема массива под ``text/event-stream`` генерацию не ломает."""
    handle = operations.api.events

    assert handle.responses == ()
    (unchecked,) = handle.unchecked
    assert unchecked.content_type == "text/event-stream"


def test_wildcard_variant_gets_its_own_d42_name(tmp_path: Path) -> None:
    """У ``*/*`` нет букв для суффикса: имя ``...Any``, а не ложная коллизия с базовым."""
    project = build_one(tmp_path, "wildcardDiffers")

    document = project.build().by_key("api.wildcardDiffers").document

    assert [(item["content_type"], item["d42"]) for item in document["request"]["bodies"]] == [
        ("*/*", "GeneratedApiWildcardDiffersRequestAnySchema"),
        ("application/json", "GeneratedApiWildcardDiffersRequestSchema"),
    ]


def test_suffix_collision_is_reported_only_with_d42(tmp_path: Path) -> None:
    from geas.errors import NamespaceCollisionError

    project = build_one(tmp_path, "suffixCollision")
    with pytest.raises(NamespaceCollisionError) as info:
        project.build()
    assert "'application/vnd-a+json' и 'application/vnd.a+json'" in str(info.value)

    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={
            "api.suffixCollision": {
                "source": "main",
                "operation_id": "suffixCollision",
                "d42": False,
            }
        },
    )
    assert len(project.build().by_key("api.suffixCollision").document["request"]["bodies"]) == 3


def test_text_xml_is_unchecked_like_application_xml(operations: Any) -> None:
    """``text/xml`` — это XML (RFC 7303), а не строка: схему объекта он не ломает."""
    handle = operations.api.xml_and_json

    assert [variant.content_type for variant in handle.responses] == ["application/json"]
    assert [item.content_type for item in handle.unchecked] == ["text/xml"]


def test_string_compositions_are_strings(operations: Any) -> None:
    """``allOf`` и ``oneOf`` из одних строк под ``text/plain`` — тоже строка."""
    handle = operations.api.string_compositions

    handle.validate_response("ab", status=200)
    with pytest.raises(ResponseContractError):
        handle.validate_response("abcd", status=200)
    with pytest.raises(ResponseContractError):
        handle.validate_response("<b", status=200)
    handle.validate_response("draft", status=201)
    with pytest.raises(ResponseContractError):
        handle.validate_response("maybe", status=201)


def test_text_body_under_allow_any_waiver_is_any_string(tmp_path: Path) -> None:
    """Схема без ограничений принимается по waiver ``allow_any`` и значит «любая строка»."""
    project = build_one(tmp_path, "anyText")
    # Без waiver'а незакреплённый вариант уходит в unchecked, а причина несёт заготовку.
    (unchecked,) = project.build().by_key("api.anyText").unchecked
    assert "allow_any" in unchecked.reason
    template = unchecked.reason
    expected_source = re.search(r"expected_source: (\w+)", template)
    assert expected_source is not None
    project.write_waivers(
        [
            {
                "operation": "api.anyText",
                "direction": "response",
                "json_pointer": "/body",
                "rule": "allow_any",
                "expected_source": expected_source.group(1),
                "reason": "тело — произвольный текст",
                "owner": "qa",
                "issue": "QAAUTOSAM-692",
                "expires_at": "2099-01-01",
            }
        ]
    )
    project.update()

    with project.importable() as module:
        handle = module.operations.api.any_text
        assert handle.response(status=200).json_schema["type"] == "string"
        handle.validate_response("что угодно", status=200)
        with pytest.raises(ResponseContractError):
            handle.validate_response({"login": "robot"}, status=200)
        with pytest.raises(ResponseContractError):
            handle.mock(response=42, status=200)
        handle.mock(response="текст", status=200)


@pytest.mark.parametrize(
    "dialect, fixture",
    [("openapi30", "text_bodies.yaml"), ("swagger2", "text_bodies_swagger2.json")],
)
def test_wildcard_media_type_is_json_in_both_dialects(
    tmp_path: Path, dialect: str, fixture: str
) -> None:
    """``*/*`` — JSON и в OpenAPI 3 (раньше только в Swagger 2.0)."""
    project = make_project(tmp_path / "project")
    project.write_spec(f"api/{fixture}", spec("features", fixture))
    source = {"path": f"api/{fixture}", "selection": "explicit"}
    project.write_manifest(
        sources={"main": source},
        operations={"api.anyContent": {"source": "main", "operation_id": "anyContent"}},
    )

    document = project.build().by_key("api.anyContent").document

    (response,) = document["responses"]
    assert response["content_type"] == "*/*"
    assert response["schema"]["required"] == ["login"]
    assert "unchecked" not in document


def test_unchecked_variants_are_structured_in_the_contract(operations: Any) -> None:
    """Объявленное, но не проверяемое тело названо в документе и в ручке."""
    handle = operations.api.upload_attachment

    assert handle.request.bodies == ()
    assert handle.responses == ()
    assert [(item.direction.value, item.content_type) for item in handle.unchecked] == [
        ("request", "multipart/form-data"),
        ("response", "application/octet-stream"),
    ]
    assert handle.unchecked[0].required is True
    assert handle.unchecked[1].status == 200
    assert all("контракт тела для него не строится" in item.reason for item in handle.unchecked)
    assert [item.label() for item in handle.unchecked] == [
        "request multipart/form-data",
        "response 200:application/octet-stream",
    ]
    assert handle.unsupported == tuple(item.describe() for item in handle.unchecked)


# ----------------------------------------------------------- runtime без сети


def test_validate_response_checks_a_string_against_the_contract(operations: Any) -> None:
    handle = operations.api.text_preview

    variant = handle.validate_response("bold", status=200)

    assert variant.content_type == "text/plain"
    with pytest.raises(ResponseContractError) as info:
        handle.validate_response("x" * 201, status=200)
    assert info.value.validator == "maxLength"


def test_validate_response_checks_the_pattern_of_a_text_ref(operations: Any) -> None:
    handle = operations.api.compile_pattern

    handle.validate_response("Привет, {name}", status=200)
    with pytest.raises(ResponseContractError) as info:
        handle.validate_response("<b>bold</b>", status=200)
    assert info.value.validator == "pattern"


@pytest.mark.parametrize("body", [{"text": "строка"}, ["a"], 7, None])
def test_validate_response_rejects_a_non_string_text_body(operations: Any, body: Any) -> None:
    with pytest.raises((ResponseContractError, ResponseVariantError)):
        operations.api.text_preview.validate_response(body, status=200)


def test_validate_request_body_checks_the_string(operations: Any) -> None:
    handle = operations.api.text_preview

    handle.validate_request_body("привет")
    with pytest.raises(RequestContractError):
        handle.validate_request_body("")
    with pytest.raises(RequestContractError):
        handle.validate_request_body("x" * 41)


def test_describe_text_body_and_response(operations: Any) -> None:
    handle = operations.api.text_preview

    request = handle.describe_request_body()
    response = handle.describe_response(status=200)

    assert "text/plain" in request
    assert "string" in request
    assert "text/plain" in response
    assert "тело отсутствует" not in response


def recorded(
    operations: Any,
    key: str,
    *,
    body: Any = None,
    raw_body: bytes | None = None,
    content_type: str | None = "text/plain",
    path: str | None = None,
    method: str = "POST",
) -> None:
    """Проверить перехваченный запрос; ``body``/``raw_body`` — как их отдаёт JJ."""
    handle = operations.by_key(key)
    headers = [] if content_type is None else [("Content-Type", content_type)]
    handle.validate_recorded_request(
        method=method,
        path=path or handle.path,
        segments={},
        params=[],
        headers=headers,
        body=body,
        raw_body=raw_body,
        index=0,
    )


def test_recorded_text_request_is_checked_as_a_string(operations: Any) -> None:
    recorded(operations, "api.textPreview", body="привет", raw_body="привет".encode())


def test_text_request_body_is_not_parsed_as_json(operations: Any) -> None:
    """Строка ``"x"`` в кавычках — это шесть символов текста, а не JSON-литерал."""
    recorded(operations, "api.textPreview", body='{"a": 1}', raw_body=b'{"a": 1}')
    recorded(operations, "api.textPreview", body='"quoted"', raw_body=b'"quoted"')


def test_text_request_body_is_decoded_by_the_charset_of_the_header(operations: Any) -> None:
    """JJ декодирует всегда как UTF-8, а проверять надо в кодировке из заголовка."""
    text = "привет"
    encoded = text.encode("windows-1251")

    recorded(
        operations,
        "api.textPreview",
        body=encoded,
        raw_body=encoded,
        content_type="text/plain; charset=windows-1251",
    )
    with pytest.raises(RequestContractError) as info:
        recorded(
            operations,
            "api.textPreview",
            body=("я" * 41).encode("windows-1251"),
            raw_body=("я" * 41).encode("windows-1251"),
            content_type="text/plain; charset=windows-1251",
        )
    # Сорок один символ, а не сорок один байт в UTF-8 (82): длина считается по тексту.
    assert info.value.validator == "maxLength"


def test_text_request_body_without_charset_is_utf8(operations: Any) -> None:
    broken = "привет".encode("windows-1251")

    with pytest.raises(RequestContractError) as info:
        recorded(operations, "api.textPreview", body=broken, raw_body=broken)

    assert "не декодируется как utf-8" in str(info.value)
    assert info.value.json_pointer == "/body"


def test_text_request_body_with_unknown_charset(operations: Any) -> None:
    with pytest.raises(RequestContractError) as info:
        recorded(
            operations,
            "api.textPreview",
            body=b"abc",
            raw_body=b"abc",
            content_type="text/plain; charset=klingon",
        )

    assert "неизвестный charset 'klingon'" in str(info.value)
    assert info.value.json_pointer == "/header/Content-Type"


def test_text_request_body_violating_the_schema(operations: Any) -> None:
    with pytest.raises(RequestContractError) as info:
        recorded(operations, "api.textPreview", body="x" * 41, raw_body=b"x" * 41)

    assert info.value.validator == "maxLength"


def test_text_request_with_foreign_content_type_is_rejected(operations: Any) -> None:
    with pytest.raises(RequestContractError) as info:
        recorded(operations, "api.textPreview", body='{"a": 1}', content_type="application/json")

    assert info.value.json_pointer == "/header/Content-Type"
    assert "text/plain" in str(info.value)


def test_required_text_request_without_body_is_rejected(operations: Any) -> None:
    with pytest.raises(RequestContractError, match="обязательно"):
        recorded(operations, "api.textPreview", body=None, content_type=None)


# ------------------------------------------- объявлено, но не проверяется


def test_unchecked_request_body_is_accepted_but_not_checked(operations: Any) -> None:
    """``multipart/form-data`` объявлен спецификацией: запрос не дефект, но и не проверен."""
    recorded(
        operations,
        "api.uploadAttachment",
        body=b"--b\r\n...",
        raw_body=b"--b\r\n...",
        content_type="multipart/form-data; boundary=b",
    )


def test_unchecked_only_operation_still_rejects_an_undeclared_content_type(
    operations: Any,
) -> None:
    with pytest.raises(RequestContractError) as info:
        recorded(
            operations,
            "api.uploadAttachment",
            body="{}",
            raw_body=b"{}",
            content_type="application/json",
        )

    assert "не описан контрактом" in str(info.value)
    assert "multipart/form-data" in str(info.value)


def test_required_unchecked_body_must_be_present(operations: Any) -> None:
    with pytest.raises(RequestContractError, match="обязательно"):
        recorded(operations, "api.uploadAttachment", body=None, content_type=None)


def test_mixed_operation_validates_json_and_lets_multipart_through(operations: Any) -> None:
    recorded(operations, "api.mixedBodies", body={"name": "a"}, content_type="application/json")
    recorded(
        operations,
        "api.mixedBodies",
        body=b"--b",
        raw_body=b"--b",
        content_type="multipart/form-data; boundary=b",
    )
    with pytest.raises(RequestContractError):
        recorded(operations, "api.mixedBodies", body={}, content_type="application/json")


def test_request_body_lookup_names_the_unchecked_body(operations: Any) -> None:
    """Вместо «у операции нет тела запроса» — что объявлено и почему не проверяется."""
    handle = operations.api.upload_attachment

    with pytest.raises(ResponseVariantError) as info:
        handle.request.body()
    message = str(info.value)
    assert "у операции нет проверяемого тела запроса" in message
    assert "объявлено без проверки: multipart/form-data" in message

    with pytest.raises(
        ResponseVariantError, match=r"объявлено спецификацией .*, но не проверяется"
    ):
        handle.request.body("multipart/form-data")
    with pytest.raises(ResponseVariantError, match="объявлено без проверки: multipart/form-data"):
        handle.describe_request_body()

    with pytest.raises(ResponseVariantError) as info:
        operations.api.mixed_bodies.request.body("text/plain")
    assert "доступны ['application/json']" in str(info.value)
    assert "multipart/form-data" in str(info.value)


def test_response_lookup_names_the_unchecked_response(operations: Any) -> None:
    handle = operations.api.upload_attachment

    with pytest.raises(ResponseVariantError) as info:
        handle.response(status=200)
    message = str(info.value)
    assert "объявлен спецификацией, но не проверяется" in message
    assert "application/octet-stream" in message
    assert "доступны []" not in message

    with pytest.raises(ResponseVariantError, match="не проверяется"):
        handle.describe_response(status=200, content_type="application/octet-stream")

    with pytest.raises(ResponseVariantError, match="объявлено без проверки"):
        handle.response(status=404)


def test_mock_of_an_unchecked_response_explains_why_there_is_no_variant(
    operations: Any,
) -> None:
    with pytest.raises(ResponseVariantError, match="не проверяется"):
        operations.api.upload_attachment.mock(response="x", status=200)


def test_wildcard_request_body_covers_any_content_type(operations: Any) -> None:
    """``*/*`` в теле запроса — JSON-контракт для любого присланного JSON-типа."""
    recorded(
        operations,
        "api.anyRequest",
        body={"login": "robot"},
        raw_body=b'{"login": "robot"}',
        content_type="application/json",
    )
    with pytest.raises(RequestContractError) as info:
        recorded(
            operations, "api.anyRequest", body={}, raw_body=b"{}", content_type="application/json"
        )
    assert info.value.validator == "required"
    # Текстовый запрос разбирается как текст: строка — не объект.
    with pytest.raises(RequestContractError) as info:
        recorded(
            operations,
            "api.anyRequest",
            body='{"login": "robot"}',
            raw_body=b'{"login": "robot"}',
            content_type="text/plain",
        )
    assert info.value.validator == "type"
    # Бинарное тело ``*/*`` покрывает, но разобрать его для проверки нечем.
    recorded(
        operations,
        "api.anyRequest",
        body=b"\x89PNG",
        raw_body=b"\x89PNG",
        content_type="image/png",
    )


def test_media_range_request_body_is_matched_by_type(operations: Any) -> None:
    """``image/*`` объявлен, клиент шлёт ``image/png`` — это тот же вариант."""
    recorded(
        operations,
        "api.imageUpload",
        body=b"\x89PNG",
        raw_body=b"\x89PNG",
        content_type="image/png",
    )
    with pytest.raises(RequestContractError) as info:
        recorded(operations, "api.imageUpload", body={}, content_type="application/json")
    assert "image/*" in str(info.value)


def test_d42_schema_lookup_names_unchecked_bodies(operations: Any) -> None:
    from geas.errors import OperationLookupError
    from geas.models import Direction

    handle = operations.api.upload_attachment

    with pytest.raises(OperationLookupError) as info:
        handle.d42_schema(Direction.REQUEST)
    assert "нет проверяемого тела запроса" in str(info.value)
    assert "request multipart/form-data" in str(info.value)
    with pytest.raises(OperationLookupError, match="нет проверяемого ответа"):
        handle.d42_schema(Direction.RESPONSE)


def test_document_without_unchecked_key_still_loads(tmp_path: Path) -> None:
    """Артефакты прошлой версии без ``unchecked`` читаются: непроверяемых тел просто нет."""
    from geas.runtime import handle_from_document

    project = text_project(tmp_path / "project", ("textPreview",), package="legacy_contracts")
    project.update()
    document = project.contract_document("api__text_preview")
    assert "unchecked" not in document

    handle = handle_from_document(document)

    assert handle.unchecked == ()
    assert handle.request.unchecked_bodies == ()
    handle.validate_response("ok", status=200)


def test_unchecked_key_is_not_a_semantic_change(tmp_path: Path) -> None:
    """После обновления ``geas diff`` не считает появление ``unchecked: []`` изменением контракта."""
    from geas.semantic_diff import diff_documents

    project = text_project(tmp_path / "project", ("uploadAttachment",), package="diff_contracts")
    project.update()
    after = project.contract_document("api__upload_attachment")
    before = {key: value for key, value in after.items() if key != "unchecked"}

    diff = diff_documents(before, after, "api.uploadAttachment")

    assert not diff.is_semantic
    assert [change.pointer for change in diff.cosmetic] == ["/unchecked"]


def test_changes_inside_unchecked_are_semantic(tmp_path: Path) -> None:
    """``required`` у непроверяемого тела меняет проверку запроса — это изменение контракта."""
    from geas.semantic_diff import diff_documents, semantic_fingerprint

    project = text_project(tmp_path / "project", ("uploadAttachment",), package="diff2_contracts")
    project.update()
    after = project.contract_document("api__upload_attachment")
    before = json.loads(json.dumps(after))
    before["unchecked"][0]["required"] = False

    diff = diff_documents(before, after, "api.uploadAttachment")

    assert diff.is_semantic
    assert [change.pointer for change in diff.semantic] == ["/unchecked/0/required"]
    assert semantic_fingerprint(before) != semantic_fingerprint(after)


def test_operation_without_unchecked_bodies_keeps_its_document(tmp_path: Path) -> None:
    """Без непроверяемых тел ключа ``unchecked`` нет: JSON-only артефакты не меняются."""
    from geas.semantic_diff import semantic_fingerprint

    project = text_project(tmp_path / "project", ("textPreview",), package="fp_contracts")
    project.update()
    document = project.contract_document("api__text_preview")

    assert "unchecked" not in document
    assert semantic_fingerprint(document) == semantic_fingerprint({**document, "unchecked": []})


def test_one_schema_request_bodies_need_no_content_type(operations: Any) -> None:
    """Тело запроса Swashbuckle: ``application/json`` и ``text/json`` с одним ``$ref``."""
    handle = operations.api.swashbuckle_request

    assert handle.request.body().content_type == "application/json"
    handle.validate_request_body({"login": "robot"})
    (unchecked,) = handle.request.unchecked_bodies
    assert unchecked.content_type == "text/plain"
    assert "как и у application/json, text/json" in unchecked.reason


def test_one_schema_variants_prefer_a_concrete_json_type_over_wildcard(operations: Any) -> None:
    """``application/problem+json`` и ``*/*`` с одной схемой — выбирается конкретный тип."""
    handle = operations.api.problem_and_wildcard

    assert handle.response(status=200).content_type == "application/problem+json"


def test_request_body_lookup_matches_a_media_range(operations: Any) -> None:
    """``request.body("application/json")`` у тела ``*/*`` — это тело ``*/*``."""
    handle = operations.api.any_request

    assert handle.request.body("application/json").content_type == "*/*"
    handle.validate_request_body({"login": "robot"}, content_type="application/json")
    with pytest.raises(ResponseVariantError, match=r"\(image/\*\), но не проверяется"):
        operations.api.image_upload.request.body("image/png")


# ------------------------------------------------------------- живой обмен


async def send(
    server: JJServer, method: str, path: str, **kwargs: Any
) -> tuple[int, aiohttp.typedefs.LooseHeaders, bytes]:
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, f"{server.url}{path}", **kwargs) as response,
    ):
        return response.status, dict(response.headers), await response.read()


async def test_mock_returns_a_text_response_as_is(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Строка уходит как есть, без кавычек JSON, и с ``Content-Type`` варианта."""
    async with operations.api.text_preview.mock(
        response="Привет, мир", status=200, wait_for_requests=1
    ) as mock:
        status, headers, raw = await send(
            jj_server,
            "POST",
            "/pattern/textPreview",
            data="Привет".encode(),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    assert status == 200
    assert raw.decode("utf-8") == "Привет, мир"
    assert str(headers["Content-Type"]).split(";")[0] == "text/plain"
    assert "charset=utf-8" in str(headers["Content-Type"]).lower()
    assert mock.diagnostics == ()
    assert mock.requests[0].method == "POST"


async def test_mock_serves_the_response_in_the_charset_the_caller_asked_for(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    async with operations.api.text_preview.mock(
        response="Привет",
        status=200,
        response_headers={"Content-Type": "text/plain; charset=windows-1251"},
        wait_for_requests=1,
    ):
        status, headers, raw = await send(
            jj_server,
            "POST",
            "/pattern/textPreview",
            data=b"abc",
            headers={"Content-Type": "text/plain"},
        )

    assert status == 200
    assert raw == "Привет".encode("windows-1251")
    assert "windows-1251" in str(headers["Content-Type"]).lower()


async def test_mock_serves_text_html(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    async with operations.api.render_fragment.mock(
        response="<p>фрагмент</p>",
        status=200,
        path_params={"fragmentId": "f-1"},
        wait_for_requests=1,
    ):
        status, headers, raw = await send(jj_server, "GET", "/fragments/f-1")

    assert status == 200
    assert str(headers["Content-Type"]).split(";")[0] == "text/html"
    assert raw.decode("utf-8") == "<p>фрагмент</p>"


async def test_mock_serves_a_text_response_by_ref(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    async with operations.api.compile_pattern.mock(
        response="Текст шаблона", status=200, path_params={"patternId": 7}, wait_for_requests=1
    ):
        status, _, raw = await send(jj_server, "GET", "/pattern/7/compile")

    assert status == 200
    assert raw.decode("utf-8") == "Текст шаблона"


async def test_mock_picks_the_text_variant_of_a_shared_status(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    handle = operations.api.json_or_text
    with pytest.raises(ResponseVariantError, match="не выбран однозначно"):
        handle.mock(response="x", status=200)

    async with handle.mock(
        response="только текст", status=200, content_type="text/plain", wait_for_requests=1
    ):
        text_status, text_headers, text_raw = await send(jj_server, "GET", "/messages/preview")
    async with handle.mock(
        response={"text": "объект"},
        status=200,
        content_type="application/json",
        wait_for_requests=1,
    ):
        _, json_headers, json_raw = await send(jj_server, "GET", "/messages/preview")

    assert text_status == 200
    assert text_raw.decode("utf-8") == "только текст"
    assert str(text_headers["Content-Type"]).split(";")[0] == "text/plain"
    assert json.loads(json_raw) == {"text": "объект"}
    assert str(json_headers["Content-Type"]).split(";")[0] == "application/json"


@pytest.mark.parametrize(
    "response",
    [
        pytest.param("x" * 201, id="longer-than-maxLength"),
        pytest.param({"text": "объект"}, id="object"),
        pytest.param(42, id="number"),
    ],
)
def test_mock_rejects_a_text_response_outside_the_contract(operations: Any, response: Any) -> None:
    """Проверка идёт до регистрации мока: в JJ ничего не попадает."""
    with pytest.raises(ResponseContractError):
        operations.api.text_preview.mock(response=response, status=200)


def test_mock_rejects_a_content_type_that_is_not_the_variants(operations: Any) -> None:
    with pytest.raises(ContractMockError, match="content type ответа не совпадает"):
        operations.api.text_preview.mock(
            response="ok", status=200, response_headers={"Content-Type": "application/json"}
        )


def test_mock_picks_the_text_variant_of_default_by_content_type(operations: Any) -> None:
    """Fallback на ``default`` выбирает вариант по ``content_type``, а не первый попавшийся."""
    handle = operations.api.text_default

    text = handle.mock(response="ab", status=500, content_type="text/plain")
    as_json = handle.mock(response={"login": "x"}, status=500, content_type="application/json")

    assert (text.status, text.variant.content_type) == (500, "text/plain")
    assert as_json.variant.content_type == "application/json"
    with pytest.raises(ResponseVariantError, match="не выбран однозначно"):
        handle.mock(response="ab", status=500)
    with pytest.raises(ResponseContractError):
        handle.mock(response="abcd", status=500, content_type="text/plain")


def test_mock_does_not_fall_back_to_default_for_a_declared_status(operations: Any) -> None:
    """Объявленный статус чужой схемой ``default`` не мокается."""
    with pytest.raises(ResponseVariantError, match="200:application/json"):
        operations.api.text_default.mock(
            response={"login": "x"}, status=200, content_type="application/json"
        )
    with pytest.raises(ResponseVariantError, match="объявлен спецификацией, но не проверяется"):
        operations.api.download_with_default.mock(
            response={"login": "x"}, status=200, path_params={"downloadId": "d-1"}
        )
    # Необъявленный статус по-прежнему отдаётся вариантом ``default``.
    mock = operations.api.download_with_default.mock(
        response={"login": "x"}, status=404, path_params={"downloadId": "d-1"}
    )
    assert mock.status == 404


@pytest.mark.parametrize(
    "response, content_type, message",
    [
        pytest.param("ok", "text/plain; charset=klingon", "неизвестный charset", id="unknown"),
        pytest.param(
            "Привет", "text/plain; charset=latin-1", "не кодируется в latin-1", id="lossy"
        ),
    ],
)
def test_mock_rejects_a_text_body_that_does_not_encode(
    operations: Any, response: str, content_type: str, message: str
) -> None:
    """Ошибка кодировки — на создании мока, а не из JJ на входе в ``async with``."""
    with pytest.raises(ContractMockError, match=message):
        operations.api.text_preview.mock(
            response=response, status=200, response_headers={"Content-Type": content_type}
        )


async def test_mock_checks_the_text_request_on_exit(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Запрос с телом вне контракта (слишком длинная строка) падает на выходе из блока."""
    with pytest.raises(RequestContractError) as info:
        async with operations.api.text_preview.mock(response="ok", status=200, wait_for_requests=1):
            await send(
                jj_server,
                "POST",
                "/pattern/textPreview",
                data=b"x" * 41,
                headers={"Content-Type": "text/plain"},
            )

    assert info.value.validator == "maxLength"


async def test_mock_checks_the_text_request_in_the_declared_charset(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Кириллица в windows-1251 проходит: 6 символов, а не нечитаемые UTF-8 байты."""
    async with operations.api.text_preview.mock(
        response="ok", status=200, wait_for_requests=1
    ) as mock:
        await send(
            jj_server,
            "POST",
            "/pattern/textPreview",
            data="привет".encode("windows-1251"),
            headers={"Content-Type": "text/plain; charset=windows-1251"},
        )

    assert mock.diagnostics == ()


async def test_mock_rejects_a_json_request_to_a_text_operation(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    with pytest.raises(RequestContractError, match="не описан контрактом"):
        async with operations.api.text_preview.mock(response="ok", status=200, wait_for_requests=1):
            await send(jj_server, "POST", "/pattern/textPreview", json={"a": 1})


async def test_wildcard_response_is_served_as_json(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """``*/*`` — не заголовок: JSON-ответ подписывается как ``application/json``."""
    async with operations.api.any_content.mock(
        response={"login": "robot"}, status=200, wait_for_requests=1
    ):
        status, headers, raw = await send(jj_server, "GET", "/communications/myself")

    assert status == 200
    assert str(headers["Content-Type"]).split(";")[0] == "application/json"
    assert json.loads(raw) == {"login": "robot"}


def test_wildcard_response_with_a_binary_content_type_is_rejected(operations: Any) -> None:
    with pytest.raises(ContractMockError, match="описан JSON-схемой"):
        operations.api.any_content.mock(
            response={"login": "robot"},
            status=200,
            response_headers={"Content-Type": "application/octet-stream"},
        )


def test_wildcard_response_served_as_text_needs_a_string(operations: Any) -> None:
    with pytest.raises(ContractMockError, match="отдаётся строкой как есть"):
        operations.api.any_content.mock(
            response={"login": "robot"},
            status=200,
            response_headers={"Content-Type": "text/plain"},
        )


async def test_wildcard_string_response_with_text_content_type_is_served_as_text(
    jj_server: JJServer,  # noqa: F811 - фикстура
    tmp_path: Path,
) -> None:
    """``*/*`` со строковой схемой и ``Content-Type: text/plain`` — текст, а не ``"hello"``."""
    project = make_project(tmp_path / "project", package="wildcard_text_contracts")
    project.write(
        "api/spec.yaml",
        """\
        openapi: "3.0.3"
        info: {title: t, version: "1"}
        paths:
          /hello:
            get:
              operationId: hello
              responses:
                "200":
                  description: Строка
                  content:
                    "*/*":
                      schema:
                        type: string
        """,
    )
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={"api.hello": {"source": "main", "operation_id": "hello"}},
    )
    project.update()
    with project.importable() as module:
        async with module.operations.api.hello.mock(
            response="hello",
            status=200,
            response_headers={"Content-Type": "text/plain"},
            wait_for_requests=1,
        ):
            _, headers, raw = await send(jj_server, "GET", "/hello")

    assert raw == b"hello"
    assert str(headers["Content-Type"]).split(";")[0] == "text/plain"


# --------------------------------------------------------------------- CLI


def cli_project(tmp_path: Path, operation_ids: tuple[str, ...]) -> Project:
    project = text_project(tmp_path / "workspace", operation_ids, package="cli_contracts")
    return project


def test_cli_list_shows_text_variants_as_checked_and_binary_as_unchecked(tmp_path: Path) -> None:
    project = cli_project(tmp_path, ("textPreview", "uploadAttachment"))

    human = project.cli("list")
    machine = project.cli("list", "--json")

    assert human.returncode == 0, human.stderr
    assert "request: text/plain" in human.stdout
    assert "responses: 200:text/plain" in human.stdout
    assert (
        "объявлено, но не проверяется: request multipart/form-data: media type "
        "'multipart/form-data'" in human.stdout
    )
    (source,) = json.loads(machine.stdout)["sources"]
    by_id = {item["operation_id"]: item for item in source["operations"]}
    assert by_id["textPreview"]["request_content_types"] == ["text/plain"]
    assert by_id["textPreview"]["responses"] == ["200:text/plain"]
    assert by_id["uploadAttachment"]["request_content_types"] == []
    assert list(by_id["uploadAttachment"]["unchecked_request_content_types"]) == [
        "multipart/form-data"
    ]


def test_cli_update_reports_unchecked_bodies(tmp_path: Path) -> None:
    project = cli_project(tmp_path, ("uploadAttachment",))

    result = project.cli("update")

    assert result.returncode == 0, result.stderr
    assert (
        "api.uploadAttachment: объявлено, но не проверяется — request body multipart/form-data"
        in result.stdout
    )
    assert "response 200:application/octet-stream" in result.stdout


def test_cli_update_reports_and_rejects_non_string_text_bodies(tmp_path: Path) -> None:
    """Незакреплённый — в выводе как непроверяемый, закреплённый — ошибка с кодом 1."""
    project = cli_project(tmp_path, ("objectAsText",))

    unpinned = project.cli("update")

    assert unpinned.returncode == 0, unpinned.stderr
    assert "api.objectAsText: объявлено, но не проверяется — request body text/plain" in (
        unpinned.stdout
    )

    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={
            "api.objectAsText": {
                "source": "main",
                "operation_id": "objectAsText",
                "request": {"content_type": "text/plain"},
            }
        },
    )
    pinned = project.cli("update")

    assert pinned.returncode == 1
    assert "тело запроса text/plain — строка" in pinned.stderr


def test_cli_add_pins_a_text_request_content_type(tmp_path: Path) -> None:
    project = cli_project(tmp_path, ())

    result = project.cli(
        "add",
        "api.textPreview",
        "--source",
        "main",
        "--operation-id",
        "textPreview",
        "--request-content-type",
        "text/plain",
        "--response",
        "200:text/plain",
    )

    assert result.returncode == 0, result.stderr
    assert project.cli("update").returncode == 0
    assert (
        "text/plain"
        in project.contract_document("api__text_preview")["request"]["bodies"][0]["content_type"]
    )


def test_cli_add_explains_that_a_declared_request_body_is_not_checked(tmp_path: Path) -> None:
    """Раньше: «доступны: <нет тела>» — будто спецификация тела не объявляет."""
    project = cli_project(tmp_path, ())
    before = project.manifest_bytes()

    result = project.cli(
        "add",
        "api.uploadAttachment",
        "--source",
        "main",
        "--operation-id",
        "uploadAttachment",
        "--request-content-type",
        "multipart/form-data",
    )

    assert result.returncode == 2
    assert "объявлен операцией, но не проверяется" in result.stderr
    assert "<нет тела>" not in result.stderr
    assert project.manifest_bytes() == before


def test_cli_add_lists_declared_types_when_the_requested_one_is_unknown(tmp_path: Path) -> None:
    project = cli_project(tmp_path, ())

    result = project.cli(
        "add",
        "api.mixedBodies",
        "--source",
        "main",
        "--operation-id",
        "mixedBodies",
        "--request-content-type",
        "text/plain",
    )

    assert result.returncode == 2
    assert "['application/json', 'multipart/form-data']" in result.stderr
    assert "из них не проверяются: ['multipart/form-data']" in result.stderr


def test_cli_show_prints_a_text_body_and_explains_an_unchecked_one(tmp_path: Path) -> None:
    project = cli_project(tmp_path, ("textPreview", "uploadAttachment"))
    assert project.cli("update").returncode == 0

    text = project.cli("show", "api.textPreview", "--direction", "request")
    unchecked = project.cli("show", "api.uploadAttachment", "--direction", "request")
    response = project.cli("show", "api.uploadAttachment", "--status", "200")

    assert text.returncode == 0, text.stderr
    assert "text/plain" in text.stdout
    assert "string" in text.stdout
    assert unchecked.returncode == 2
    assert "нет проверяемого тела запроса" in unchecked.stderr
    assert "объявлено без проверки: multipart/form-data" in unchecked.stderr
    assert response.returncode == 2
    assert "объявлен спецификацией, но не проверяется" in response.stderr


def test_cli_add_warns_about_an_unchecked_response_selector(tmp_path: Path) -> None:
    """Закрепить непроверяемый вариант можно (как на ``main``), но ``add`` об этом предупреждает."""
    project = cli_project(tmp_path, ())

    result = project.cli(
        "add",
        "api.download",
        "--source",
        "main",
        "--operation-id",
        "downloadWithDefault",
        "--response",
        "200:application/octet-stream",
    )

    assert result.returncode == 0, result.stderr
    assert "предупреждение: вариант ответа '200:application/octet-stream'" in result.stderr
    assert "Мок и проверка тела для него недоступны" in result.stderr
    assert "Проверяемые: default:application/json" in result.stderr
    manifest = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))
    assert manifest["operations"]["api.download"]["responses"] == [
        {"status": 200, "content_type": "application/octet-stream"}
    ]


def test_cli_add_names_unchecked_responses_when_nothing_is_checked(tmp_path: Path) -> None:
    project = cli_project(tmp_path, ())

    result = project.cli(
        "add", "api.upload", "--source", "main", "--operation-id", "uploadAttachment"
    )

    assert result.returncode == 2
    assert "нет проверяемого варианта ответа" in result.stderr
    assert "200:application/octet-stream" in result.stderr
    assert "Закрепите нужный явно через --response" in result.stderr


def swagger2_cli_project(tmp_path: Path) -> Project:
    project = make_project(tmp_path / "workspace", package="sw2_cli_contracts")
    project.write_spec("api/spec.json", spec("features", "text_bodies_swagger2.json"))
    project.write_manifest(sources={"main": {"path": "api/spec.json", "selection": "explicit"}})
    return project


def test_cli_add_pins_json_next_to_text_plain_without_flags(tmp_path: Path) -> None:
    """Общие ``consumes``/``produces: [application/json, text/plain]``: закрепляется JSON."""
    project = swagger2_cli_project(tmp_path)

    result = project.cli(
        "add", "api.create", "--source", "main", "--operation-id", "createWithText"
    )

    assert result.returncode == 0, result.stderr
    manifest = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))
    entry = manifest["operations"]["api.create"]
    assert entry["request"] == {"content_type": "application/json"}
    assert entry["responses"] == [{"status": 201, "content_type": "application/json"}]


def test_cli_add_still_asks_when_there_are_two_json_types(tmp_path: Path) -> None:
    """``application/json`` и ``merge-patch+json`` — настоящий выбор, его делает человек."""
    project = make_project(tmp_path / "workspace", package="multi_cli_contracts")
    project.write_spec("api/spec.yaml", spec("features", "multi_content_types.yaml"))
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}})

    result = project.cli(
        "add", "api.probe", "--source", "main", "--operation-id", "replaceDocumentContent"
    )

    assert result.returncode == 2
    assert "несколько request content types" in result.stderr
    assert "application/merge-patch+json" in result.stderr


# ------------------------------------------- закреплённые в manifest варианты


def added_project(tmp_path: Path, operation_id: str, key: str) -> Any:
    """Операция, добавленная ``geas add`` без флагов: так закрепляет её пользователь."""
    project = cli_project(tmp_path, ())
    result = project.cli("add", key, "--source", "main", "--operation-id", operation_id)
    assert result.returncode == 0, result.stderr
    assert project.cli("update").returncode == 0
    return project


def test_pinned_operation_keeps_unchecked_responses(tmp_path: Path) -> None:
    """``geas add`` закрепляет ``default``, но ``200: octet-stream`` мок не подменяет."""
    project = added_project(tmp_path, "downloadWithDefault", "api.download")
    manifest = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))
    assert manifest["operations"]["api.download"]["responses"] == [
        {"status": "default", "content_type": "application/json"}
    ]

    with project.importable() as module:
        handle = module.operations.api.download
        assert [item.label() for item in handle.unchecked] == [
            "response 200:application/octet-stream"
        ]
        with pytest.raises(ResponseVariantError, match="объявлен спецификацией, но не проверяется"):
            handle.response(status=200)
        with pytest.raises(ResponseVariantError, match="объявлен спецификацией, но не проверяется"):
            handle.mock(response={"login": "x"}, status=200, path_params={"downloadId": "d-1"})


def test_pinned_request_keeps_unchecked_bodies(tmp_path: Path) -> None:
    """``geas add`` закрепляет ``application/json``, а multipart-запрос не отклоняется."""
    project = added_project(tmp_path, "mixedBodies", "api.mixed")

    with project.importable() as module:
        handle = module.operations.api.mixed
        assert [item.content_type for item in handle.request.unchecked_bodies] == [
            "multipart/form-data"
        ]
        handle.validate_recorded_request(
            method="POST",
            path="/mixed",
            segments={},
            params=[],
            headers=[("Content-Type", "multipart/form-data; boundary=b")],
            body=b"--b",
            raw_body=b"--b",
            index=0,
        )


def test_swagger2_unchecked_text_variant_leaves_no_trace(tmp_path: Path) -> None:
    """Отвергнутый ``400: text/plain`` не требует waiver'ов, не отключает d42 у ``200``
    и не добавляет свою d42-схему в модуль."""
    project = make_project(tmp_path / "project", package="dirty_contracts")
    project.write_spec("api/spec.json", spec("features", "text_bodies_swagger2.json"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.json", "selection": "explicit"}},
        operations={"api.dirty": {"source": "main", "operation_id": "textWithDirtyErrors"}},
    )

    project.update()

    document = project.contract_document("api__dirty")
    assert [(item["status"], item["content_type"]) for item in document["responses"]] == [
        (200, "text/plain")
    ]
    (unchecked,) = document["unchecked"]
    assert unchecked["status"] == 400
    # Схема заведомо объект: заготовка waiver'а для поля без схемы не помогла бы.
    assert unchecked["reason"].startswith("схема описывает object")
    assert "allow_any" not in unchecked["reason"]
    assert document["d42"]["response_module"] == "api__dirty_response"
    module = (project.output_dir / "_d42" / "api__dirty_response.py").read_text(encoding="utf-8")
    assert "DirtyError" not in module


def sw2_project(tmp_path: Path, operations: dict[str, Any], *, package: str) -> Project:
    project = make_project(tmp_path / "project", package=package)
    project.write_spec("api/spec.json", spec("features", "text_bodies_swagger2.json"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.json", "selection": "explicit"}},
        operations=operations,
    )
    return project


def _waiver_from_template(text: str, *, issue: str) -> dict[str, Any]:
    """Waiver из заготовки в сообщении нормализатора."""
    fields = dict(
        re.findall(
            r"^\s*-?\s*(operation|direction|json_pointer|rule|expected_source): (\S+)$", text, re.M
        )
    )
    return {
        **fields,
        "reason": "проверка",
        "owner": "qa",
        "issue": issue,
        "expires_at": "2099-01-01",
    }


def test_swagger2_operation_consumes_is_not_shared(tmp_path: Path) -> None:
    """``consumes: [text/plain]`` операции — выбор автора, а не общий список корня."""
    project = make_project(tmp_path / "project", package="own_consumes_contracts")
    project.write_spec("api/spec.json", spec("features", "text_bodies_swagger2.json"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.json", "selection": "explicit"}},
        operations={"api.body": {"source": "main", "operation_id": "objectBody"}},
    )

    (unchecked,) = project.build().by_key("api.body").unchecked

    assert "общего consumes/produces" not in unchecked.reason
    assert "закреплённый в manifest вариант" in unchecked.reason


def test_text_value_is_required_even_when_a_branch_allows_anything(tmp_path: Path) -> None:
    """Вложенная схема без ограничений (``anyOf`` с ``allow_any``) под ``text/plain``.

    JSON Schema такую ветку не сужает, но тело ``text/*`` — всегда строка.
    """
    project = build_one(tmp_path, "anyTextRef")
    (unchecked,) = project.build().by_key("api.anyTextRef").unchecked
    project.write_waivers([_waiver_from_template(unchecked.reason, issue="QAAUTOSAM-692")])
    project.update()

    with project.importable() as module:
        handle = module.operations.api.any_text_ref
        handle.validate_response("любой текст", status=200)
        with pytest.raises(ResponseContractError):
            handle.validate_response({"login": "robot"}, status=200)


def test_unpinned_json_status_still_falls_back_to_default_next_to_xml(tmp_path: Path) -> None:
    """``404`` с JSON и XML, закреплены ``200`` и ``default``: мок ``404`` — это ``default``.

    Непроверяемый ``404: application/xml`` не объявляет статус: у ``404`` есть
    проверяемый JSON-вариант, просто не закреплённый, — как на ``main``.
    """
    project = sw2_project(
        tmp_path,
        {
            "api.item": {
                "source": "main",
                "operation_id": "jsonXmlWithDefault",
                "responses": [
                    {"status": 200, "content_type": "application/json"},
                    {"status": "default", "content_type": "application/json"},
                ],
            }
        },
        package="json_xml_contracts",
    )
    project.update()

    with project.importable() as module:
        handle = module.operations.by_key("api.item")
        assert sorted(item.label() for item in handle.unchecked) == [
            "response 200:application/xml",
            "response default:application/xml",
        ]
        mock = handle.mock(response={"code": "x"}, status=404)
        assert (mock.status, mock.variant.status) == (404, "default")


def test_error_dto_of_an_unchecked_text_variant_stays_out_of_d42(tmp_path: Path) -> None:
    """Определение, на которое ссылается только отвергнутый вариант, в контракт не попадает."""
    project = sw2_project(
        tmp_path,
        {"api.text": {"source": "main", "operation_id": "textWithErrorDto"}},
        package="error_dto_contracts",
    )
    project.update()

    document = project.contract_document("api__text")
    assert [item["status"] for item in document["unchecked"]] == [400]
    module = (project.output_dir / "_d42" / "api__text_response.py").read_text(encoding="utf-8")
    assert "ErrorDto" not in module
    assert "$defs" not in document["responses"][0]["schema"]


def test_waiver_for_a_json_variant_does_not_break_the_text_neighbour(tmp_path: Path) -> None:
    """Waiver на ``/body`` без ``content_type`` выписан для JSON-варианта того же статуса.

    Раньше текстовый вариант не нормализовался, и такой waiver генерацию не ломал. Теперь
    текстовый вариант с чужим waiver'ом уходит в ``unchecked`` с причиной.
    """
    from geas.errors import WaiverError

    project = build_one(tmp_path, "jsonOrText")
    waiver = {
        "operation": "api.jsonOrText",
        "direction": "response",
        "json_pointer": "/body",
        "rule": "allow_null",
        "expected_source": "0" * 64,
        "reason": "проверка",
        "owner": "qa",
        "issue": "QAAUTOSAM-692",
        "expires_at": "2099-01-01",
    }
    project.write_waivers([waiver])
    with pytest.raises(WaiverError) as info:
        project.build()
    actual = re.search(r"фактический:\s+(\w+)", str(info.value))
    assert actual is not None
    project.write_waivers([{**waiver, "expected_source": actual.group(1)}])

    built = project.build().by_key("api.jsonOrText")

    assert [item["content_type"] for item in built.document["responses"]] == ["application/json"]
    (unchecked,) = built.unchecked
    assert unchecked.content_type == "text/plain"
    assert "устарел" in unchecked.reason


def test_stale_request_waiver_suggests_pinning_the_request_body(tmp_path: Path) -> None:
    """У waiver'а запроса нет ``content_type``: подсказка — закрепить ``request.content_type``."""
    from geas.errors import WaiverError

    project = build_one(tmp_path, "mixedBodies")
    project.write_waivers(
        [
            {
                "operation": "api.mixedBodies",
                "direction": "request",
                "json_pointer": "/body",
                "rule": "allow_null",
                "expected_source": "0" * 64,
                "reason": "проверка",
                "owner": "qa",
                "issue": "QAAUTOSAM-692",
                "expires_at": "2099-01-01",
            }
        ]
    )

    with pytest.raises(
        WaiverError, match=r"закрепите это тело в manifest полем request\.content_type"
    ):
        project.build()


def test_unchecked_text_request_body_is_accepted(operations: Any) -> None:
    """``text/plain`` с объектом у ``objectAsText`` не проверяется, но и не отклоняется."""
    recorded(operations, "api.objectAsText", body="что угодно", raw_body="что угодно".encode())
    with pytest.raises(RequestContractError, match="не описан контрактом"):
        recorded(operations, "api.objectAsText", body={}, content_type="application/json")


def test_most_specific_request_body_wins(operations: Any) -> None:
    """``application/json`` точнее ``*/*``: тело проверяется своей схемой, а не чужой."""
    recorded(operations, "api.wildcardDiffers", body={"a": "x"}, content_type="application/json")
    with pytest.raises(RequestContractError) as info:
        recorded(
            operations, "api.wildcardDiffers", body={"b": "x"}, content_type="application/json"
        )
    assert info.value.validator == "required"
    # Другой JSON-тип покрывает только ``*/*`` — и проверяется его схемой.
    recorded(
        operations,
        "api.wildcardDiffers",
        body=b'{"b": "x"}',
        raw_body=b'{"b": "x"}',
        content_type="application/vnd.example+json",
    )
    with pytest.raises(RequestContractError):
        recorded(
            operations,
            "api.wildcardDiffers",
            body=b'{"a": "x"}',
            raw_body=b'{"a": "x"}',
            content_type="application/vnd.example+json",
        )


def test_non_string_branch_inside_a_text_composition_is_unchecked(tmp_path: Path) -> None:
    project = build_one(tmp_path, "mixedComposition")

    (unchecked,) = project.build().by_key("api.mixedComposition").unchecked

    assert unchecked.reason.startswith("схема описывает object")


def test_same_schema_on_different_statuses_is_still_ambiguous(tmp_path: Path) -> None:
    """Одна схема у ``200`` и ``201`` — разные статусы, выбрать за пользователя нельзя."""
    project = build_one(tmp_path, "twoStatuses")
    project.update()

    with (
        project.importable() as module,
        pytest.raises(ResponseVariantError, match="не выбран однозначно"),
    ):
        module.operations.api.two_statuses.response()


def test_json_string_and_text_with_one_schema_are_not_merged(tmp_path: Path) -> None:
    """JSON-литерал ``"abc"`` и текст ``abc`` на проводе разные: нужен ``content_type``."""
    project = build_one(tmp_path, "stringTwice")
    project.update()

    with (
        project.importable() as module,
        pytest.raises(ResponseVariantError, match="не выбран однозначно"),
    ):
        module.operations.api.string_twice.response(status=200)


def test_raw_bytes_win_over_the_body_jj_decoded_as_utf8(tmp_path: Path) -> None:
    """``Рё`` в windows-1251 — это байты ``d0 b8``, которые JJ прочитал бы как UTF-8 ``и``."""
    project = build_one(tmp_path, "shortCode")
    project.update()

    with project.importable() as module:
        handle = module.operations.api.short_code
        arguments: dict[str, Any] = {
            "method": "POST",
            "path": "/short-code",
            "segments": {},
            "params": [],
            "index": 0,
        }
        handle.validate_recorded_request(
            headers=[("Content-Type", "text/plain")], body="и", raw_body=b"\xd0\xb8", **arguments
        )
        with pytest.raises(RequestContractError) as info:
            handle.validate_recorded_request(
                headers=[("Content-Type", "text/plain; charset=windows-1251")],
                body="и",
                raw_body=b"\xd0\xb8",
                **arguments,
            )
    assert info.value.validator == "maxLength"


@pytest.mark.parametrize(
    "content_types, expected",
    [
        (["application/json", "text/plain"], "application/json"),
        (["application/json", "text/json", "*/*"], "application/json"),
        (["*/*", "text/plain"], "*/*"),
        (["application/problem+json", "text/plain"], "application/problem+json"),
        (["application/json", "application/merge-patch+json"], None),
        (["text/plain", "text/html"], None),
    ],
)
def test_add_prefers_the_only_primary_json_type(
    content_types: list[str], expected: str | None
) -> None:
    from geas.cli import _preferred_media_type

    assert _preferred_media_type(content_types) == expected


# ----------------------------------------------- схемы, которые не разбираются


@pytest.mark.parametrize("operation_id", ["brokenTextRef", "missingTextRef"])
def test_broken_ref_of_an_unpinned_text_variant_does_not_break_generation(
    tmp_path: Path, operation_id: str
) -> None:
    """Битый ``$ref`` или ``$ref`` с соседним ``maxLength`` у ``text/plain`` — ``unchecked``.

    На ``main`` такой вариант не нормализовался, и генерация проходила.
    """
    from geas.errors import RefResolutionError

    project = build_one(tmp_path, operation_id)

    (unchecked,) = project.build().by_key(f"api.{operation_id}").unchecked
    assert unchecked.reason.startswith("схема не разбирается: вариант text/plain не проверяется")
    assert "Ошибка нормализации:" in unchecked.reason

    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={
            f"api.{operation_id}": {
                "source": "main",
                "operation_id": operation_id,
                "responses": [{"status": 200, "content_type": "text/plain"}],
            }
        },
    )
    with pytest.raises(RefResolutionError):
        project.build()


@pytest.mark.parametrize(
    "fixture", ["text_bodies.yaml", "text_bodies_swagger2.json"], ids=["openapi30", "swagger2"]
)
def test_binary_download_under_wildcard_is_unchecked(tmp_path: Path, fixture: str) -> None:
    """``*/*: {type: string, format: binary}`` — файл, а не JSON-строка в кавычках."""
    project = make_project(tmp_path / "project", package="download_contracts")
    project.write_spec(f"api/{fixture}", spec("features", fixture))
    project.write_manifest(
        sources={"main": {"path": f"api/{fixture}", "selection": "explicit"}},
        operations={"api.download": {"source": "main", "operation_id": "downloadAny"}},
    )

    built = project.build().by_key("api.download")

    assert built.document["responses"] == []
    (unchecked,) = built.unchecked
    assert unchecked.content_type == "*/*"
    assert "бинарное тело" in unchecked.reason


def test_ref_chain_across_files_is_resolved_from_the_target_file(tmp_path: Path) -> None:
    """Локальная ссылка внутри другого файла разрешается от этого файла, а не от корня."""
    project = make_project(tmp_path / "project", package="multifile_contracts")
    project.write(
        "api/spec.yaml",
        """\
        openapi: "3.0.3"
        info: {title: t, version: "1"}
        paths:
          /err:
            get:
              operationId: err
              responses:
                "400":
                  description: Ошибка
                  content:
                    text/plain:
                      schema:
                        $ref: "./schemas/err.yaml#/Err"
        """,
    )
    project.write(
        "api/schemas/err.yaml",
        """\
        Err:
          $ref: "#/Inner"
        Inner:
          type: object
          properties:
            x: {}
        """,
    )
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit", "root": "api"}},
        operations={"api.err": {"source": "main", "operation_id": "err"}},
    )

    (unchecked,) = project.build().by_key("api.err").unchecked

    assert unchecked.reason.startswith("схема описывает object")
    assert "allow_any" not in unchecked.reason


def test_untyped_array_is_named_as_array(tmp_path: Path) -> None:
    project = build_one(tmp_path, "untypedArray")

    (unchecked,) = project.build().by_key("api.untypedArray").unchecked

    assert unchecked.reason.startswith("схема описывает array")


def test_only_json_neighbours_count_as_twins(tmp_path: Path) -> None:
    """``text/html`` с той же схемой — не JSON-близнец: причина не называет его."""
    project = build_one(tmp_path, "htmlAndPlain")

    reasons = [item.reason for item in project.build().by_key("api.htmlAndPlain").unchecked]

    assert len(reasons) == 2
    assert not any("как и у" in reason for reason in reasons)


def test_allow_any_json_root_stays_any(tmp_path: Path) -> None:
    """``type: string`` получает только корень текстового тела, а не JSON."""
    project = build_one(tmp_path, "anyJson")
    with pytest.raises(UnsupportedConstructError) as info:
        project.build()
    project.write_waivers([_waiver_from_template(str(info.value), issue="QAAUTOSAM-692")])

    document = project.build().by_key("api.anyJson").document

    assert "type" not in document["responses"][0]["schema"]


@pytest.mark.parametrize("operation_id", ["nullableWithFormat", "twoStringProblems"])
def test_probe_rolls_back_waiver_usage_on_both_outcomes(tmp_path: Path, operation_id: str) -> None:
    """Waiver, нужный только пробе, остаётся неиспользованным.

    ``nullableWithFormat``: проба успешна, но строка nullable. ``twoStringProblems``: waiver
    снимает первую проблему, а вторая роняет пробу после того, как waiver был отмечен.
    """
    from geas.errors import WaiverError

    project = pinned_project(
        tmp_path, operation_id, {"responses": [{"status": 200, "content_type": "text/plain"}]}
    )
    with pytest.raises(UnsupportedConstructError) as info:
        project.build()
    project.write_waivers([_waiver_from_template(str(info.value), issue="QAAUTOSAM-692")])
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={f"api.{operation_id}": {"source": "main", "operation_id": operation_id}},
    )

    with pytest.raises(WaiverError, match="больше не нужны"):
        project.build()


def test_response_lookup_by_another_content_type_is_not_called_declared(
    operations: Any,
) -> None:
    """``200: image/png`` не объявлен — у ``200`` есть только ``octet-stream``."""
    with pytest.raises(ResponseVariantError) as info:
        operations.api.download_with_default.response(status=200, content_type="image/png")

    assert "нет варианта ответа 200:image/png" in str(info.value)
    assert "объявлен спецификацией" not in str(info.value)


def test_cli_add_explains_a_bodiless_selector_for_a_status_with_only_unchecked_bodies(
    tmp_path: Path,
) -> None:
    """``--response 200`` — вариант без тела; у ``200`` есть только ``octet-stream``."""
    project = cli_project(tmp_path, ())
    before = project.manifest_bytes()

    result = project.cli(
        "add",
        "api.download",
        "--source",
        "main",
        "--operation-id",
        "downloadWithDefault",
        "--response",
        "200",
    )

    assert result.returncode == 2
    assert "означает вариант без тела" in result.stderr
    assert "--response 200:application/octet-stream" in result.stderr
    assert project.manifest_bytes() == before


# ------------------------------------------------- раунд 6: */*, пробы, add


@pytest.mark.parametrize(
    "fixture, lenient",
    [("text_bodies.yaml", True), ("text_bodies_swagger2.json", False)],
    ids=["openapi30", "swagger2"],
)
def test_wildcard_with_a_broken_schema(tmp_path: Path, fixture: str, lenient: bool) -> None:
    """OpenAPI 3: ``*/*`` раньше не нормализовался — его ошибка у незакреплённого варианта
    уходит в ``unchecked``. Swagger 2.0: ``*/*`` был JSON и раньше — ошибка, как на ``main``."""
    project = make_project(tmp_path / "project", package="wildcard_dirty_contracts")
    project.write_spec(f"api/{fixture}", spec("features", fixture))
    project.write_manifest(
        sources={"main": {"path": f"api/{fixture}", "selection": "explicit"}},
        operations={"api.dirty": {"source": "main", "operation_id": "wildcardDirty"}},
    )

    if not lenient:
        with pytest.raises(UnsupportedConstructError, match="allow_any"):
            project.build()
        return
    (unchecked,) = project.build().by_key("api.dirty").unchecked
    assert unchecked.content_type == "*/*"
    assert unchecked.reason.startswith("схема не разбирается: вариант */* не проверяется")

    project.write_manifest(
        sources={"main": {"path": f"api/{fixture}", "selection": "explicit"}},
        operations={
            "api.dirty": {
                "source": "main",
                "operation_id": "wildcardDirty",
                "responses": [{"status": 200, "content_type": "*/*"}],
            }
        },
    )
    with pytest.raises(UnsupportedConstructError, match="allow_any"):
        project.build()


def test_pinned_text_object_names_the_object_without_a_waiver_recipe(tmp_path: Path) -> None:
    """Waiver на поле объекта не помог бы: вариант всё равно не строка."""
    project = pinned_project(
        tmp_path,
        "objectWithAnyField",
        {"responses": [{"status": 200, "content_type": "text/plain"}]},
    )

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    assert "ответ 200 text/plain — строка, но схема описывает object" in info.value.base_message
    assert "allow_any" not in str(info.value)


@pytest.mark.parametrize(
    "operation_id, direction",
    [("leakyComposition", "response"), ("leakyRequest", "request")],
)
def test_probe_keeps_definitions_of_a_rejected_variant_out_of_the_contract(
    tmp_path: Path, operation_id: str, direction: str
) -> None:
    """``Leaky`` достижим только из отвергнутого ``text/plain``: d42 направления не страдает."""
    project = build_one(tmp_path, operation_id)
    project.update()

    document = project.contract_document(slugify_key(f"api.{operation_id}"))
    (unchecked,) = document["unchecked"]
    assert unchecked["content_type"] == "text/plain"
    assert document["d42"][f"{direction}_module"] is not None
    assert "Leaky" not in json.dumps(document, ensure_ascii=False)


def test_cli_add_keeps_text_variants_secondary_across_statuses(tmp_path: Path) -> None:
    """``200: application/json`` и ``201: text/plain`` — закрепляется JSON, как на ``main``."""
    project = cli_project(tmp_path, ())

    result = project.cli(
        "add", "api.created", "--source", "main", "--operation-id", "createdAsText"
    )

    assert result.returncode == 0, result.stderr
    manifest = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))
    assert manifest["operations"]["api.created"]["responses"] == [
        {"status": 200, "content_type": "application/json"}
    ]

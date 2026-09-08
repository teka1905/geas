"""Runtime: generated namespace, реестр, выбор вариантов и валидация.

Проверяется то, чем пользуется автор теста после ``openapi-contracts update``:
``operations.<ns>.<op>`` отдаёт :class:`OperationHandle`, ручка знает свой ключ,
метод и маршрут, а любая неоднозначность (несколько вариантов ответа, несколько
тел запроса) обязана падать ошибкой, а не «молча брать первый».

Отдельный большой блок — :meth:`OperationHandle.validate_recorded_request`: это
единственное место, где библиотека судит о чужом HTTP-запросе, и цена ошибки тут
максимальна. Проверяются метод, маршрут, query/header/cookie, сериализация
массивов, приведение типов и тело вместе с content type.

Ни один тест не ходит в сеть: ``validate_recorded_request`` вызывается напрямую
с уже разобранными частями запроса. Живой обмен с JJ — в ``test_jj_mock.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from openapi_contracts.errors import (
    ArtifactError,
    OperationLookupError,
    RequestContractError,
    ResponseContractError,
    ResponseVariantError,
)
from support import Project, make_project, spec

#: Синтетический источник с обязательными query-параметром и заголовком: в
#: ``fixtures/specs`` таких нет, а проверить «обязательный отсутствует» надо.
MEMBERS_SPEC = """\
openapi: "3.0.3"

info:
  title: Member directory API
  version: "1.0.0"

paths:
  /members:
    get:
      operationId: listMembers
      parameters:
        - name: workspaceId
          in: query
          required: true
          schema:
            type: string
            format: uuid
        - name: limit
          in: query
          required: false
          schema:
            type: integer
            minimum: 1
            maximum: 100
        - name: X-Api-Key
          in: header
          required: true
          schema:
            type: string
            minLength: 4
      responses:
        "200":
          description: Страница участников
          content:
            application/json:
              schema:
                type: object
                required: [items]
                properties:
                  items:
                    type: array
                    items:
                      type: string
"""

#: Валидный uuid: в спецификации у workspaceId стоит format uuid, а
#: format-checker включён — «просто строка» тут не пройдёт.
WORKSPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
DOCUMENT_ID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

DOCUMENTS_PATH = f"/api/v1/workspaces/{WORKSPACE_ID}/documents"

#: Минимальное валидное тело запроса createDocument.
CREATE_BODY: dict[str, Any] = {"title": "Черновик", "slug": "draft-one", "notifyMembers": True}

#: Минимальный валидный документ из ответа.
DOCUMENT: dict[str, Any] = {"id": DOCUMENT_ID, "title": "Черновик", "slug": "draft-one"}

JSON_HEADERS = [("Content-Type", "application/json")]


def build_project(root: Path, *, package: str) -> Project:
    """Собрать проект-потребитель на четырёх источниках и записать артефакты.

    ``d42`` выключен там, где спецификация содержит типизированный
    ``additionalProperties`` (``DocumentBase.labels``): d42 такое не выражает, и
    генерация d42-схем для этих операций законно падает. Для runtime это не
    важно — JSON Schema работает независимо.
    """
    project = make_project(root, package=package)
    project.write_spec("api/openapi.yaml", spec("basic", "openapi30.yaml"))
    project.write_spec("api/session.yaml", spec("features", "cookie_params.yaml"))
    project.write_spec("api/content.yaml", spec("features", "multi_content_types.yaml"))
    project.write_spec("api/members.yaml", MEMBERS_SPEC)
    project.write_manifest(
        sources={
            "main": {"path": "api/openapi.yaml", "selection": "explicit", "root": "api"},
            "session": {"path": "api/session.yaml", "selection": "explicit", "root": "api"},
            "content": {"path": "api/content.yaml", "selection": "explicit", "root": "api"},
            "members": {"path": "api/members.yaml", "selection": "explicit", "root": "api"},
        },
        operations={
            "api.listDocuments": {
                "source": "main",
                "operation_id": "listDocuments",
                "python_path": ["api", "list_documents"],
                "d42": False,
            },
            "api.createDocument": {
                "source": "main",
                "operation_id": "createDocument",
                "python_path": ["api", "create_document"],
                "d42": False,
            },
            "api.deleteDocument": {
                "source": "main",
                "operation_id": "deleteDocument",
                "python_path": ["api", "delete_document"],
            },
            "session.getSession": {
                "source": "session",
                "operation_id": "getSession",
                "python_path": ["session", "get_session"],
            },
            "content.replaceContent": {
                "source": "content",
                "operation_id": "replaceDocumentContent",
                "python_path": ["content", "replace_content"],
            },
            "members.listMembers": {
                "source": "members",
                "operation_id": "listMembers",
                "python_path": ["members", "list_members"],
            },
        },
    )
    project.update()
    return project


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Импортированный generated-пакет. Один на модуль: сборка дорогая, чтение — нет."""
    project = build_project(tmp_path_factory.mktemp("runtime"), package="runtime_contracts")
    with project.importable() as module:
        yield module


@pytest.fixture(scope="module")
def operations(generated: Any) -> Any:
    """Корень generated namespace."""
    return generated.operations


@pytest.fixture(scope="module")
def registry(generated: Any) -> Any:
    """Реестр операций."""
    return generated.REGISTRY


# --------------------------------------------------------------- namespace


def test_namespace_attribute_returns_handle(operations: Any) -> None:
    """``operations.<ns>.<op>`` отдаёт ручку с координатами из спецификации."""
    handle = operations.api.create_document

    assert handle.key == "api.createDocument"
    assert handle.operation_id == "createDocument"
    assert handle.method == "POST"
    assert handle.path == "/api/v1/workspaces/{workspaceId}/documents"
    assert handle.source == "main"
    assert handle.python_path == ("api", "create_document")


def test_repr_names_key_method_and_path(operations: Any) -> None:
    """``repr`` ручки читаем: по нему видно, какая это операция."""
    assert repr(operations.api.create_document) == (
        "OperationHandle('api.createDocument', POST /api/v1/workspaces/{workspaceId}/documents)"
    )


def test_by_key_returns_the_same_handle(operations: Any) -> None:
    """Строковый доступ и namespace ведут к одному и тому же объекту."""
    assert operations.by_key("api.createDocument") is operations.api.create_document


def test_by_key_rejects_unknown_key(operations: Any) -> None:
    """Неизвестный ключ — ошибка со списком известных, а не ``None``."""
    with pytest.raises(OperationLookupError) as info:
        operations.by_key("api.noSuchOperation")

    assert "api.noSuchOperation" in str(info.value)
    assert "api.createDocument" in str(info.value)


def test_keys_lists_every_operation(operations: Any) -> None:
    """``keys()`` перечисляет все операции manifest в стабильном порядке."""
    keys = operations.keys()

    assert keys == (
        "api.createDocument",
        "api.deleteDocument",
        "api.listDocuments",
        "content.replaceContent",
        "members.listMembers",
        "session.getSession",
    )
    assert keys == tuple(sorted(keys))


def test_registry_by_python_path(registry: Any, operations: Any) -> None:
    """Реестр находит операцию по её пути в generated namespace."""
    assert registry.by_python_path("api", "delete_document") is operations.api.delete_document


def test_registry_by_python_path_rejects_unknown(registry: Any) -> None:
    """Несуществующий python path — ошибка поиска, а не пустой результат."""
    with pytest.raises(OperationLookupError, match=r"api\.no_such_operation"):
        registry.by_python_path("api", "no_such_operation")


def test_registry_len_and_iteration(registry: Any) -> None:
    """Реестр — коллекция: длина и обход отдают все операции."""
    assert len(registry) == 6
    assert [handle.key for handle in registry] == list(registry.keys())


# ----------------------------------------------------- варианты ответа


def test_response_with_single_variant_needs_no_arguments(operations: Any) -> None:
    """Когда вариант один, его не надо называть."""
    variant = operations.members.list_members.response()

    assert variant.status == 200
    assert variant.content_type == "application/json"


def test_response_with_several_variants_requires_choice(operations: Any) -> None:
    """Несколько вариантов — выбор обязателен; первый молча не берётся."""
    handle = operations.api.create_document

    with pytest.raises(ResponseVariantError) as info:
        handle.response()

    message = str(info.value)
    assert "не выбран однозначно" in message
    # В сообщении перечислены оба варианта — значит ни один не был выбран тихо.
    assert "200:application/json" in message
    assert "400:application/json" in message
    assert "api.createDocument" in message


def test_response_selected_by_status(operations: Any) -> None:
    """Явно названный статус снимает неоднозначность."""
    assert operations.api.create_document.response(status=400).status == 400


def test_response_rejects_unknown_status(operations: Any) -> None:
    """Несуществующего варианта нет — и подменять его соседним нельзя."""
    with pytest.raises(ResponseVariantError) as info:
        operations.api.create_document.response(status=503)

    assert "нет варианта ответа 503" in str(info.value)


def test_response_rejects_unknown_content_type(operations: Any) -> None:
    """Content type, которого нет в контракте, — тоже отсутствующий вариант."""
    with pytest.raises(ResponseVariantError, match="application/xml"):
        operations.api.create_document.response(content_type="application/xml")


def test_response_without_body_is_a_variant_too(operations: Any) -> None:
    """204 — полноценный вариант, просто без тела."""
    variant = operations.api.delete_document.response(status=204)

    assert variant.content_type is None
    assert variant.json_schema is None
    assert variant.label() == "204:-"


def test_unsupported_variants_are_reported(operations: Any) -> None:
    """Непредставимый вариант не исчезает молча — он назван в ``unsupported``."""
    handle = operations.content.replace_content

    assert [variant.label() for variant in handle.responses] == ["200:application/json"]
    assert any("application/pdf" in reason for reason in handle.unsupported)


# ------------------------------------------------------- тела запроса


def test_request_body_with_single_variant_needs_no_arguments(operations: Any) -> None:
    """Одно тело — content type не обязателен."""
    body = operations.api.create_document.request.body()

    assert body.content_type == "application/json"
    assert body.required is True


def test_request_body_with_several_variants_requires_choice(operations: Any) -> None:
    """Несколько тел — выбор обязателен, первый молча не берётся."""
    with pytest.raises(ResponseVariantError) as info:
        operations.content.replace_content.request.body()

    message = str(info.value)
    assert "несколько тел запроса" in message
    assert "application/json" in message
    assert "application/merge-patch+json" in message


def test_request_body_selected_by_content_type(operations: Any) -> None:
    """Явный content type выбирает нужное тело."""
    body = operations.content.replace_content.request.body("application/merge-patch+json")

    assert body.content_type == "application/merge-patch+json"


def test_request_body_rejects_unknown_content_type(operations: Any) -> None:
    """Тела с чужим content type нет — ошибка со списком доступных."""
    with pytest.raises(ResponseVariantError, match="application/xml"):
        operations.content.replace_content.request.body("application/xml")


def test_request_body_absent_is_an_error(operations: Any) -> None:
    """У операции без тела запрашивать тело нечего."""
    with pytest.raises(ResponseVariantError, match="нет тела запроса"):
        operations.api.delete_document.request.body()


# --------------------------------------------------- валидация ответа


def test_validate_response_accepts_valid_payload(operations: Any) -> None:
    """Валидное тело проходит и возвращает выбранный вариант."""
    variant = operations.api.create_document.validate_response(DOCUMENT, status=200)

    assert variant.label() == "200:application/json"


def test_validate_response_rejects_invalid_payload(operations: Any) -> None:
    """Невалидное тело отклоняется с точным указателем внутрь значения."""
    payload = {**DOCUMENT, "owner": {"id": DOCUMENT_ID, "email": "не-адрес"}}

    with pytest.raises(ResponseContractError) as info:
        operations.api.create_document.validate_response(payload, status=200)

    error = info.value
    assert error.json_pointer == "/owner/email"
    assert error.validator == "format"
    assert error.direction == "response"
    assert error.operation_key == "api.createDocument"


def test_validate_response_reports_missing_required_property(operations: Any) -> None:
    """Отсутствие обязательного поля — ошибка в корне со списком required."""
    with pytest.raises(ResponseContractError) as info:
        operations.members.list_members.validate_response({})

    assert info.value.json_pointer == "/"
    assert info.value.validator == "required"


def test_validate_response_rejects_body_for_bodiless_variant(operations: Any) -> None:
    """У 204 тела нет: переданное тело — ошибка, а не «ну и ладно»."""
    with pytest.raises(ResponseVariantError, match="описан без тела"):
        operations.api.delete_document.validate_response({"code": "conflict"}, status=204)


def test_validate_response_requires_body_where_contract_has_one(operations: Any) -> None:
    """И наоборот: вариант с телом не принимает ``None``."""
    with pytest.raises(ResponseVariantError, match="требует тело"):
        operations.api.delete_document.validate_response(None, status=400)


# --------------------------------------------------- валидация тела запроса


def test_validate_request_body_accepts_valid_payload(operations: Any) -> None:
    """Валидное тело запроса проходит."""
    operations.api.create_document.validate_request_body(CREATE_BODY)


def test_validate_request_body_rejects_invalid_payload(operations: Any) -> None:
    """Невалидное тело отклоняется как ошибка направления request."""
    with pytest.raises(RequestContractError) as info:
        operations.api.create_document.validate_request_body({**CREATE_BODY, "slug": "Не Слаг"})

    error = info.value
    assert error.json_pointer == "/slug"
    assert error.validator == "pattern"
    assert error.direction == "request"


def test_validate_request_body_requires_content_type_when_ambiguous(operations: Any) -> None:
    """Несколько тел — валидация тоже требует явного content type."""
    with pytest.raises(ResponseVariantError, match="несколько тел запроса"):
        operations.content.replace_content.validate_request_body({"body": "текст"})


# ------------------------------------------- validate_recorded_request: маршрут


def members_request(**overrides: Any) -> dict[str, Any]:
    """Шаблон корректного перехваченного запроса к ``members.listMembers``."""
    request: dict[str, Any] = {
        "method": "GET",
        "path": "/members",
        "segments": {},
        "params": [("workspaceId", WORKSPACE_ID)],
        "headers": [("X-Api-Key", "s3cret")],
        "body": None,
        "index": 0,
    }
    request.update(overrides)
    return request


def test_recorded_request_accepts_valid_request(operations: Any) -> None:
    """Эталонный запрос проходит — иначе все отрицательные проверки бессмысленны."""
    operations.members.list_members.validate_recorded_request(**members_request())


def test_recorded_request_rejects_wrong_method(operations: Any) -> None:
    """Метод сверяется с контрактом."""
    with pytest.raises(RequestContractError) as info:
        operations.members.list_members.validate_recorded_request(**members_request(method="POST"))

    error = info.value
    assert error.json_pointer == "/method"
    assert error.expected == "GET"
    assert error.actual == "POST"
    assert error.request_index == 0


def test_recorded_request_rejects_wrong_route(operations: Any) -> None:
    """Чужой маршрут не выдаётся за свой."""
    with pytest.raises(RequestContractError) as info:
        operations.members.list_members.validate_recorded_request(
            **members_request(path="/members/42")
        )

    assert info.value.json_pointer == "/path"
    assert info.value.actual == "/members/42"


def test_recorded_request_checks_path_parameter_schema(operations: Any) -> None:
    """Path-параметр проверяется по своей схеме, а не только по форме маршрута."""
    with pytest.raises(RequestContractError) as info:
        operations.api.list_documents.validate_recorded_request(
            method="GET",
            path="/api/v1/workspaces/not-a-uuid/documents",
            segments={},
            params=[],
            headers=[],
            body=None,
            index=0,
        )

    assert "workspaceId" in str(info.value)
    assert info.value.validator == "format"


# --------------------------------------- validate_recorded_request: параметры


def test_recorded_request_requires_mandatory_query_param(operations: Any) -> None:
    """Обязательный query-параметр отсутствует — это ошибка контракта."""
    with pytest.raises(RequestContractError) as info:
        operations.members.list_members.validate_recorded_request(**members_request(params=[]))

    error = info.value
    assert error.json_pointer == "/query/workspaceId"
    assert error.actual == "отсутствует"


def test_recorded_request_rejects_uncoercible_query_param(operations: Any) -> None:
    """``limit=abc`` при ``type: integer`` — не «ну пусть строка», а ошибка."""
    params = [("workspaceId", WORKSPACE_ID), ("limit", "abc")]

    with pytest.raises(RequestContractError) as info:
        operations.members.list_members.validate_recorded_request(**members_request(params=params))

    error = info.value
    assert error.json_pointer == "/limit"
    assert error.expected == "целое число в десятичной записи"
    assert error.actual == "'abc'"


def test_recorded_request_accepts_coercible_query_param(operations: Any) -> None:
    """Строка, которая приводится к объявленному типу, проходит."""
    params = [("workspaceId", WORKSPACE_ID), ("limit", "50")]

    operations.members.list_members.validate_recorded_request(**members_request(params=params))


def test_recorded_request_checks_query_param_bounds(operations: Any) -> None:
    """После приведения значение проверяется по схеме: 0 меньше минимума."""
    params = [("workspaceId", WORKSPACE_ID), ("limit", "0")]

    with pytest.raises(RequestContractError) as info:
        operations.members.list_members.validate_recorded_request(**members_request(params=params))

    assert info.value.validator == "minimum"


def test_recorded_request_accepts_exploded_array_repeated(operations: Any) -> None:
    """``explode: true`` — массив приходит повторяющимся ключом."""
    operations.api.list_documents.validate_recorded_request(
        method="GET",
        path=DOCUMENTS_PATH,
        segments={},
        params=[("status", "draft"), ("status", "review")],
        headers=[],
        body=None,
        index=0,
    )


def test_recorded_request_rejects_repeated_non_exploded_array(operations: Any) -> None:
    """``explode: false`` — элементы через запятую; повтор ключа ломает сериализацию."""
    with pytest.raises(RequestContractError) as info:
        operations.api.list_documents.validate_recorded_request(
            method="GET",
            path=DOCUMENTS_PATH,
            segments={},
            params=[("tags", "alpha"), ("tags", "beta")],
            headers=[],
            body=None,
            index=3,
        )

    error = info.value
    assert error.json_pointer == "/tags"
    assert "explode=false" in str(error)
    assert error.request_index == 3
    assert str(error).startswith("request #3: ")


def test_recorded_request_accepts_comma_separated_array(operations: Any) -> None:
    """Та же ``tags``, но сериализованная правильно, проходит."""
    operations.api.list_documents.validate_recorded_request(
        method="GET",
        path=DOCUMENTS_PATH,
        segments={},
        params=[("tags", "alpha,beta")],
        headers=[],
        body=None,
        index=0,
    )


def test_recorded_request_checks_array_items(operations: Any) -> None:
    """Элементы массива проверяются поштучно — enum здесь не декоративный."""
    with pytest.raises(RequestContractError) as info:
        operations.api.list_documents.validate_recorded_request(
            method="GET",
            path=DOCUMENTS_PATH,
            segments={},
            params=[("status", "archived")],
            headers=[],
            body=None,
            index=0,
        )

    assert info.value.validator == "enum"


def test_recorded_request_requires_mandatory_header(operations: Any) -> None:
    """Обязательный заголовок отсутствует — ошибка с указателем на него."""
    with pytest.raises(RequestContractError) as info:
        operations.members.list_members.validate_recorded_request(**members_request(headers=[]))

    assert info.value.json_pointer == "/header/X-Api-Key"


def test_recorded_request_matches_header_case_insensitively(operations: Any) -> None:
    """Имя заголовка регистронезависимо — HTTP так и работает."""
    operations.members.list_members.validate_recorded_request(
        **members_request(headers=[("x-api-key", "s3cret")])
    )


def test_recorded_request_checks_header_schema(operations: Any) -> None:
    """Значение заголовка проверяется по схеме."""
    with pytest.raises(RequestContractError) as info:
        operations.members.list_members.validate_recorded_request(
            **members_request(headers=[("X-Api-Key", "ab")])
        )

    assert info.value.validator == "minLength"


def test_recorded_request_reads_cookie_parameter(operations: Any) -> None:
    """Cookie-параметр достаётся из заголовка ``Cookie`` и проверяется по схеме."""
    operations.session.get_session.validate_recorded_request(
        method="GET",
        path="/session",
        segments={},
        params=[],
        headers=[("Cookie", "theme=dark; session_id=abcdefgh")],
        body=None,
        index=0,
    )


def test_recorded_request_requires_mandatory_cookie(operations: Any) -> None:
    """Обязательная cookie отсутствует — ошибка с указателем ``/cookie/<имя>``."""
    with pytest.raises(RequestContractError) as info:
        operations.session.get_session.validate_recorded_request(
            method="GET",
            path="/session",
            segments={},
            params=[],
            headers=[("Cookie", "theme=dark")],
            body=None,
            index=0,
        )

    assert info.value.json_pointer == "/cookie/session_id"


def test_recorded_request_checks_cookie_schema(operations: Any) -> None:
    """Значение cookie тоже проверяется по схеме, а не просто «есть»."""
    with pytest.raises(RequestContractError) as info:
        operations.session.get_session.validate_recorded_request(
            method="GET",
            path="/session",
            segments={},
            params=[],
            headers=[("Cookie", "session_id=short")],
            body=None,
            index=0,
        )

    assert info.value.validator == "minLength"


# ----------------------------------------- validate_recorded_request: тело


def create_request(**overrides: Any) -> dict[str, Any]:
    """Шаблон корректного перехваченного запроса к ``api.createDocument``."""
    request: dict[str, Any] = {
        "method": "POST",
        "path": DOCUMENTS_PATH,
        "segments": {},
        "params": [],
        "headers": list(JSON_HEADERS),
        "body": dict(CREATE_BODY),
        "index": 0,
    }
    request.update(overrides)
    return request


def test_recorded_request_accepts_valid_body(operations: Any) -> None:
    """Эталонное тело проходит."""
    operations.api.create_document.validate_recorded_request(**create_request())


def test_recorded_request_rejects_body_with_wrong_content_type(operations: Any) -> None:
    """Content type, которого нет в контракте, — ошибка с указателем на заголовок."""
    with pytest.raises(RequestContractError) as info:
        operations.api.create_document.validate_recorded_request(
            **create_request(headers=[("Content-Type", "application/xml")])
        )

    error = info.value
    assert error.json_pointer == "/header/Content-Type"
    assert error.actual == "application/xml"


def test_recorded_request_ignores_content_type_parameters(operations: Any) -> None:
    """``;charset=utf-8`` и регистр не мешают найти контракт тела."""
    operations.api.create_document.validate_recorded_request(
        **create_request(headers=[("Content-Type", "Application/JSON; charset=utf-8")])
    )


def test_recorded_request_requires_content_type_for_body(operations: Any) -> None:
    """Тело есть, а заголовка нет — не понять, каким контрактом судить."""
    with pytest.raises(RequestContractError) as info:
        operations.api.create_document.validate_recorded_request(**create_request(headers=[]))

    assert info.value.json_pointer == "/header/Content-Type"
    assert info.value.actual == "отсутствует"


def test_recorded_request_requires_body_when_contract_demands_it(operations: Any) -> None:
    """Обязательное тело не пришло — ошибка, а не пропуск проверки."""
    with pytest.raises(RequestContractError) as info:
        operations.api.create_document.validate_recorded_request(
            **create_request(body=None, headers=[])
        )

    assert info.value.json_pointer == "/body"
    assert info.value.actual == "пусто"


def test_recorded_request_rejects_non_json_body(operations: Any) -> None:
    """Сырое тело, которое не разбирается как JSON, — ошибка контракта."""
    with pytest.raises(RequestContractError) as info:
        operations.api.create_document.validate_recorded_request(
            **create_request(body=b"<xml/>", raw_body=b"<xml/>")
        )

    error = info.value
    assert error.json_pointer == "/body"
    assert "не разбирается как JSON" in str(error)
    assert error.expected == "валидный JSON"


def test_recorded_request_parses_raw_json_body(operations: Any) -> None:
    """Сырое валидное JSON-тело разбирается и проверяется."""
    operations.api.create_document.validate_recorded_request(
        **create_request(body=json.dumps(CREATE_BODY).encode("utf-8"))
    )


def test_recorded_request_rejects_invalid_body(operations: Any) -> None:
    """Тело, нарушающее схему, отклоняется с указателем внутрь тела."""
    with pytest.raises(RequestContractError) as info:
        operations.api.create_document.validate_recorded_request(
            **create_request(body={"title": "Черновик"})
        )

    error = info.value
    assert error.json_pointer == "/"
    assert error.validator == "required"
    assert "notifyMembers" in str(error)


def test_read_only_field_is_cut_from_request_contract(operations: Any) -> None:
    """``readOnly`` вырезается из направления request, но остаётся в response.

    Это ровно то, что говорит фикстура: ``DocumentBase.createdAt`` помечен
    ``readOnly``, поэтому в теле запроса его нет, а в теле ответа — есть.
    """
    request_schema = operations.api.create_document.request.body().json_schema
    response_schema = operations.api.create_document.response(status=200).json_schema

    assert "createdAt" not in request_schema["$defs"]["DocumentCreate"]["properties"]
    assert "createdAt" in response_schema["$defs"]["Document"]["properties"]


def test_read_only_field_sent_in_request_is_tolerated(operations: Any) -> None:
    """``readOnly``-поле в запросе проходит — и это не недосмотр теста.

    Схема ``DocumentCreate`` в фикстуре **не** объявляет
    ``additionalProperties: false``, поэтому по JSON Schema лишний ключ законен.
    Библиотека не имеет права ужесточать контракт по собственному желанию, так
    что фиксируется настоящее поведение, а не желаемое.
    """
    body = {**CREATE_BODY, "createdAt": "2024-01-01T00:00:00Z"}

    operations.api.create_document.validate_recorded_request(**create_request(body=body))


def test_closed_object_still_rejects_unknown_property(operations: Any) -> None:
    """Там, где спецификация закрыла объект, лишний ключ отклоняется.

    Пара к предыдущему тесту: терпимость к ``createdAt`` — следствие открытой
    схемы, а не отключённой проверки. ``Member`` объявлен с
    ``additionalProperties: false``, и лишний ключ в нём — ошибка.
    """
    owner = {"id": DOCUMENT_ID, "email": "member@example.test", "unexpected": 1}

    with pytest.raises(RequestContractError) as info:
        operations.api.create_document.validate_recorded_request(
            **create_request(body={**CREATE_BODY, "owner": owner})
        )

    error = info.value
    assert error.json_pointer == "/owner"
    assert error.validator == "additionalProperties"


def test_recorded_request_uses_segments_when_route_is_pinned(operations: Any) -> None:
    """Значения шаблонных сегментов, отданные моком, дополняют разбор маршрута."""
    operations.api.delete_document.validate_recorded_request(
        method="DELETE",
        path=f"/api/v1/workspaces/{WORKSPACE_ID}/documents/{DOCUMENT_ID}",
        segments={"documentId": DOCUMENT_ID},
        params=[],
        headers=[],
        body=None,
        index=0,
    )


def test_recorded_request_rejects_path_param_other_than_pinned(operations: Any) -> None:
    """Закреплённый в моке path-параметр обязан совпасть с пришедшим."""
    other = "11111111-2222-3333-4444-555555555555"

    with pytest.raises(RequestContractError) as info:
        operations.api.list_documents.validate_recorded_request(
            method="GET",
            path=f"/api/v1/workspaces/{other}/documents",
            segments={},
            params=[],
            headers=[],
            body=None,
            index=0,
            pinned_path_params={"workspaceId": WORKSPACE_ID},
        )

    error = info.value
    assert error.json_pointer == "/path/workspaceId"
    assert error.expected == WORKSPACE_ID
    assert error.actual == other


# ------------------------------------------------ версия документа контракта


def test_contract_document_version_is_guarded(tmp_path: Path) -> None:
    """Документ контракта чужой версии не читается «как-нибудь».

    Формат документа — часть контракта между генератором и runtime. Если файл
    сгенерирован другой версией библиотеки, единственный честный ответ —
    отказаться и попросить перегенерировать.
    """
    project = build_project(tmp_path / "guarded", package="guarded_contracts")
    contract_path = project.output_dir / "contracts" / "api__create_document.json"
    document = json.loads(contract_path.read_text(encoding="utf-8"))
    document["artifact"]["version"] = 999
    contract_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with project.importable() as module, pytest.raises(ArtifactError) as info:
        module.operations.api.create_document  # noqa: B018 - обращение и есть действие

    message = str(info.value)
    assert "999" in message
    assert "openapi-contracts update" in message


def test_missing_contract_document_is_reported(tmp_path: Path) -> None:
    """Пропавший файл контракта — понятная ошибка артефактов, а не ``FileNotFoundError``."""
    project = build_project(tmp_path / "incomplete", package="incomplete_contracts")
    (project.output_dir / "contracts" / "api__create_document.json").unlink()

    with project.importable() as module, pytest.raises(ArtifactError) as info:
        module.operations.api.create_document  # noqa: B018 - обращение и есть действие

    assert "api__create_document.json" in str(info.value)

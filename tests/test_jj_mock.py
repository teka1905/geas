"""JJ-интеграция целиком: настоящий мок-сервер, настоящий HTTP-клиент.

Здесь ничего не подменяется: на loopback поднимается ``python -m jj``
(см. ``tests/jj_server.py``), ``ContractMock`` регистрирует в нём мок, а
``aiohttp`` ходит к нему как обычный клиент. Проверяется не то, «как мы думаем,
устроен JJ», а то, что получается на живом обмене.

Порядок жизненного цикла, который здесь закрепляется:

* тело ответа проверяется **до** регистрации мока — иначе тест успевает
  получить от мока заведомо неконтрактный ответ и падает не там;
* перехваченные запросы проверяются **на выходе** из блока, когда история уже
  забрана;
* исключение из тела сценария остаётся первичным, а диагностика контракта
  прикладывается к нему заметкой и остаётся в ``mock.diagnostics``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from jj_server import JJServer, jj_server  # noqa: F401 - сессионная фикстура
from openapi_contracts.errors import (
    ContractMockError,
    RequestContractError,
    ResponseContractError,
    ResponseVariantError,
)
from support import Project, make_project, spec

#: Источник с path-параметром — обычной строкой, без ``format: uuid``.
#: Нужен, чтобы проверить защиту от ``/`` в значении: значение со слэшем обязано
#: пройти схему параметра и упереться именно в проверку маршрута.
LABELS_SPEC = """\
openapi: "3.0.3"

info:
  title: Label API
  version: "1.0.0"

paths:
  /labels/{labelName}:
    delete:
      operationId: deleteLabel
      parameters:
        - name: labelName
          in: path
          required: true
          schema:
            type: string
            minLength: 1
      responses:
        "204":
          description: Метка удалена
"""

WORKSPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
DOCUMENT_ID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

DOCUMENTS_PATH = f"/api/v1/workspaces/{WORKSPACE_ID}/documents"

#: Валидное тело запроса createDocument.
CREATE_BODY: dict[str, Any] = {"title": "Черновик", "slug": "draft-one", "notifyMembers": True}

#: Валидный документ из ответа.
DOCUMENT: dict[str, Any] = {"id": DOCUMENT_ID, "title": "Черновик", "slug": "draft-one"}

#: Валидная страница документов.
PAGE: dict[str, Any] = {"items": [DOCUMENT], "total": 1}


class ScenarioFailedError(Exception):
    """Исключение «из тела теста» — им проверяется приоритет первичной ошибки."""


def build_project(root: Path, *, package: str) -> Project:
    """Собрать проект-потребитель и записать артефакты."""
    project = make_project(root, package=package)
    project.write_spec("api/openapi.yaml", spec("basic", "openapi30.yaml"))
    project.write_spec("api/labels.yaml", LABELS_SPEC)
    project.write_manifest(
        sources={
            "main": {"path": "api/openapi.yaml", "selection": "explicit", "root": "api"},
            "labels": {"path": "api/labels.yaml", "selection": "explicit", "root": "api"},
        },
        operations={
            # d42 выключен там, где спецификация использует типизированный
            # additionalProperties: d42 такое не выражает. JSON Schema — работает.
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
            "labels.deleteLabel": {
                "source": "labels",
                "operation_id": "deleteLabel",
                "python_path": ["labels", "delete_label"],
            },
        },
    )
    project.update()
    return project


@pytest.fixture(scope="module")
def operations(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Generated namespace проекта-потребителя."""
    project = build_project(tmp_path_factory.mktemp("jj"), package="jj_contracts")
    with project.importable() as module:
        yield module.operations


async def send(
    server: JJServer,
    method: str,
    path: str,
    **kwargs: Any,
) -> tuple[int, str, Any]:
    """Сходить к мок-серверу и вернуть статус, content type и разобранное тело."""
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, f"{server.url}{path}", **kwargs) as response,
    ):
        raw = await response.read()
        return response.status, response.headers.get("Content-Type", ""), raw


async def send_json(server: JJServer, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
    """То же, но с разбором JSON-тела ответа."""
    import json

    status, _, raw = await send(server, method, path, **kwargs)
    return status, json.loads(raw) if raw else None


# ---------------------------------------------------------------- happy path


async def test_mock_answers_with_contract_response(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Клиент получает ровно то, что положено в мок: статус, content type и тело."""
    handle = operations.api.create_document

    async with handle.mock(
        response=DOCUMENT,
        status=200,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    ) as mock:
        # До выхода из блока история ещё не забрана — это часть контракта класса.
        assert mock.history == ()
        status, content_type, raw = await send(jj_server, "POST", DOCUMENTS_PATH, json=CREATE_BODY)

    assert status == 200
    assert content_type.split(";")[0] == "application/json"
    assert raw.decode("utf-8") is not None
    assert mock.status == 200
    assert mock.operation is handle
    assert mock.diagnostics == ()

    assert len(mock.history) == 1
    request = mock.requests[0]
    assert request.method == "POST"
    assert request.path == DOCUMENTS_PATH
    assert request.body == CREATE_BODY


async def test_mock_records_query_parameters(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Повторяющиеся query-параметры доходят до истории как есть, без склейки."""
    async with operations.api.list_documents.mock(
        response=PAGE,
        status=200,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    ) as mock:
        status, body = await send_json(
            jj_server,
            "GET",
            f"{DOCUMENTS_PATH}?status=draft&status=review&tags=alpha,beta&limit=25",
        )

    assert (status, body) == (200, PAGE)
    params = list(mock.requests[0].params.items())
    assert ("status", "draft") in params
    assert ("status", "review") in params
    assert ("tags", "alpha,beta") in params
    assert ("limit", "25") in params


async def test_thin_wrapper_keeps_migration_compatible_api(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Тонкая обёртка вида ``mocked_*`` работает как обычный мок.

    Проекты, приезжающие на библиотеку с самописных моков, оборачивают
    ``handle.mock(...)`` функцией с привычным именем и сигнатурой. Никакой магии
    для этого не нужно — ``ContractMock`` уже полноценный async-контекст.
    """

    def mocked_create_document(body: Any, wait_for_requests: int = 1) -> Any:
        return operations.api.create_document.mock(
            response=body,
            status=200,
            path_params={"workspaceId": WORKSPACE_ID},
            wait_for_requests=wait_for_requests,
        )

    async with mocked_create_document(DOCUMENT) as mock:
        status, body = await send_json(jj_server, "POST", DOCUMENTS_PATH, json=CREATE_BODY)

    assert (status, body) == (200, DOCUMENT)
    assert len(mock.history) == 1


# ------------------------------------------- ответ проверяется до регистрации


async def test_invalid_response_is_rejected_before_registration(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Неконтрактный ответ падает на создании мока — регистрации не происходит.

    Порядок здесь принципиален. Если бы мок сначала регистрировался, тест успел
    бы получить от него заведомо неверный ответ и упал бы где-то в прикладном
    коде, а не на строчке с ``mock(...)``.
    """
    with pytest.raises(ResponseContractError) as info:
        operations.api.create_document.mock(
            response={"title": "без id и slug"},
            status=200,
            path_params={"workspaceId": WORKSPACE_ID},
        )

    assert info.value.validator == "required"

    # Сервер о таком моке не знает: запрос никуда не попадает.
    status, _, _ = await send(jj_server, "POST", DOCUMENTS_PATH, json=CREATE_BODY)
    assert status == 404


async def test_ambiguous_variant_requires_explicit_status(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """У операции два варианта ответа — мок обязан спросить, какой имелся в виду."""
    with pytest.raises(ResponseVariantError) as info:
        operations.api.create_document.mock(
            response=DOCUMENT,
            path_params={"workspaceId": WORKSPACE_ID},
        )

    message = str(info.value)
    assert "200:application/json" in message
    assert "400:application/json" in message


async def test_error_variant_can_be_mocked(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Отрицательный вариант мокается так же, как положительный."""
    error_body = {"code": "invalid_request", "message": "плохой запрос"}

    async with operations.api.create_document.mock(
        response=error_body,
        status=400,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    ) as mock:
        status, body = await send_json(jj_server, "POST", DOCUMENTS_PATH, json=CREATE_BODY)

    assert (status, body) == (400, error_body)
    assert mock.diagnostics == ()


# --------------------------------------------------------- вариант без тела


async def test_bodiless_variant_answers_without_body(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """204 отдаётся без тела: ``null`` в ответе появиться не должен."""
    path = f"{DOCUMENTS_PATH}/{DOCUMENT_ID}"

    async with operations.api.delete_document.mock(
        response=None,
        status=204,
        path_params={"workspaceId": WORKSPACE_ID, "documentId": DOCUMENT_ID},
        wait_for_requests=1,
    ) as mock:
        status, _, raw = await send(jj_server, "DELETE", path)

    assert status == 204
    assert raw == b""
    assert len(mock.history) == 1


async def test_bodiless_variant_rejects_body(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Тело для варианта без тела — ошибка, а не «положим на всякий случай»."""
    with pytest.raises(ResponseVariantError, match="описан без тела"):
        operations.api.delete_document.mock(
            response={"code": "conflict", "message": "нельзя"},
            status=204,
            path_params={"workspaceId": WORKSPACE_ID, "documentId": DOCUMENT_ID},
        )


# ------------------------------------------- запрос проверяется на выходе


async def test_request_is_validated_on_exit(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Неконтрактный запрос клиента поднимается из ``__aexit__`` с индексом и указателем."""
    mock = operations.api.create_document.mock(
        response=DOCUMENT,
        status=200,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    )

    with pytest.raises(RequestContractError) as info:
        async with mock:
            status, _ = await send_json(
                jj_server, "POST", DOCUMENTS_PATH, json={"title": "Черновик"}
            )
            # Мок ответил как обещал: контракт нарушил клиент, а не мок.
            assert status == 200

    error = info.value
    assert error.request_index == 0
    assert error.json_pointer == "/"
    assert error.validator == "required"
    assert "notifyMembers" in str(error)
    assert len(mock.history) == 1


async def test_wrong_content_type_of_request_is_reported(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Тело, отправленное не тем content type, ловится на выходе из блока."""
    mock = operations.api.create_document.mock(
        response=DOCUMENT,
        status=200,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    )

    with pytest.raises(RequestContractError) as info:
        async with mock:
            await send(
                jj_server,
                "POST",
                DOCUMENTS_PATH,
                data=b"title=x",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    assert info.value.json_pointer == "/header/Content-Type"


# --------------------------------------------- приоритет первичной ошибки


async def test_scenario_exception_stays_primary(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Исключение из тела блока — первичное; контрактная ошибка идёт заметкой.

    Иначе разбор падения начинается не с того места: тест упал по своему
    ассерту, а наружу вылезла бы ошибка контракта, случившаяся позже.
    """
    mock = operations.api.create_document.mock(
        response=DOCUMENT,
        status=200,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    )

    with pytest.raises(ScenarioFailedError) as info:
        async with mock:
            await send(jj_server, "POST", DOCUMENTS_PATH, json={"title": "Черновик"})
            raise ScenarioFailedError("ассерт сценария")

    # Cleanup всё равно отработал: история забрана, мок снят.
    assert len(mock.history) == 1
    assert [type(item).__name__ for item in mock.diagnostics] == ["RequestContractError"]

    notes = getattr(info.value, "__notes__", [])
    assert any("RequestContractError" in note for note in notes)
    assert any("notifyMembers" in note for note in notes)


# ----------------------------------------------------- параллельные моки


async def test_two_mocks_work_independently(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Два мока на разные операции не мешают друг другу и не смешивают историю."""
    async with (
        operations.api.list_documents.mock(
            response=PAGE,
            status=200,
            path_params={"workspaceId": WORKSPACE_ID},
            wait_for_requests=1,
        ) as listing,
        operations.api.create_document.mock(
            response=DOCUMENT,
            status=200,
            path_params={"workspaceId": WORKSPACE_ID},
            wait_for_requests=1,
        ) as creation,
    ):
        assert await send_json(jj_server, "GET", DOCUMENTS_PATH) == (200, PAGE)
        assert await send_json(jj_server, "POST", DOCUMENTS_PATH, json=CREATE_BODY) == (
            200,
            DOCUMENT,
        )

    assert [item.method for item in listing.requests] == ["GET"]
    assert [item.method for item in creation.requests] == ["POST"]
    assert listing.diagnostics == ()
    assert creation.diagnostics == ()


async def test_each_mock_validates_only_its_own_requests(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Нарушение контракта одной операции не пачкает соседний мок."""
    listing = operations.api.list_documents.mock(
        response=PAGE,
        status=200,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    )
    creation = operations.api.create_document.mock(
        response=DOCUMENT,
        status=200,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    )

    with pytest.raises(RequestContractError) as info:
        async with listing:
            async with creation:
                await send_json(jj_server, "GET", DOCUMENTS_PATH)
                await send_json(jj_server, "POST", DOCUMENTS_PATH, json={"title": "Черновик"})

    assert info.value.operation_key == "api.createDocument"
    assert listing.diagnostics == ()
    assert len(listing.history) == 1


# ----------------------------------------------------- wait_for_requests


async def test_missing_requests_are_reported(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Запросов пришло меньше, чем ждали, — это ошибка, а не тихое ожидание."""
    mock = operations.api.create_document.mock(
        response=DOCUMENT,
        status=200,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=2,
        timeout=0.5,
    )

    with pytest.raises(ContractMockError) as info:
        async with mock:
            await send_json(jj_server, "POST", DOCUMENTS_PATH, json=CREATE_BODY)

    message = str(info.value)
    assert "ожидалось минимум 2" in message
    assert "перехвачено 1" in message
    # История всё равно доступна: по ней видно, что реально успело прийти.
    assert len(mock.history) == 1


# --------------------------------------------------------- path_params


async def test_unknown_path_param_is_rejected(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Опечатка в имени path-параметра ловится сразу, а не молчаливым несовпадением."""
    with pytest.raises(ContractMockError) as info:
        operations.api.create_document.mock(
            response=DOCUMENT,
            status=200,
            path_params={"workspace_id": WORKSPACE_ID},
        )

    message = str(info.value)
    assert "workspace_id" in message
    assert "workspaceId" in message


async def test_path_param_is_validated_against_its_schema(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Значение path-параметра проверяется по схеме параметра."""
    with pytest.raises(RequestContractError) as info:
        operations.api.create_document.mock(
            response=DOCUMENT,
            status=200,
            path_params={"workspaceId": "не-uuid"},
        )

    assert info.value.validator == "format"
    assert "path_params['workspaceId']" in str(info.value)


async def test_path_param_with_slash_is_rejected(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Слэш в значении разбил бы маршрут на лишние сегменты — это ошибка использования."""
    mock = operations.labels.delete_label.mock(response=None, path_params={"labelName": "a/b"})

    with pytest.raises(ContractMockError, match="разбил бы маршрут"):
        async with mock:
            pass

    # Мок не зарегистрирован: сервер о маршруте не знает.
    status, _, _ = await send(jj_server, "DELETE", "/labels/a/b")
    assert status == 404


async def test_path_param_is_substituted_into_route(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Закреплённое значение подставляется в маршрут и сужает мок до него."""
    async with operations.labels.delete_label.mock(
        response=None,
        path_params={"labelName": "urgent"},
        wait_for_requests=1,
    ) as mock:
        assert (await send(jj_server, "DELETE", "/labels/urgent"))[0] == 204
        # Другая метка тем же моком не перехватывается.
        assert (await send(jj_server, "DELETE", "/labels/other"))[0] == 404

    assert [item.path for item in mock.requests] == ["/labels/urgent"]


async def test_repr_survives_unusable_path_param(operations: Any) -> None:
    """``repr`` не имеет права падать: иначе pytest покажет не ту ошибку.

    Значение со слэшем — законный повод отказаться регистрировать мок, но
    ``repr`` вызывается отладчиком и самим pytest при разборе падения. Если он
    бросит исключение, настоящая причина потеряется.
    """
    mock = operations.labels.delete_label.mock(response=None, path_params={"labelName": "a/b"})

    text = repr(mock)

    assert "labels.deleteLabel" in text
    assert "DELETE" in text


# ------------------------------------------------------- повторный вход


async def test_mock_cannot_be_reentered(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    """Один ``ContractMock`` — один блок: повторный вход перетёр бы историю."""
    mock = operations.api.create_document.mock(
        response=DOCUMENT,
        status=200,
        path_params={"workspaceId": WORKSPACE_ID},
    )

    async with mock:
        pass

    with pytest.raises(ContractMockError, match="повторно"):
        async with mock:
            pass

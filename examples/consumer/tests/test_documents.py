"""Пример-потребитель: как выглядит тест, написанный поверх generated-контрактов.

Здесь показаны все пять вещей, ради которых библиотека существует:

1. точка входа — ``from app_contracts.generated import operations``: статический
   namespace с автодополнением, без строковых ключей в теле теста;
2. канонический вид мока —
   ``async with operations.api.list_documents.mock(...) as mock``;
3. тонкая обёртка ``mocked_*`` — для проектов, которые переезжают со старого
   стиля и не хотят переписывать все тесты разом;
4. :func:`overlay_generators` с маркером :data:`EACH` — подмена генератора
   одного листа внутри массива, структура и контракт остаются сгенерированными;
5. ассерты по ``mock.history`` — количество вызовов проверяет тест, библиотека
   этого за него не делает.

Запуск (нужен поднятый локально мок-сервер JJ)::

    python -m jj -H 127.0.0.1 -p 8080 &
    JJ_REMOTE_MOCK_URL=http://127.0.0.1:8080 pytest examples/consumer/tests

Всё, что импортируется ниже из ``openapi_contracts``, — публичный API:
``openapi_contracts`` и ``openapi_contracts.integrations.d42``. Во внутренности
интеграций (``...integrations.jj.contract_mock``, ``...d42.converter``) тест не
лезет и лезть не должен.
"""

from __future__ import annotations

import os
from typing import Any

import aiohttp
import pytest
from app_contracts.generated import operations
from d42 import schema

from openapi_contracts import Direction
from openapi_contracts.integrations.d42 import EACH, build_fixture, overlay_generators

#: Рабочее пространство, в котором живут документы этого теста.
WORKSPACE_ID = "w-42"

#: Заголовок, которым подменяется сгенерированная «строка из случайных букв».
READABLE_TITLE = "Годовой отчёт"


# --------------------------------------------------------------------------------------
# Прикладной код, который тест проверяет.
#
# В настоящем проекте это клиент сервиса или страница фронта. Здесь — три строки
# на aiohttp: важно не то, чем ходят в сеть, а то, что ходят в мок и что мок
# проверяет запрос по контракту.
# --------------------------------------------------------------------------------------


def _base_url() -> str:
    """Адрес мок-сервера JJ. Тот же, что читает сам JJ."""
    return os.environ.get("JJ_REMOTE_MOCK_URL", "http://localhost:8080")


def _documents_url(workspace_id: str = WORKSPACE_ID) -> str:
    return f"{_base_url()}/api/v1/workspaces/{workspace_id}/documents"


async def fetch_documents(*, limit: int | None = None) -> dict[str, Any]:
    """Прикладной вызов: получить страницу документов."""
    params = {} if limit is None else {"limit": str(limit)}
    async with (
        aiohttp.ClientSession() as session,
        session.get(_documents_url(), params=params) as response,
    ):
        response.raise_for_status()
        return await response.json()


async def create_document(*, title: str, visibility: str) -> dict[str, Any]:
    """Прикладной вызов: создать документ."""
    payload = {"title": title, "visibility": visibility}
    async with (
        aiohttp.ClientSession() as session,
        session.post(_documents_url(), json=payload) as response,
    ):
        response.raise_for_status()
        return await response.json()


# --------------------------------------------------------------------------------------
# Тестовые данные: generated-схема + overlay поверх неё.
# --------------------------------------------------------------------------------------


def document_page_fixture() -> dict[str, Any]:
    """Страница документов: контракт сгенерирован, читаемым сделан один лист.

    ``d42_schema`` отдаёт ровно ту схему, которую сгенерировал ``update``;
    :func:`overlay_generators` подменяет генератор ``items[*].title``, а
    :data:`EACH` означает «каждый элемент массива». Структура (какие ключи есть,
    какие обязательны, сколько элементов в списке) остаётся сгенерированной —
    overlay умеет менять только генератор листа.
    """
    handle = operations.api.list_documents
    variant = handle.response(status=200)
    generated = handle.d42_schema(Direction.RESPONSE, export=variant.d42_export)
    readable = overlay_generators(generated, {("items", EACH, "title"): schema.str(READABLE_TITLE)})
    # build_fixture детерминирован и проверяет результат и по overlay-схеме,
    # и по контракту, который был до overlay'я.
    return build_fixture(readable)


def document_fixture() -> dict[str, Any]:
    """Один документ — ответ на создание (201)."""
    handle = operations.api.create_document
    variant = handle.response(status=201)
    generated = handle.d42_schema(Direction.RESPONSE, export=variant.d42_export)
    readable = overlay_generators(generated, {("title",): schema.str(READABLE_TITLE)})
    return build_fixture(readable)


# --------------------------------------------------------------------------------------
# Тонкая обёртка mocked_* — совместимость с прежним стилем тестов.
# --------------------------------------------------------------------------------------


def mocked_list_documents(
    *,
    response: dict[str, Any],
    workspace_id: str = WORKSPACE_ID,
    wait_for_requests: int | None = 1,
) -> Any:
    """Старое имя, новая реализация.

    Проекты, где уже написаны сотни ``async with mocked_list_documents(...)``,
    переезжают на контракты без правки сценариев: обёртка ровно одна строка, а
    все проверки (тело ответа по контракту, path-параметры, перехваченные
    запросы) появляются сами.

    Возвращаемый объект — контекстный менеджер мока; его тип принадлежит
    интеграции с JJ, поэтому здесь он намеренно не импортируется и не
    аннотируется точнее, чем ``Any``.
    """
    return operations.api.list_documents.mock(
        response=response,
        path_params={"workspaceId": workspace_id},
        wait_for_requests=wait_for_requests,
    )


# --------------------------------------------------------------------------------------
# Тесты
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_documents_canonical_mock() -> None:
    """Канонический вид: ``async with operations.<ns>.<op>.mock(...) as mock``."""
    response = document_page_fixture()

    async with operations.api.list_documents.mock(
        response=response,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    ) as mock:
        page = await fetch_documents(limit=3)

    # Overlay сработал: заголовки читаемые, а структуру никто не трогал.
    assert page == response
    assert page["items"], "контракт требует минимум один элемент"
    assert [item["title"] for item in page["items"]] == [READABLE_TITLE] * len(page["items"])

    # Количество вызовов проверяет тест. wait_for_requests — это синхронизация
    # жизненного цикла мока, а не ассерт: он ждёт «минимум N».
    assert len(mock.history) == 1
    request = mock.history[0]["request"]
    assert request.method == "GET"
    assert dict(request.params)["limit"] == "3"


@pytest.mark.asyncio
async def test_list_documents_through_mocked_wrapper() -> None:
    """Тот же мок через обёртку ``mocked_*`` — и ровно два вызова."""
    response = document_page_fixture()

    async with mocked_list_documents(response=response, wait_for_requests=2) as mock:
        first = await fetch_documents()
        second = await fetch_documents(limit=1)

    assert first == second == response
    # Библиотека дождалась минимум двух запросов; что их ровно два — знает тест.
    assert len(mock.history) == 2
    assert [item["request"].method for item in mock.history] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_create_document_validates_outgoing_request() -> None:
    """Исходящий запрос проверяется по контракту автоматически."""
    response = document_fixture()

    async with operations.api.create_document.mock(
        response=response,
        path_params={"workspaceId": WORKSPACE_ID},
        wait_for_requests=1,
    ) as mock:
        created = await create_document(title="Черновик", visibility="private")

    assert created == response
    assert len(mock.history) == 1
    # Выход из блока уже проверил метод, маршрут, path-параметры, content type и
    # тело запроса по сгенерированной JSON Schema. Тесту остаётся содержательное.
    assert mock.history[0]["request"].body == {"title": "Черновик", "visibility": "private"}

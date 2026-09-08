"""Матрица изменений спецификации: что обязано сломаться, а что обязано устоять.

Библиотека полезна ровно настолько, насколько предсказуемо она реагирует на
правку OpenAPI. Здесь каждая строка матрицы — отдельный тест: берётся маленькая
синтетическая спецификация, по ней генерируются артефакты, затем спецификация
мутируется, и проверяется заявленное поведение.

======================================  ==================================================
Мутация                                 Ожидание
======================================  ==================================================
добавили необязательное поле ответа     контракт его принимает, фикстура не меняется
добавили обязательное поле ответа       после ``update`` фикстура получает поле
добавили обязательное поле запроса      прежде валидное тело перестаёт проходить
удалили/переименовали поле под overlay   ``ContractOverlayError`` с указанием пути
сузили тип / enum / pattern / границы   прежде валидные значение или override падают
добавили вариант enum или union         контракт растёт, фикстура не меняет ветку
поменяли метод / маршрут / статус / CT  ``ManifestBindingError``
поменяли только описания и порядок      артефакты байт-в-байт те же, ``check`` чист
======================================  ==================================================

Домен вымышленный (workspace / document), спецификация собирается из словаря
прямо здесь: мутировать её надо точечно, а править ради этого общие фикстуры
набора — значит ломать соседние тесты.
"""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from d42 import schema

from openapi_contracts.errors import (
    ContractOverlayError,
    ManifestBindingError,
    RequestContractError,
    ResponseContractError,
)
from openapi_contracts.integrations.d42 import build_fixture, overlay_generators
from openapi_contracts.semantic_diff import semantic_fingerprint
from support import Project, make_project

#: Стабильный ключ единственной операции.
OPERATION_KEY = "api.createDocument"

#: Слаг операции: из него собираются имена generated-файлов.
OPERATION_SLUG = "api__create_document"

#: Маршрут операции.
ROUTE = "/api/v1/workspaces/{workspaceId}/documents"

#: Имя generated d42-схемы ответа (корень ответа — ``$ref`` на ``Document``).
RESPONSE_SCHEMA = "GeneratedDocumentSchema"

#: Имя generated d42-схемы тела запроса.
REQUEST_SCHEMA = "GeneratedDocumentCreateSchema"

#: Тело запроса, валидное по исходной спецификации.
VALID_REQUEST: dict[str, Any] = {"title": "Черновик"}

#: Тело ответа, валидное по исходной спецификации.
VALID_RESPONSE: dict[str, Any] = {
    "id": "doc-1",
    "title": "Отчёт",
    "slug": "Report 42",
    "visibility": "workspace",
    "rank": 50,
    "payload": {"text": "первый абзац"},
}


def base_spec() -> dict[str, Any]:
    """Свежая копия исходной спецификации.

    Возвращается именно копия: тесты мутируют её на месте, и общий словарь
    протёк бы между ними.
    """
    return {
        "openapi": "3.0.3",
        "info": {"title": "Workspace Documents API", "version": "1.0.0"},
        "paths": {
            ROUTE: {
                "parameters": [
                    {
                        "name": "workspaceId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "post": {
                    "operationId": "createDocument",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/DocumentCreate"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Созданный документ",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Document"}
                                }
                            },
                        }
                    },
                },
            }
        },
        "components": {
            "schemas": {
                "DocumentCreate": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 120},
                        "visibility": {"type": "string", "enum": ["private", "workspace"]},
                    },
                },
                "Document": {
                    "type": "object",
                    "required": ["id", "title", "visibility", "rank", "payload"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "title": {"type": "string", "minLength": 1, "maxLength": 120},
                        # Без ограничений: сюда добавляется pattern в тесте сужения.
                        "slug": {"type": "string"},
                        "visibility": {"type": "string", "enum": ["private", "workspace"]},
                        "rank": {"type": "integer", "minimum": 0, "maximum": 100},
                        "payload": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {"text": {"type": "string"}},
                                },
                                {
                                    "type": "object",
                                    "required": ["href"],
                                    "properties": {"href": {"type": "string"}},
                                },
                            ]
                        },
                    },
                },
            }
        },
    }


def document(spec: dict[str, Any]) -> dict[str, Any]:
    """Схема ``Document`` внутри спецификации."""
    return spec["components"]["schemas"]["Document"]


def document_create(spec: dict[str, Any]) -> dict[str, Any]:
    """Схема ``DocumentCreate`` внутри спецификации."""
    return spec["components"]["schemas"]["DocumentCreate"]


# --------------------------------------------------------------------------------------
# Каркас: проект, запись спецификации, доступ к артефактам
# --------------------------------------------------------------------------------------


def build_project(root: Path, spec: dict[str, Any] | str) -> Project:
    """Собрать проект-потребитель с единственной операцией."""
    project = make_project(root)
    write_spec(project, spec)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "selection": "explicit"}},
        operations={
            OPERATION_KEY: {
                "source": "main",
                "operation_id": "createDocument",
                "method": "POST",
                "path": ROUTE,
                "request": {"content_type": "application/json"},
                "responses": [{"status": 200, "content_type": "application/json"}],
                "python_path": ["api", "create_document"],
            }
        },
    )
    return project


def write_spec(project: Project, spec: dict[str, Any] | str) -> None:
    """Записать спецификацию: словарь сериализуется, строка кладётся как есть."""
    text = (
        spec if isinstance(spec, str) else yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)
    )
    project.write_spec("api/openapi.yaml", text)


def d42_module(project: Project, direction: str) -> dict[str, Any]:
    """Выполнить сгенерированный d42-модуль и вернуть его пространство имён.

    Модуль именно **выполняется**, а не импортируется. Так тест смотрит на
    закоммиченный артефакт, а не на то, что успел закешировать интерпретатор:
    в одном тесте артефакты перегенерируются по нескольку раз, и кеш ``.pyc``
    (он различает версии по секундам и размеру) мог бы подсунуть старый модуль.
    """
    path = project.output_dir / "_d42" / f"{OPERATION_SLUG}_{direction}.py"
    namespace: dict[str, Any] = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace


def response_fixture(project: Project) -> Any:
    """Детерминированная фикстура ответа по сгенерированной d42-схеме."""
    return build_fixture(d42_module(project, "response")[RESPONSE_SCHEMA])


def response_body_schema(project: Project) -> dict[str, Any]:
    """JSON Schema тела ответа (развёрнутое определение ``Document``)."""
    contract = project.contract_document(OPERATION_SLUG)
    return contract["responses"][0]["schema"]["$defs"]["Document"]


def response_properties(project: Project) -> dict[str, Any]:
    """Свойства JSON Schema ответа из документа контракта."""
    return response_body_schema(project)["properties"]


@contextmanager
def opened(project: Project) -> Iterator[Any]:
    """Импортировать generated-пакет и отдать ``operations``.

    Перед импортом чистится ``__pycache__``: в пределах одного теста артефакты
    переписываются, и стоит уложиться в ту же секунду с тем же размером файла —
    интерпретатор возьмёт устаревший байт-код.
    """
    for cache in project.root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    with project.importable() as module:
        yield module.operations


def handle(operations: Any) -> Any:
    """Ручка единственной операции из generated namespace."""
    return operations.api.create_document


# --------------------------------------------------------------------------------------
# Строка 1: необязательное поле ответа
# --------------------------------------------------------------------------------------


def test_optional_response_field_is_accepted_and_fixture_stays_stable(tmp_path: Path) -> None:
    """Необязательное поле контракт принимает, а стабильную фикстуру не двигает."""
    project = build_project(tmp_path / "project", base_spec())
    project.update()
    before = response_fixture(project)

    spec = base_spec()
    document(spec)["properties"]["summary"] = {"type": "string", "maxLength": 500}
    write_spec(project, spec)
    project.update()

    assert "summary" in response_properties(project)
    assert "summary" not in response_body_schema(project)["required"]

    # Контракт принимает и тело с новым полем, и прежнее тело без него.
    with opened(project) as operations:
        handle(operations).validate_response(dict(VALID_RESPONSE, summary="кратко"), status=200)
        handle(operations).validate_response(VALID_RESPONSE, status=200)

    # Главное: закоммиченная фикстура не поехала. Генератор d42 не заполняет
    # необязательные ключи, поэтому новое поле не сдвигает ни одно значение.
    assert response_fixture(project) == before


# --------------------------------------------------------------------------------------
# Строка 2: обязательное поле ответа
# --------------------------------------------------------------------------------------


def test_required_response_field_appears_in_fixture(tmp_path: Path) -> None:
    """Обязательное поле после ``update`` появляется в сгенерированной фикстуре."""
    project = build_project(tmp_path / "project", base_spec())
    project.update()
    before = response_fixture(project)
    assert "revision" not in before

    spec = base_spec()
    document(spec)["properties"]["revision"] = {"type": "integer", "minimum": 1, "maximum": 9}
    document(spec)["required"].append("revision")
    write_spec(project, spec)
    project.update()

    after = response_fixture(project)
    assert set(after) == set(before) | {"revision"}
    assert 1 <= after["revision"] <= 9

    # И прежнее тело ответа теперь контракту не соответствует.
    with opened(project) as operations, pytest.raises(ResponseContractError) as error:
        handle(operations).validate_response(VALID_RESPONSE, status=200)
    assert "revision" in str(error.value)


# --------------------------------------------------------------------------------------
# Строка 3: обязательное поле запроса
# --------------------------------------------------------------------------------------


def test_required_request_field_breaks_previously_valid_body(tmp_path: Path) -> None:
    """Новое обязательное поле запроса ломает тело, которое вчера было валидным."""
    project = build_project(tmp_path / "project", base_spec())
    project.update()
    with opened(project) as operations:
        handle(operations).validate_request_body(VALID_REQUEST)

    spec = base_spec()
    document_create(spec)["properties"]["notifyMembers"] = {"type": "boolean"}
    document_create(spec)["required"].append("notifyMembers")
    write_spec(project, spec)
    project.update()

    with opened(project) as operations:
        with pytest.raises(RequestContractError) as error:
            handle(operations).validate_request_body(VALID_REQUEST)
        # Дополненное тело проходит: сломался контракт, а не валидатор.
        handle(operations).validate_request_body(dict(VALID_REQUEST, notifyMembers=True))

    assert "notifyMembers" in str(error.value)
    assert error.value.direction == "request"


# --------------------------------------------------------------------------------------
# Строка 4: поле под overlay'ем исчезло
# --------------------------------------------------------------------------------------


def rename_title(spec: dict[str, Any]) -> None:
    """Переименовать ``title`` в ``headline``."""
    properties = document(spec)["properties"]
    properties["headline"] = properties.pop("title")
    document(spec)["required"] = ["id", "headline", "visibility", "rank", "payload"]


def drop_title(spec: dict[str, Any]) -> None:
    """Удалить ``title`` совсем."""
    del document(spec)["properties"]["title"]
    document(spec)["required"] = ["id", "visibility", "rank", "payload"]


@pytest.mark.parametrize(
    "mutate",
    [pytest.param(rename_title, id="rename"), pytest.param(drop_title, id="remove")],
)
def test_missing_overlay_target_names_the_path(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    """Поле под overlay'ем исчезло — ошибка при сборке overlay'я, с точным путём."""
    project = build_project(tmp_path / "project", base_spec())
    project.update()

    overrides = {("title",): schema.str("Годовой отчёт")}
    readable = overlay_generators(d42_module(project, "response")[RESPONSE_SCHEMA], overrides)
    assert build_fixture(readable)["title"] == "Годовой отчёт"

    spec = base_spec()
    mutate(spec)
    write_spec(project, spec)
    project.update()

    with pytest.raises(ContractOverlayError) as error:
        overlay_generators(d42_module(project, "response")[RESPONSE_SCHEMA], overrides)
    message = str(error.value)
    assert "/title" in message, message
    assert "'title'" in message, message


# --------------------------------------------------------------------------------------
# Строка 5: сужение типа, enum, pattern и границ
# --------------------------------------------------------------------------------------


def narrow_type(spec: dict[str, Any]) -> None:
    """Сузить тип ``id`` со строки до целого."""
    document(spec)["properties"]["id"] = {"type": "integer", "minimum": 1}


def narrow_bounds(spec: dict[str, Any]) -> None:
    """Сузить верхнюю границу ``rank`` со 100 до 5."""
    document(spec)["properties"]["rank"]["maximum"] = 5


def narrow_pattern(spec: dict[str, Any]) -> None:
    """Навесить ``pattern`` на ``slug``, у которого ограничений не было."""
    document(spec)["properties"]["slug"]["pattern"] = "^[a-z0-9-]+$"


def narrow_enum(spec: dict[str, Any]) -> None:
    """Убрать вариант ``workspace`` из enum ``visibility``."""
    document(spec)["properties"]["visibility"]["enum"] = ["private"]


@pytest.mark.parametrize(
    ("mutate", "pointer"),
    [
        pytest.param(narrow_type, "/id", id="type"),
        pytest.param(narrow_bounds, "/rank", id="bounds"),
        pytest.param(narrow_pattern, "/slug", id="pattern"),
        pytest.param(narrow_enum, "/visibility", id="enum"),
    ],
)
def test_narrowing_rejects_previously_valid_response(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], pointer: str
) -> None:
    """Любое сужение ловится на теле, которое до правки было валидным."""
    project = build_project(tmp_path / "project", base_spec())
    project.update()
    with opened(project) as operations:
        handle(operations).validate_response(VALID_RESPONSE, status=200)

    spec = base_spec()
    mutate(spec)
    write_spec(project, spec)
    project.update()

    with opened(project) as operations, pytest.raises(ResponseContractError) as error:
        handle(operations).validate_response(VALID_RESPONSE, status=200)
    assert error.value.json_pointer == pointer


def test_narrowed_enum_rejects_previously_valid_override(tmp_path: Path) -> None:
    """Override, попадавший в enum, после сужения отвергается при сборке overlay'я."""
    project = build_project(tmp_path / "project", base_spec())
    project.update()

    overrides = {("visibility",): schema.str("workspace")}
    readable = overlay_generators(d42_module(project, "response")[RESPONSE_SCHEMA], overrides)
    assert build_fixture(readable)["visibility"] == "workspace"

    spec = base_spec()
    narrow_enum(spec)
    write_spec(project, spec)
    project.update()

    with pytest.raises(ContractOverlayError) as error:
        overlay_generators(d42_module(project, "response")[RESPONSE_SCHEMA], overrides)
    message = str(error.value)
    assert "/visibility" in message, message
    assert "'workspace'" in message, message


def test_narrowed_bounds_reject_previously_valid_overlay_fixture(tmp_path: Path) -> None:
    """Границы проверяются на значении, поэтому падает генерация фикстуры, а не сборка.

    Совместимость типов overlay проверяет заранее, а «влезает ли 50 в
    ``max(5)``» — только для конкретного значения. Поэтому overlay собирается, а
    :func:`build_fixture` падает ``ContractOverlayError``: ручной генератор вышел
    за пределы сгенерированного контракта.
    """
    project = build_project(tmp_path / "project", base_spec())
    project.update()

    overrides = {("rank",): schema.int(50)}
    readable = overlay_generators(d42_module(project, "response")[RESPONSE_SCHEMA], overrides)
    assert build_fixture(readable)["rank"] == 50

    spec = base_spec()
    narrow_bounds(spec)
    write_spec(project, spec)
    project.update()

    narrowed = overlay_generators(d42_module(project, "response")[RESPONSE_SCHEMA], overrides)
    with pytest.raises(ContractOverlayError) as error:
        build_fixture(narrowed)
    assert "контракт" in str(error.value)


# --------------------------------------------------------------------------------------
# Строка 6: новый вариант enum или union
# --------------------------------------------------------------------------------------


def test_added_variants_extend_contract_without_moving_fixture(tmp_path: Path) -> None:
    """Вариант, добавленный в конец, расширяет контракт и не двигает фикстуру."""
    project = build_project(tmp_path / "project", base_spec())
    project.update()
    before = response_fixture(project)
    document_before = project.contract_document(OPERATION_SLUG)
    assert before["visibility"] == "private"
    assert set(before["payload"]) == {"text"}

    spec = base_spec()
    document(spec)["properties"]["visibility"]["enum"].append("archived")
    document(spec)["properties"]["payload"]["oneOf"].append(
        {"type": "object", "required": ["ref"], "properties": {"ref": {"type": "string"}}}
    )
    write_spec(project, spec)
    project.update()

    properties = response_properties(project)
    assert properties["visibility"]["enum"] == ["private", "workspace", "archived"]
    assert len(properties["payload"]["oneOf"]) == 3

    document_after = project.contract_document(OPERATION_SLUG)
    assert semantic_fingerprint(document_before) != semantic_fingerprint(document_after)

    # Фикстура по-прежнему идёт по первой ветке: ни enum, ни oneOf не переехали.
    after = response_fixture(project)
    assert after == before

    # И новый вариант контракт принимает.
    with opened(project) as operations:
        handle(operations).validate_response(
            dict(VALID_RESPONSE, visibility="archived", payload={"ref": "doc-2"}), status=200
        )


# --------------------------------------------------------------------------------------
# Строка 7: изменилась привязка
# --------------------------------------------------------------------------------------


def change_method(spec: dict[str, Any]) -> None:
    """POST стал PUT."""
    item = spec["paths"][ROUTE]
    item["put"] = item.pop("post")


def change_path(spec: dict[str, Any]) -> None:
    """Маршрут переименован."""
    spec["paths"]["/api/v1/workspaces/{workspaceId}/records"] = spec["paths"].pop(ROUTE)


def change_status(spec: dict[str, Any]) -> None:
    """Статус успешного ответа стал 201."""
    responses = spec["paths"][ROUTE]["post"]["responses"]
    responses["201"] = responses.pop("200")


def change_request_content_type(spec: dict[str, Any]) -> None:
    """Content type тела запроса стал вендорным."""
    content = spec["paths"][ROUTE]["post"]["requestBody"]["content"]
    content["application/vnd.example.document+json"] = content.pop("application/json")


def change_response_content_type(spec: dict[str, Any]) -> None:
    """Content type тела ответа стал вендорным."""
    content = spec["paths"][ROUTE]["post"]["responses"]["200"]["content"]
    content["application/vnd.example.document+json"] = content.pop("application/json")


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(change_method, "метод изменился", id="method"),
        pytest.param(change_path, "маршрут изменился", id="path"),
        pytest.param(change_status, "исчез из спецификации", id="status"),
        pytest.param(change_request_content_type, "request content type", id="request-ct"),
        pytest.param(change_response_content_type, "исчез из спецификации", id="response-ct"),
    ],
)
def test_binding_change_is_reported(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    """Любое расхождение закреплённой привязки — ``ManifestBindingError``."""
    project = build_project(tmp_path / "project", base_spec())
    project.update()

    spec = base_spec()
    mutate(spec)
    write_spec(project, spec)

    with pytest.raises(ManifestBindingError) as error:
        project.build()
    message = str(error.value)
    assert expected in message, message
    assert OPERATION_KEY in message, message

    # Артефакты на диске не тронуты: сборка упала до записи.
    assert project.contract_document(OPERATION_SLUG)["method"] == "POST"


# --------------------------------------------------------------------------------------
# Строка 8: чистая косметика
# --------------------------------------------------------------------------------------


def cosmetic_rewrite(node: Any) -> Any:
    """Переписать документ, не трогая семантику.

    Порядок ключей переворачивается, к каждой схеме добавляются ``description``
    и ``example``. Ни то, ни другое не влияет на то, какие данные валидны.
    """
    if isinstance(node, dict):
        rewritten = {key: cosmetic_rewrite(node[key]) for key in reversed(list(node))}
        kind = rewritten.get("type")
        if kind in ("string", "integer", "object", "array"):
            rewritten["description"] = f"человекочитаемое описание ({kind})"
        if kind == "string":
            rewritten["example"] = "образец"
        elif kind == "integer":
            rewritten["example"] = 7
        return rewritten
    if isinstance(node, list):
        return [cosmetic_rewrite(item) for item in node]
    return node


def test_cosmetic_changes_produce_byte_identical_artifacts(tmp_path: Path) -> None:
    """Переформатирование YAML не меняет ни артефакты, ни отпечаток семантики."""
    project = build_project(tmp_path / "project", base_spec())
    before = project.update()
    document_before = project.contract_document(OPERATION_SLUG)

    reformatted = yaml.safe_dump(
        cosmetic_rewrite(base_spec()),
        allow_unicode=True,
        sort_keys=True,
        default_flow_style=False,
        indent=4,
        width=60,
    )
    original = (project.root / "api" / "openapi.yaml").read_text(encoding="utf-8")
    assert reformatted != original, "переформатирование обязано менять текст файла"

    write_spec(project, reformatted)
    after = project.render()

    assert {item.path: item.content for item in after.files} == {
        item.path: item.content for item in before.files
    }
    assert project.check().is_clean

    document_after = next(
        item for item in after.files if item.path == f"contracts/{OPERATION_SLUG}.json"
    )
    assert semantic_fingerprint(document_before) == semantic_fingerprint(
        json.loads(document_after.content)
    )


def test_cosmetic_rewrite_actually_touches_the_document() -> None:
    """Страховка самой мутации: она обязана менять документ, иначе тест выше пустой."""
    spec = base_spec()
    rewritten = cosmetic_rewrite(copy.deepcopy(spec))
    assert rewritten != spec
    assert "description" in rewritten["components"]["schemas"]["Document"]
    assert list(rewritten) != list(spec)

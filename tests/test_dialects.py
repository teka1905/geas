"""Диалекты: детект версии, отказ от неподдержанных и эквивалентность пары.

Главный тест файла — :func:`test_dialect_pair_produces_identical_contracts`.
Он доказывает то, ради чего вообще существует слой ``dialects/``: Swagger 2.0 и
OpenAPI 3.0.x, описывающие один и тот же API, обязаны давать **байт-в-байт
одинаковый** контракт. Если это так, значит различия диалектов действительно
исчезли на границе адаптера, а не расползлись по генератору и runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from geas.dialects.base import detect_dialect
from geas.errors import ManifestBindingError, UnsupportedSpecVersionError
from support import make_project, spec

_OPERATIONS = ("listDocuments", "createDocument", "deleteDocument")


def _pair_project(root: Path, fixture: str, filename: str) -> Any:
    """Проект вокруг одной из двух эквивалентных спецификаций."""
    project = make_project(root)
    project.write_spec(f"api/{filename}", spec("basic", fixture))
    project.write_manifest(
        sources={"main": {"path": f"api/{filename}", "selection": "explicit"}},
        operations={
            f"api.{name}": {"source": "main", "operation_id": name} for name in _OPERATIONS
        },
    )
    return project


def test_detects_swagger2() -> None:
    """``swagger: "2.0"`` уходит в адаптер Swagger 2.0."""
    document = json.loads(spec("basic", "swagger2.json").read_text(encoding="utf-8"))

    assert detect_dialect(document, source="main").name == "swagger2"


def test_detects_openapi30() -> None:
    """``openapi: 3.0.x`` уходит в адаптер OpenAPI 3.0."""
    document = yaml.safe_load(spec("basic", "openapi30.yaml").read_text(encoding="utf-8"))

    assert detect_dialect(document, source="main").name == "openapi30"


def test_openapi31_is_rejected_with_a_precise_diagnostic() -> None:
    """OpenAPI 3.1 не считается совместимым с 3.0 и отклоняется явно.

    Требование к тексту жёсткое: он обязан назвать конкретные несовместимости,
    а не ограничиться «версия не поддерживается». Иначе первое, что сделает
    читатель, — попробует «просто поправить номер версии».
    """
    document = yaml.safe_load(spec("features", "openapi31.yaml").read_text(encoding="utf-8"))

    with pytest.raises(UnsupportedSpecVersionError) as info:
        detect_dialect(document, source="main")

    message = str(info.value)
    assert "3.1" in message
    assert "nullable" in message
    assert "exclusiveMinimum" in message
    assert "openapi31.py" in message
    # Номер версии библиотеки в тексте устаревает с первым же релизом.
    assert "в версии" not in message, message


def test_openapi31_is_rejected_through_the_whole_pipeline(tmp_path: Path) -> None:
    """Отказ приходит и из полного пайплайна, а не только из детекта."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("features", "openapi31.yaml"))
    project.write_manifest(sources={"main": {"path": "api/spec.yaml", "selection": "all"}})

    with pytest.raises(UnsupportedSpecVersionError):
        project.build()


def test_swagger1_is_rejected() -> None:
    """Swagger 1.2 не поддерживается."""
    document = json.loads(spec("features", "swagger1.json").read_text(encoding="utf-8"))

    with pytest.raises(UnsupportedSpecVersionError) as info:
        detect_dialect(document, source="main")

    assert "2.0" in str(info.value)


def test_document_without_version_is_rejected() -> None:
    """Документ без ``swagger`` и без ``openapi`` — не спецификация."""
    with pytest.raises(UnsupportedSpecVersionError) as info:
        detect_dialect({"paths": {}}, source="main")

    assert "swagger" in str(info.value)
    assert "openapi" in str(info.value)


def test_dialect_pair_produces_identical_contracts(tmp_path: Path) -> None:
    """Эквивалентные Swagger 2.0 и OpenAPI 3.0 дают один и тот же контракт.

    Отличаться разрешено ровно одному полю — ``dialect``: это происхождение
    документа, а не часть контракта, и в semantic fingerprint оно не входит.
    """
    swagger = _pair_project(tmp_path / "swagger", "swagger2.json", "spec.json").build()
    openapi = _pair_project(tmp_path / "openapi", "openapi30.yaml", "spec.yaml").build()

    assert [item.contract.key for item in swagger.operations] == [
        item.contract.key for item in openapi.operations
    ]

    for left, right in zip(swagger.operations, openapi.operations):
        assert left.document["dialect"] == "swagger2"
        assert right.document["dialect"] == "openapi30"
        assert {k: v for k, v in left.document.items() if k != "dialect"} == {
            k: v for k, v in right.document.items() if k != "dialect"
        }


def test_dialect_pair_has_identical_semantic_fingerprints(tmp_path: Path) -> None:
    """Отпечаток семантики не зависит от диалекта источника."""
    from geas.semantic_diff import semantic_fingerprint

    swagger = _pair_project(tmp_path / "swagger", "swagger2.json", "spec.json").build()
    openapi = _pair_project(tmp_path / "openapi", "openapi30.yaml", "spec.yaml").build()

    assert {
        item.contract.key: semantic_fingerprint(item.document) for item in swagger.operations
    } == {item.contract.key: semantic_fingerprint(item.document) for item in openapi.operations}


def test_swagger2_base_path_becomes_part_of_the_route(tmp_path: Path) -> None:
    """``basePath`` Swagger 2.0 — часть маршрута, а не украшение.

    Без него мок сматчил бы не тот путь, и тест бы «проходил» на пустой истории.
    """
    result = _pair_project(tmp_path / "swagger", "swagger2.json", "spec.json").build()
    routes = {item.contract.key: item.contract.path for item in result.operations}

    assert routes["api.listDocuments"] == "/api/v1/workspaces/{workspaceId}/documents"


def test_base_path_can_be_overridden_in_manifest(tmp_path: Path) -> None:
    """``sources.<name>.base_path`` перекрывает префикс, который диктует документ."""
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.json", spec("basic", "swagger2.json"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.json", "selection": "explicit", "base_path": "/proxy"}},
        operations={"api.listDocuments": {"source": "main", "operation_id": "listDocuments"}},
    )

    result = project.build()

    assert result.operations[0].contract.path == "/proxy/workspaces/{workspaceId}/documents"


def test_collection_formats_map_to_the_same_serialization(tmp_path: Path) -> None:
    """``collectionFormat`` Swagger 2.0 и ``style``/``explode`` OpenAPI 3.0 совпадают."""
    swagger = _pair_project(tmp_path / "swagger", "swagger2.json", "spec.json").build()
    openapi = _pair_project(tmp_path / "openapi", "openapi30.yaml", "spec.yaml").build()

    def serialization(result: Any) -> dict[str, tuple[str, bool]]:
        listing = result.by_key("api.listDocuments").contract
        return {item.name: (item.style, item.explode) for item in listing.request.query_parameters}

    left, right = serialization(swagger), serialization(openapi)
    assert left == right
    # multi → повторяющийся ключ, csv → одно значение через запятую.
    assert left["status"] == ("form", True)
    assert left["tags"] == ("form", False)


def _gateway_project(root: Path, fixture: str, filename: str, **source: Any) -> Any:
    project = make_project(root)
    project.write_spec(f"api/{filename}", spec("basic", fixture))
    project.write_manifest(
        sources={"main": {"path": f"api/{filename}", "selection": "explicit", **source}},
        operations={"api.listDocuments": {"source": "main", "operation_id": "listDocuments"}},
    )
    return project


@pytest.mark.parametrize(
    ("fixture", "filename"), [("openapi30.yaml", "spec.yaml"), ("swagger2.json", "spec.json")]
)
def test_strip_prefix_replaces_spec_prefix_with_gateway_prefix(
    tmp_path: Path, fixture: str, filename: str
) -> None:
    """``strip_prefix`` снимает префикс спецификации, ``base_path`` добавляет префикс шлюза."""
    project = _gateway_project(
        tmp_path / "p", fixture, filename, strip_prefix="/api/v1", base_path="/gw/docs"
    )

    result = project.build()

    assert result.operations[0].contract.path == "/gw/docs/workspaces/{workspaceId}/documents"


def test_strip_prefix_without_base_path_just_removes_the_prefix(tmp_path: Path) -> None:
    project = _gateway_project(
        tmp_path / "p", "openapi30.yaml", "spec.yaml", strip_prefix="/api/v1/"
    )

    assert project.build().operations[0].contract.path == "/workspaces/{workspaceId}/documents"


def test_strip_prefix_mismatch_on_selected_operation_fails(tmp_path: Path) -> None:
    """Операция вне снимаемого префикса не сохраняет исходный путь молча."""
    project = _gateway_project(tmp_path / "p", "openapi30.yaml", "spec.yaml", strip_prefix="/other")

    with pytest.raises(ManifestBindingError, match="не начинается со снимаемого префикса"):
        project.build()


def test_strip_prefix_matches_whole_segments_only(tmp_path: Path) -> None:
    """``/api/v`` не должен срезать часть сегмента ``/api/v1``."""
    project = _gateway_project(tmp_path / "p", "openapi30.yaml", "spec.yaml", strip_prefix="/api/v")

    with pytest.raises(ManifestBindingError, match="не начинается со снимаемого префикса"):
        project.build()


def test_strip_prefix_mismatch_ignores_unselected_operations(tmp_path: Path) -> None:
    """Не выбранные операции вне префикса генерацию не ломают."""
    ok = {"200": {"description": "ok"}}
    document = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/svc/ping": {"get": {"operationId": "ping", "responses": ok}},
            "/other/pong": {"get": {"operationId": "pong", "responses": ok}},
        },
    }
    project = make_project(tmp_path / "p")
    project.write("api/spec.yaml", yaml.safe_dump(document))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "strip_prefix": "/svc", "base_path": "/gw"}},
        operations={"api.ping": {"source": "main", "operation_id": "ping"}},
    )

    assert project.build().operations[0].contract.path == "/gw/ping"


def test_list_and_add_use_the_final_route(tmp_path: Path) -> None:
    project = _gateway_project(
        tmp_path / "p", "openapi30.yaml", "spec.yaml", strip_prefix="/api/v1", base_path="/gw"
    )

    listing = project.cli("list", "--json")
    assert listing.returncode == 0, listing.stderr
    paths = {op["path"] for op in json.loads(listing.stdout)["sources"][0]["operations"]}
    assert "/gw/workspaces/{workspaceId}/documents" in paths
    # Все маршруты отображены: легенда про «!» не нужна.
    assert "! —" not in project.cli("list").stdout

    added = project.cli(
        "add", "api.createDocument", "--source", "main", "--operation-id", "createDocument"
    )
    assert added.returncode == 0, added.stderr
    assert "/gw/workspaces/{workspaceId}/documents" in project.manifest_path.read_text(
        encoding="utf-8"
    )


def _routes_project(
    root: Path,
    routes: dict[str, str],
    *,
    operations: dict[str, Any] | None = None,
    **source: Any,
) -> Any:
    """Проект с OpenAPI 3.0: ``routes`` — маршрут → ``operationId`` его GET-операции."""
    ok = {"200": {"description": "ok"}}
    document = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {
            path: {"get": {"operationId": operation_id, "responses": ok}}
            for path, operation_id in routes.items()
        },
    }
    project = make_project(root)
    project.write("api/spec.yaml", yaml.safe_dump(document))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", **source}},
        operations=operations,
    )
    return project


@pytest.mark.parametrize(
    "binding",
    [
        {"method": "GET", "path": "/b/x"},
        {"operation_id": "mapped", "method": "GET", "path": "/b/x"},
    ],
)
def test_final_route_wins_over_same_original_route_outside_prefix(
    tmp_path: Path, binding: dict[str, str]
) -> None:
    """Исходный путь операции вне префикса не перехватывает итоговый маршрут другой."""
    project = _routes_project(
        tmp_path / "p",
        {"/a/x": "mapped", "/b/x": "outside"},
        operations={"api.x": {"source": "main", **binding}},
        strip_prefix="/a",
        base_path="/b",
    )

    operation = project.build().operations[0].contract

    assert (operation.operation_id, operation.path) == ("mapped", "/b/x")


def test_add_picks_the_mapped_operation_when_routes_collide(tmp_path: Path) -> None:
    project = _routes_project(
        tmp_path / "p", {"/a/x": "mapped", "/b/x": "outside"}, strip_prefix="/a", base_path="/b"
    )

    added = project.cli("add", "api.x", "--source", "main", "--method", "GET", "--path", "/b/x")

    assert added.returncode == 0, added.stderr
    assert "operation_id: mapped" in project.manifest_path.read_text(encoding="utf-8")


def test_strip_prefix_with_selection_all_maps_every_operation(tmp_path: Path) -> None:
    project = _routes_project(
        tmp_path / "p",
        {"/svc/ping": "ping", "/svc/pong": "pong"},
        selection="all",
        strip_prefix="/svc",
        base_path="/gw",
    )

    paths = {item.contract.path for item in project.build().operations}

    assert paths == {"/gw/ping", "/gw/pong"}


def test_strip_prefix_with_selection_all_fails_on_any_operation_outside(tmp_path: Path) -> None:
    """В режиме ``all`` выбраны все операции, поэтому любая вне префикса — ошибка."""
    project = _routes_project(
        tmp_path / "p",
        {"/svc/ping": "ping", "/other/pong": "pong"},
        selection="all",
        strip_prefix="/svc",
    )

    with pytest.raises(ManifestBindingError, match="'/other/pong' не начинается"):
        project.build()


def test_final_route_wins_when_colliding_operations_share_operation_id(tmp_path: Path) -> None:
    """Общий ``operationId`` не отдаёт маршрут операции вне префикса, даже если она первая."""
    project = _routes_project(
        tmp_path / "p",
        {"/a/x": "dup", "/b/x": "dup"},
        operations={
            "api.x": {"source": "main", "operation_id": "dup", "method": "GET", "path": "/a/x"}
        },
        strip_prefix="/b",
        base_path="/a",
    )

    assert project.build().operations[0].contract.path == "/a/x"

    added = project.cli(
        "add",
        "api.y",
        "--source",
        "main",
        "--operation-id",
        "dup",
        "--method",
        "GET",
        "--path",
        "/a/x",
    )
    assert added.returncode == 0, added.stderr


def test_list_selects_only_the_mapped_operation_when_routes_collide(tmp_path: Path) -> None:
    project = _routes_project(
        tmp_path / "p",
        {"/a/x": "mapped", "/b/x": "outside"},
        operations={"api.x": {"source": "main", "method": "GET", "path": "/b/x"}},
        strip_prefix="/a",
        base_path="/b",
    )

    listing = project.cli("list", "--json")
    assert listing.returncode == 0, listing.stderr
    report = {
        op["operation_id"]: op for op in json.loads(listing.stdout)["sources"][0]["operations"]
    }
    assert (report["mapped"]["key"], report["mapped"]["route_error"]) == ("api.x", None)
    assert report["outside"]["key"] is None
    assert "не начинается со снимаемого префикса" in report["outside"]["route_error"]

    text = project.cli("list")
    assert text.returncode == 0, text.stderr
    assert "! маршрут '/b/x' не начинается" in text.stdout
    assert "sources.main.strip_prefix" in text.stdout
    assert "! — маршрут не отображён strip_prefix" in text.stdout


@pytest.mark.parametrize(
    ("source", "expected"),
    [({"strip_prefix": "/svc", "base_path": "/gw"}, "/gw"), ({"strip_prefix": "/svc/"}, "/")],
)
def test_route_equal_to_strip_prefix_maps_to_base_path(
    tmp_path: Path, source: dict[str, str], expected: str
) -> None:
    project = _routes_project(
        tmp_path / "p",
        {"/svc": "root"},
        operations={"api.root": {"source": "main", "operation_id": "root"}},
        **source,
    )

    assert project.build().operations[0].contract.path == expected


@pytest.mark.parametrize("selection", ["explicit", "all"])
def test_routes_collapsed_by_strip_prefix_are_not_guessed(tmp_path: Path, selection: str) -> None:
    """``/a`` и ``/a/`` без префикса оба стали бы ``/`` — выбрать одну из них нельзя."""
    project = _routes_project(
        tmp_path / "p",
        {"/a": "one", "/a/": "two"},
        operations=(
            {"api.root": {"source": "main", "method": "GET", "path": "/"}}
            if selection == "explicit"
            else None
        ),
        selection=selection,
        strip_prefix="/a",
    )

    with pytest.raises(ManifestBindingError, match=r"маршруты \['/a', '/a/'\] совпадают: GET /"):
        project.build()


def test_missing_route_hints_at_strip_prefix(tmp_path: Path) -> None:
    """Путь до ``strip_prefix`` в manifest — частая ошибка при переходе: подсказываем её."""
    project = _routes_project(
        tmp_path / "p",
        {"/svc/ping": "ping"},
        operations={"api.ping": {"source": "main", "method": "GET", "path": "/svc/ping"}},
        strip_prefix="/svc",
        base_path="/gw",
    )

    with pytest.raises(ManifestBindingError, match="укажите итоговый маршрут"):
        project.build()


def test_check_detects_drift_of_the_final_route(tmp_path: Path) -> None:
    """Итоговый маршрут входит в артефакты, поэтому смена ``base_path`` — это drift."""
    project = _gateway_project(
        tmp_path / "p", "openapi30.yaml", "spec.yaml", strip_prefix="/api/v1", base_path="/gw"
    )
    project.update()
    slug = project.generated_index()["operations"]["api.listDocuments"]["slug"]
    assert project.contract_document(slug)["path"] == "/gw/workspaces/{workspaceId}/documents"
    checked = project.cli("check")
    assert checked.returncode == 0, checked.stdout + checked.stderr

    _gateway_project(
        tmp_path / "p", "openapi30.yaml", "spec.yaml", strip_prefix="/api/v1", base_path="/gw2"
    )
    drifted = project.cli("check")

    assert drifted.returncode == 1, drifted.stdout + drifted.stderr
    assert "разошлись со спецификацией" in drifted.stderr


@pytest.mark.parametrize(
    ("routes", "flags", "reason"),
    [
        ({"/other/x": "x"}, ["--operation-id", "x"], "не начинается со снимаемого префикса"),
        ({"/svc": "one", "/svc/": "two"}, ["--method", "GET", "--path", "/"], "совпадают: GET /"),
    ],
)
def test_add_refuses_an_operation_with_route_error(
    tmp_path: Path, routes: dict[str, str], flags: list[str], reason: str
) -> None:
    """``add`` называет причину и не трогает manifest, а не пишет «неоднозначно»."""
    project = _routes_project(tmp_path / "p", routes, strip_prefix="/svc")
    before = project.manifest_path.read_bytes()

    added = project.cli("add", "api.x", "--source", "main", *flags)

    assert added.returncode == 2, added.stdout + added.stderr
    assert reason in added.stderr
    assert "неоднозначно" not in added.stderr
    assert project.manifest_path.read_bytes() == before


def test_add_with_a_route_before_strip_prefix_hints_at_the_final_route(tmp_path: Path) -> None:
    project = _routes_project(tmp_path / "p", {"/svc/ping": "ping"}, strip_prefix="/svc")

    added = project.cli(
        "add", "api.ping", "--source", "main", "--method", "GET", "--path", "/svc/ping"
    )

    assert added.returncode == 2, added.stdout + added.stderr
    assert "укажите итоговый маршрут" in added.stderr


def test_route_bound_before_strip_prefix_hints_at_the_final_route(tmp_path: Path) -> None:
    """Типичная миграция: ``geas add`` закрепил operation_id и исходный путь, потом включили
    ``strip_prefix``. Ошибка называет итоговый маршрут и причину."""
    project = _routes_project(
        tmp_path / "p",
        {"/svc/x": "x"},
        operations={
            "api.x": {"source": "main", "operation_id": "x", "method": "GET", "path": "/svc/x"}
        },
        strip_prefix="/svc",
        base_path="/gw",
    )

    with pytest.raises(ManifestBindingError) as info:
        project.build()

    assert "маршрут изменился: в manifest '/svc/x', в спецификации '/gw/x'" in str(info.value)
    assert "укажите итоговый маршрут" in str(info.value)


def test_operation_outside_prefix_reports_its_own_reason_despite_route_collision(
    tmp_path: Path,
) -> None:
    """Закреплённая по operationId операция вне префикса не подменяется чужой по маршруту."""
    project = _routes_project(
        tmp_path / "p",
        {"/a/x": "mapped", "/b/x": "outside"},
        operations={
            "api.x": {"source": "main", "operation_id": "outside", "method": "GET", "path": "/b/x"}
        },
        strip_prefix="/a",
        base_path="/b",
    )

    with pytest.raises(ManifestBindingError, match="'/b/x' не начинается со снимаемого префикса"):
        project.build()


def test_add_hints_at_the_final_route_only_when_the_route_is_missing(tmp_path: Path) -> None:
    project = _routes_project(
        tmp_path / "p", {"/svc/ping": "ping"}, strip_prefix="/svc", base_path="/gw"
    )

    added = project.cli(
        "add", "api.ping", "--source", "main", "--operation-id", "typo", "--path", "/gw/ping"
    )

    assert added.returncode == 2, added.stdout + added.stderr
    assert "нет операции" in added.stderr
    assert "укажите итоговый маршрут" not in added.stderr


def test_empty_base_path_still_drops_swagger_base_path(tmp_path: Path) -> None:
    """Пустой ``base_path`` проходит проверку на ``/`` и по-прежнему убирает ``basePath``."""
    project = _gateway_project(tmp_path / "p", "swagger2.json", "spec.json", base_path="")

    assert project.build().operations[0].contract.path == "/workspaces/{workspaceId}/documents"

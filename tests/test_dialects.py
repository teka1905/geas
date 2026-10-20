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

    with pytest.raises(ManifestBindingError):
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

    added = project.cli(
        "add", "api.createDocument", "--source", "main", "--operation-id", "createDocument"
    )
    assert added.returncode == 0, added.stderr
    assert "/gw/workspaces/{workspaceId}/documents" in project.manifest_path.read_text(
        encoding="utf-8"
    )

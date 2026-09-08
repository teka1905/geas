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
from geas.errors import UnsupportedSpecVersionError
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

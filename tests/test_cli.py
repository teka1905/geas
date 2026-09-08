"""CLI ``openapi-contracts``: коды возврата, вывод и транзакционность команд.

Каждая проверка гоняет настоящий подпроцесс через :meth:`support.Project.cli`, а не
вызывает ``main()`` в текущем интерпретаторе. Так проверяются именно коды возврата
(0 / 1 / 2), а не то, что функция вернула число: в CI важен ``$?``, а не значение.

Контракт кодов:

* ``0`` — успех, расхождений нет;
* ``1`` — ошибка контракта: drift, binding, неподдержанная конструкция;
* ``2`` — ошибка использования: опечатка в команде, конфликт, отказ затирать файлы.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from support import Project, make_project, spec

# Источники демо-проекта: имя в manifest → (путь внутри проекта, фикстура).
_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "main": ("api/openapi.yaml", ("basic", "openapi30.yaml")),
    "multi": ("api/multi.yaml", ("features", "multi_content_types.yaml")),
}

# Операции basic/ идут с ``d42: false``: DocumentBase.labels — типизированный
# additionalProperties, который d42 выразить не умеет. CLI это не проверяет.
_LIST = {"source": "main", "operation_id": "listDocuments", "d42": False}
_CREATE = {"source": "main", "operation_id": "createDocument", "d42": False}
_REPLACE = {"source": "multi", "operation_id": "replaceDocumentContent"}


def demo_project(root: Path, operations: Mapping[str, Any]) -> Project:
    """Проект с ровно теми источниками, на которые ссылаются операции."""
    project = make_project(root)
    sources: dict[str, Any] = {}
    for name in sorted({str(item["source"]) for item in operations.values()}):
        relative, fixture = _SOURCES[name]
        project.write_spec(relative, spec(*fixture))
        sources[name] = {"path": relative, "selection": "explicit"}
    project.write_manifest(sources=sources, operations=operations)
    return project


def edit_spec(project: Project, relative: str, mutate: Any) -> None:
    """Изменить спецификацию проекта через YAML round-trip."""
    path = project.root / relative
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


# ------------------------------------------------------------------- init


def test_init_creates_manifest_and_waivers(tmp_path: Path) -> None:
    """``init`` создаёт оба файла и печатает, что делать дальше."""
    project = make_project(tmp_path / "workspace")
    project.write_spec("api/openapi.yaml", spec("basic", "openapi30.yaml"))
    project.waivers_path.unlink()  # make_project кладёт пустой waivers заранее

    result = project.cli(
        "init",
        "--source",
        "api/openapi.yaml",
        "--package",
        "demo_contracts.generated",
        "--directory",
        "demo_contracts/generated",
    )

    assert result.returncode == 0, result.stderr
    assert project.manifest_path.is_file()
    assert project.waivers_path.is_file()
    manifest = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["output"] == {
        "directory": "demo_contracts/generated",
        "package": "demo_contracts.generated",
    }
    assert manifest["sources"]["main"]["path"] == "api/openapi.yaml"
    assert manifest["operations"] == {}
    assert yaml.safe_load(project.waivers_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "waivers": [],
    }
    assert "Дальше:" in result.stdout
    for command in ("list", "add", "update", "check"):
        assert f"openapi-contracts -m {project.manifest_path} {command}" in result.stdout


def test_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    """Повторный ``init`` без ``--force`` — ошибка использования, файлы целы."""
    project = make_project(tmp_path / "workspace")
    project.write_spec("api/openapi.yaml", spec("basic", "openapi30.yaml"))
    project.waivers_path.unlink()
    arguments = ("init", "--source", "api/openapi.yaml", "--package", "demo_contracts.generated")
    assert project.cli(*arguments).returncode == 0
    project.manifest_path.write_text("# правка руками\n", encoding="utf-8")

    result = project.cli(*arguments)

    assert result.returncode == 2
    assert "--force" in result.stderr
    assert project.manifest_path.read_text(encoding="utf-8") == "# правка руками\n"


def test_init_overwrites_with_force(tmp_path: Path) -> None:
    """``--force`` перезаписывает оба файла и возвращает 0."""
    project = make_project(tmp_path / "workspace")
    project.write_spec("api/openapi.yaml", spec("basic", "openapi30.yaml"))
    project.manifest_path.write_text("# правка руками\n", encoding="utf-8")
    arguments = ("init", "--source", "api/openapi.yaml", "--package", "demo_contracts.generated")

    result = project.cli(*arguments, "--force")

    assert result.returncode == 0, result.stderr
    document = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))
    assert document["sources"]["main"]["path"] == "api/openapi.yaml"


def test_init_then_update_and_check_are_clean(tmp_path: Path) -> None:
    """Свежий проект без операций проходит полный цикл без расхождений."""
    project = make_project(tmp_path / "workspace")
    project.write_spec("api/openapi.yaml", spec("basic", "openapi30.yaml"))
    project.waivers_path.unlink()
    project.cli(
        "init",
        "--source",
        "api/openapi.yaml",
        "--package",
        "demo_contracts.generated",
        "--directory",
        "demo_contracts/generated",
    )

    assert project.cli("update").returncode == 0
    assert project.cli("check").returncode == 0


# ------------------------------------------------------------- list / inspect


@pytest.mark.parametrize("command", ["list", "inspect"])
def test_list_human_output_marks_selected_operations(tmp_path: Path, command: str) -> None:
    """Человекочитаемый вывод помечает выбранные операции и объясняет пометку."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})

    result = project.cli(command)

    assert result.returncode == 0, result.stderr
    assert "источник main: api/openapi.yaml [openapi30]" in result.stdout
    assert "режим выбора: explicit, операций: 3" in result.stdout
    assert "  * GET     /api/v1/workspaces/{workspaceId}/documents" in result.stdout
    assert "ключ=api.listDocuments" in result.stdout
    # Невыбранная операция показана без звёздочки и без ключа.
    assert "    DELETE  /api/v1/workspaces/{workspaceId}/documents/{documentId}" in result.stdout
    assert "operationId=deleteDocument  ключ=-" in result.stdout
    assert "* — операция выбрана manifest" in result.stdout


@pytest.mark.parametrize("command", ["list", "inspect"])
def test_list_json_output_shape(tmp_path: Path, command: str) -> None:
    """``--json`` отдаёт разбираемую структуру с флагом ``selected`` на каждой операции."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})

    result = project.cli(command, "--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["manifest_version"] == 1
    (source,) = report["sources"]
    assert source["name"] == "main"
    assert source["dialect"] == "openapi30"
    assert source["selection"] == "explicit"
    by_operation_id = {item["operation_id"]: item for item in source["operations"]}
    assert by_operation_id["listDocuments"]["selected"] is True
    assert by_operation_id["listDocuments"]["key"] == "api.listDocuments"
    assert by_operation_id["deleteDocument"]["selected"] is False
    assert by_operation_id["deleteDocument"]["key"] is None
    assert by_operation_id["createDocument"]["request_content_types"] == ["application/json"]
    assert by_operation_id["listDocuments"]["responses"] == [
        "200:application/json",
        "400:application/json",
    ]


def test_list_reports_unsupported_variants(tmp_path: Path) -> None:
    """Непредставимый вариант ответа назван причиной, а не спрятан."""
    project = demo_project(tmp_path / "workspace", {"multi.replaceContent": _REPLACE})

    human = project.cli("list")
    machine = project.cli("list", "--json")

    assert human.returncode == 0, human.stderr
    assert "не поддержано: response 200:application/pdf" in human.stdout
    (source,) = json.loads(machine.stdout)["sources"]
    (operation,) = source["operations"]
    assert operation["responses"] == ["200:application/json"]
    assert len(operation["unsupported"]) == 1
    assert "application/pdf" in operation["unsupported"][0]
    assert "JSON" in operation["unsupported"][0]


def test_list_can_be_narrowed_to_one_source(tmp_path: Path) -> None:
    """``--source`` показывает только запрошенный источник."""
    project = demo_project(
        tmp_path / "workspace", {"api.listDocuments": _LIST, "multi.replaceContent": _REPLACE}
    )

    result = project.cli("list", "--source", "multi", "--json")

    assert result.returncode == 0, result.stderr
    sources = json.loads(result.stdout)["sources"]
    assert [item["name"] for item in sources] == ["multi"]


# -------------------------------------------------------------------- add


def test_add_appends_operation_to_manifest(tmp_path: Path) -> None:
    """``add`` дописывает операцию и подсказывает следующий шаг."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})

    result = project.cli(
        "add", "api.createDocument", "--source", "main", "--operation-id", "createDocument"
    )

    assert result.returncode == 0, result.stderr
    assert "api.createDocument" in result.stdout
    assert "openapi-contracts update" in result.stdout
    operations = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))["operations"]
    assert operations["api.createDocument"] == {
        "source": "main",
        "operation_id": "createDocument",
    }
    assert "api.listDocuments" in operations


def test_add_is_idempotent(tmp_path: Path) -> None:
    """Повторный идентичный ``add`` — успех и байт-в-байт тот же manifest."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    arguments = (
        "add",
        "api.createDocument",
        "--source",
        "main",
        "--operation-id",
        "createDocument",
        "--no-d42",
        "--python-path",
        "api.documents.create",
        "--response",
        "200:application/json",
    )
    assert project.cli(*arguments).returncode == 0
    after_first = project.manifest_bytes()

    result = project.cli(*arguments)

    assert result.returncode == 0, result.stderr
    assert "ничего не меняю" in result.stdout
    assert project.manifest_bytes() == after_first


def test_add_refuses_conflicting_redefinition(tmp_path: Path) -> None:
    """Тот же ключ с другими параметрами — ошибка использования, manifest не тронут."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    assert (
        project.cli(
            "add", "api.createDocument", "--source", "main", "--operation-id", "createDocument"
        ).returncode
        == 0
    )
    before = project.manifest_bytes()

    result = project.cli(
        "add",
        "api.createDocument",
        "--source",
        "main",
        "--operation-id",
        "createDocument",
        "--no-d42",
    )

    assert result.returncode == 2
    assert "уже есть в manifest" in result.stderr
    assert project.manifest_bytes() == before


@pytest.mark.parametrize(
    ("fixture", "operation_id"),
    [
        (("features", "no_type.yaml"), "getNote"),
        (("features", "unsupported_keywords.yaml"), "getConst"),
    ],
)
def test_add_is_transactional_on_unsupported_operation(
    tmp_path: Path, fixture: tuple[str, str], operation_id: str
) -> None:
    """Операция, которая не нормализуется, не попадает в manifest даже частично.

    Проверка идёт по байтам: любое «почти записалось» оставило бы в manifest
    операцию, которую следующий ``update`` не смог бы собрать.
    """
    project = make_project(tmp_path / "workspace")
    project.write_spec("api/broken.yaml", spec(*fixture))
    project.write_manifest(
        sources={"broken": {"path": "api/broken.yaml", "selection": "explicit"}}, operations={}
    )
    before = project.manifest_bytes()

    result = project.cli("add", "broken.op", "--source", "broken", "--operation-id", operation_id)

    assert result.returncode != 0
    assert result.returncode == 1, result.stderr
    assert project.manifest_bytes() == before
    assert "ошибка:" in result.stderr


def test_add_writes_python_path_response_and_d42_flag(tmp_path: Path) -> None:
    """``--python-path``, ``--response`` и ``--no-d42`` доезжают до manifest и артефактов."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})

    result = project.cli(
        "add",
        "api.createDocument",
        "--source",
        "main",
        "--operation-id",
        "createDocument",
        "--method",
        "post",
        "--path",
        "/api/v1/workspaces/{workspaceId}/documents",
        "--request-content-type",
        "application/json",
        "--response",
        "200:application/json",
        "--python-path",
        "api.documents.create",
        "--no-d42",
    )

    assert result.returncode == 0, result.stderr
    entry = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))["operations"][
        "api.createDocument"
    ]
    assert entry["method"] == "POST"  # метод нормализуется к верхнему регистру
    assert entry["request"] == {"content_type": "application/json"}
    assert entry["responses"] == [{"status": 200, "content_type": "application/json"}]
    assert entry["python_path"] == ["api", "documents", "create"]
    assert entry["d42"] is False

    assert project.cli("update").returncode == 0
    document = project.contract_document("api__create_document")
    # Закреплён ровно один вариант ответа — 400 в контракт не попадает.
    assert [(item["status"], item["content_type"]) for item in document["responses"]] == [
        (200, "application/json")
    ]
    assert document["python_path"] == ["api", "documents", "create"]
    assert document["d42"]["enabled"] is False
    assert not (project.output_dir / "_d42").exists()


def test_add_rejects_non_numeric_response_status(tmp_path: Path) -> None:
    """Нечисловой статус в ``--response`` — ошибка использования, а не traceback."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    before = project.manifest_bytes()

    result = project.cli(
        "add",
        "api.createDocument",
        "--source",
        "main",
        "--operation-id",
        "createDocument",
        "--response",
        "двести:application/json",
    )

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "--response" in result.stderr
    assert project.manifest_bytes() == before


# --------------------------------------------------------------- update / check


def test_update_then_check_is_clean(tmp_path: Path) -> None:
    """После ``update`` команда ``check`` довольна и печатает число файлов."""
    project = demo_project(
        tmp_path / "workspace", {"api.listDocuments": _LIST, "multi.replaceContent": _REPLACE}
    )

    update = project.cli("update")
    check = project.cli("check")

    assert update.returncode == 0, update.stderr
    assert "обновлено файлов:" in update.stdout
    assert check.returncode == 0, check.stderr
    assert "артефакты актуальны" in check.stdout


def test_update_reports_unsupported_variants(tmp_path: Path) -> None:
    """``update`` не молчит о вариантах, которые в контракт не попали."""
    project = demo_project(tmp_path / "workspace", {"multi.replaceContent": _REPLACE})

    result = project.cli("update")

    assert result.returncode == 0, result.stderr
    assert "multi.replaceContent: не поддержано" in result.stdout
    assert "application/pdf" in result.stdout


def test_update_reports_removed_stale_artifacts(tmp_path: Path) -> None:
    """Устаревший артефакт назван по имени при удалении."""
    root = tmp_path / "workspace"
    project = demo_project(root, {"api.listDocuments": _LIST, "multi.replaceContent": _REPLACE})
    assert project.cli("update").returncode == 0

    reduced = demo_project(root, {"api.listDocuments": _LIST})
    result = reduced.cli("update")

    assert result.returncode == 0, result.stderr
    assert "удалён устаревший артефакт: contracts/multi__replace_content.json" in result.stdout


def test_check_fails_on_hand_edited_artifact(tmp_path: Path) -> None:
    """Правка generated-файла роняет ``check`` с кодом 1 и подсказкой в stderr."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    assert project.cli("update").returncode == 0
    target = project.output_dir / "operations.py"
    target.write_text(target.read_text(encoding="utf-8") + "# правка руками\n", encoding="utf-8")

    result = project.cli("check")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "generated-артефакты разошлись со спецификацией" in result.stderr
    assert "отличается:  operations.py" in result.stderr
    assert "Запустите 'openapi-contracts update'" in result.stderr


def test_check_fails_on_deleted_artifact(tmp_path: Path) -> None:
    """Удалённый generated-файл — тоже расхождение с кодом 1."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    assert project.cli("update").returncode == 0
    (project.output_dir / "contracts" / "api__list_documents.json").unlink()

    result = project.cli("check")

    assert result.returncode == 1
    assert "отсутствует: contracts/api__list_documents.json" in result.stderr


def test_check_fails_when_spec_changed_without_update(tmp_path: Path) -> None:
    """Спецификация уехала, артефакты нет — ровно тот случай, ради которого нужен CI."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    assert project.cli("update").returncode == 0

    def relax(data: dict[str, Any]) -> None:
        data["components"]["schemas"]["DocumentBase"]["required"] = ["title"]

    edit_spec(project, "api/openapi.yaml", relax)

    result = project.cli("check")

    assert result.returncode == 1
    assert "contracts/api__list_documents.json" in result.stderr


# -------------------------------------------------------------------- diff


def test_diff_reports_no_changes(tmp_path: Path) -> None:
    """Сразу после ``update`` семантического diff нет."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    assert project.cli("update").returncode == 0

    result = project.cli("diff")

    assert result.returncode == 0, result.stderr
    assert "контракты не изменились" in result.stdout


def test_diff_reports_semantic_change(tmp_path: Path) -> None:
    """Изменение ``required`` — семантика: код 1 и явно перечисленные изменения."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    assert project.cli("update").returncode == 0

    def relax(data: dict[str, Any]) -> None:
        data["components"]["schemas"]["DocumentBase"]["required"] = ["title"]

    edit_spec(project, "api/openapi.yaml", relax)

    result = project.cli("diff")

    assert result.returncode == 1
    assert "api.listDocuments:" in result.stdout
    assert "[контракт]" in result.stdout
    assert "required" in result.stdout


def test_diff_json_shape_for_semantic_change(tmp_path: Path) -> None:
    """``--json`` отдаёт структуру с разделением semantic/cosmetic и флагом."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    assert project.cli("update").returncode == 0

    def relax(data: dict[str, Any]) -> None:
        data["components"]["schemas"]["DocumentBase"]["required"] = ["title"]

    edit_spec(project, "api/openapi.yaml", relax)

    result = project.cli("diff", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["added"] == []
    assert payload["removed"] == []
    assert payload["is_semantic"] is True
    (changed,) = payload["changed"]
    assert changed["key"] == "api.listDocuments"
    assert changed["cosmetic"] == []
    assert changed["semantic"]
    for change in changed["semantic"]:
        assert set(change) == {"kind", "pointer", "before", "after"}
        assert change["kind"] in {"added", "removed", "changed"}
    assert any("required" in change["pointer"] for change in changed["semantic"])


def test_diff_json_shape_for_cosmetic_change(tmp_path: Path) -> None:
    """Смена диалекта на эквивалентной спецификации — косметика и код 0.

    Пара ``basic/`` специально сделана семантически эквивалентной, поэтому
    переезд источника со Swagger 2.0 на OpenAPI 3.0.3 (и обратно) обязан
    выглядеть как изменение оформления, а не как поломка контракта.
    """
    root = tmp_path / "workspace"
    project = demo_project(root, {"api.listDocuments": _LIST})
    assert project.cli("update").returncode == 0

    project.write_spec("api/legacy.json", spec("basic", "swagger2.json"))
    project.write_manifest(
        sources={"main": {"path": "api/legacy.json", "selection": "explicit"}},
        operations={"api.listDocuments": _LIST},
    )

    result = project.cli("diff", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["is_semantic"] is False
    (changed,) = payload["changed"]
    assert changed["semantic"] == []
    assert [item["pointer"] for item in changed["cosmetic"]] == ["/dialect"]
    assert set(changed["cosmetic"][0]) == {"kind", "pointer"}

    human = project.cli("diff")
    assert human.returncode == 0
    assert "[оформление]" in human.stdout
    assert "[контракт]" not in human.stdout


def test_diff_reports_added_operation(tmp_path: Path) -> None:
    """Новая операция в manifest — это semantic-изменение набора контрактов."""
    root = tmp_path / "workspace"
    project = demo_project(root, {"api.listDocuments": _LIST})
    assert project.cli("update").returncode == 0

    grown = demo_project(root, {"api.listDocuments": _LIST, "api.createDocument": _CREATE})
    result = grown.cli("diff")

    assert result.returncode == 1
    assert "новая операция: api.createDocument" in result.stdout


def test_diff_reports_removed_operation(tmp_path: Path) -> None:
    """Исчезнувшая операция тоже роняет diff."""
    root = tmp_path / "workspace"
    project = demo_project(root, {"api.listDocuments": _LIST, "api.createDocument": _CREATE})
    assert project.cli("update").returncode == 0

    reduced = demo_project(root, {"api.listDocuments": _LIST})
    result = reduced.cli("diff")

    assert result.returncode == 1
    assert "операция исчезла: api.createDocument" in result.stdout


# --------------------------------------------------------------- коды возврата


def test_unknown_command_is_usage_error(tmp_path: Path) -> None:
    """Неизвестная подкоманда — код 2."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})

    assert project.cli("выгрузи-всё").returncode == 2


def test_missing_required_argument_is_usage_error(tmp_path: Path) -> None:
    """Пропущенный обязательный аргумент — код 2."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})

    assert project.cli("add", "api.createDocument").returncode == 2
    assert project.cli("init").returncode == 2


def test_missing_manifest_is_contract_error(tmp_path: Path) -> None:
    """Отсутствующий manifest — ошибка контракта (1) с внятным сообщением."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    project.manifest_path.unlink()

    for command in ("list", "update", "check", "diff"):
        result = project.cli(command)
        assert result.returncode == 1, command
        assert "Traceback" not in result.stderr, command

    result = project.cli("add", "api.createDocument", "--source", "main")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_broken_manifest_is_contract_error(tmp_path: Path) -> None:
    """Неразбираемый manifest — код 1, без traceback."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})
    project.manifest_path.write_text("version: [1\n", encoding="utf-8")

    result = project.cli("list")

    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_binding_drift_is_contract_error(tmp_path: Path) -> None:
    """``operationId`` уехал из спецификации — код 1 и объяснение."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})

    def rename(data: dict[str, Any]) -> None:
        path = data["paths"]["/api/v1/workspaces/{workspaceId}/documents"]
        path["get"]["operationId"] = "listWorkspaceDocuments"

    edit_spec(project, "api/openapi.yaml", rename)

    result = project.cli("update")

    assert result.returncode == 1
    assert "listDocuments" in result.stderr


def test_version_flag_exits_zero(tmp_path: Path) -> None:
    """``--version`` печатает версию и завершается успехом."""
    project = demo_project(tmp_path / "workspace", {"api.listDocuments": _LIST})

    result = project.cli("--version")

    assert result.returncode == 0
    assert "openapi-contracts" in result.stdout

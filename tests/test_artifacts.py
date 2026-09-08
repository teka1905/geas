"""Generated-артефакты: детерминированность, учёт owned-файлов, drift и безопасность записи.

Проверяется три обещания модуля :mod:`openapi_contracts.artifacts`:

1. **детерминированность** — повторный рендер даёт байт-в-байт тот же результат,
   в тексте нет ни времени, ни абсолютных путей, ни случайных значений, а
   результат не зависит от ``PYTHONHASHSEED``;
2. **учёт своих файлов** — генератор удаляет только то, что сам когда-то записал,
   и видит расхождение рабочего дерева с тем, что даёт спецификация;
3. **безопасность записи** — ``..``, абсолютный путь и symlink на месте артефакта
   останавливают запись ошибкой, а не «пишутся насквозь».

Все проекты-потребители поднимаются во временном каталоге помощниками из
``tests/support.py``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from openapi_contracts.artifacts import (
    ARTIFACT_FORMAT_VERSION,
    GENERATED_INDEX,
    Artifact,
    ArtifactSet,
    check_artifacts,
    read_generated_index,
    render_artifacts,
    write_artifacts,
)
from openapi_contracts.errors import ArtifactError, NamespaceCollisionError
from support import SRC_ROOT, Project, make_project, spec

# Источники, из которых собираются демо-проекты: имя в manifest → (путь внутри
# проекта, фикстура). Оба файла синтетические, домен вымышленный.
_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "main": ("api/openapi.yaml", ("basic", "openapi30.yaml")),
    "multi": ("api/multi.yaml", ("features", "multi_content_types.yaml")),
}

# Операции из basic/ идут с ``d42: false``: схема DocumentBase.labels — это
# типизированный additionalProperties, который d42 выразить не умеет, и генерация
# d42 для неё осознанно падает. Здесь проверяются артефакты, а не d42-конвертер.
_LIST = {"source": "main", "operation_id": "listDocuments", "d42": False}
_CREATE = {"source": "main", "operation_id": "createDocument", "d42": False}
# Единственная операция с включённым d42 — у неё простые тела без exotics.
_REPLACE = {"source": "multi", "operation_id": "replaceDocumentContent"}

_FULL_OPERATIONS = {
    "api.createDocument": _CREATE,
    "api.listDocuments": _LIST,
    "multi.replaceContent": _REPLACE,
}


def demo_project(root: Path, operations: Mapping[str, Any]) -> Project:
    """Проект-потребитель с ровно теми источниками, на которые ссылаются операции.

    Лишний explicit-источник без операций manifest считает опечаткой, поэтому
    список источников выводится из самих операций, а не задаётся отдельно.
    """
    project = make_project(root)
    sources: dict[str, Any] = {}
    for name in sorted({str(item["source"]) for item in operations.values()}):
        relative, fixture = _SOURCES[name]
        project.write_spec(relative, spec(*fixture))
        sources[name] = {"path": relative, "selection": "explicit"}
    project.write_manifest(sources=sources, operations=operations)
    return project


def contents(artifacts: ArtifactSet) -> list[tuple[str, str]]:
    """Набор артефактов как сравнимый список пар «путь, содержимое»."""
    return [(item.path, item.content) for item in artifacts.files]


def symlink_or_skip(link: Path, target: Path) -> None:
    """Создать symlink либо пропустить тест там, где ОС этого не разрешает."""
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - только Windows
        pytest.skip(f"ОС не разрешает создавать символические ссылки: {exc}")


# --------------------------------------------------------- детерминированность


def test_render_artifacts_twice_is_byte_identical(tmp_path: Path) -> None:
    """Два рендера подряд дают один и тот же набор байт."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)

    first = project.render()
    second = project.render()

    assert contents(first) == contents(second)


def test_render_artifacts_does_not_depend_on_hash_seed(tmp_path: Path) -> None:
    """Рендер не зависит от ``PYTHONHASHSEED``.

    Порядок обхода множеств и словарей — самый частый источник «почти
    детерминированного» вывода: в одном процессе он стабилен, а между запусками
    плывёт. Поэтому рендер выполняется в трёх подпроцессах с разными сидами.
    """
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    code = (
        "import json\n"
        "from openapi_contracts.artifacts import render_artifacts\n"
        "from openapi_contracts.contracts import build_contracts\n"
        "from openapi_contracts.manifest import load_manifest\n"
        "from openapi_contracts.waivers import load_waivers\n"
        f"manifest = load_manifest({str(project.manifest_path)!r})\n"
        f"waivers = load_waivers({str(project.waivers_path)!r})\n"
        "artifacts = render_artifacts(manifest, build_contracts(manifest, waivers))\n"
        "print(json.dumps([[i.path, i.content] for i in artifacts.files]))\n"
    )

    renders = set()
    for seed in ("0", "1", "424242"):
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(SRC_ROOT), "PYTHONHASHSEED": seed},
        )
        assert completed.returncode == 0, completed.stderr
        renders.add(completed.stdout)

    assert len(renders) == 1


def test_update_twice_leaves_check_clean(tmp_path: Path) -> None:
    """Повторный ``update`` ничего не меняет и не ломает ``check``."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)

    project.update()
    snapshot = {
        path.relative_to(project.output_dir).as_posix(): path.read_bytes()
        for path in sorted(project.output_dir.rglob("*"))
        if path.is_file()
    }
    project.update()
    again = {
        path.relative_to(project.output_dir).as_posix(): path.read_bytes()
        for path in sorted(project.output_dir.rglob("*"))
        if path.is_file()
    }

    assert again == snapshot
    assert project.check().is_clean


def test_rendered_text_has_no_time_paths_or_random_ids(tmp_path: Path) -> None:
    """В тексте артефактов нет ни дат, ни времени, ни абсолютных путей, ни uuid."""
    root = tmp_path / "workspace"
    project = demo_project(root, _FULL_OPERATIONS)

    blob = "\n".join(item.content for item in project.render().files)

    assert not re.search(r"\d{4}-\d{2}-\d{2}", blob), "похоже на дату"
    assert not re.search(r"\d{2}:\d{2}:\d{2}", blob), "похоже на время"
    assert not re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", blob
    ), "похоже на uuid"
    for absolute in (root, root.resolve(), project.output_dir, SRC_ROOT, Path.home()):
        assert str(absolute) not in blob, f"в артефакты утёк абсолютный путь {absolute}"


def test_every_artifact_ends_with_exactly_one_newline(tmp_path: Path) -> None:
    """Каждый файл заканчивается ровно одним ``\\n`` и не содержит ``\\r``."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)

    for item in project.render().files:
        assert item.content.endswith("\n"), f"{item.path} без завершающего перевода строки"
        assert not item.content.endswith("\n\n"), f"{item.path} с пустой строкой в конце"
        assert "\r" not in item.content, f"{item.path} содержит CR"


def test_written_files_use_lf_on_disk(tmp_path: Path) -> None:
    """На диск файлы попадают с LF, а не с переводом строки платформы."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    project.update()

    for path in sorted(project.output_dir.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        assert b"\r" not in raw, f"{path} записан не с LF"
        assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")


def test_no_temporary_files_left_after_update(tmp_path: Path) -> None:
    """Атомарная запись не оставляет за собой ``.tmp``-файлов."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    project.update()

    leftovers = [path.name for path in project.output_dir.rglob("*.tmp")]
    assert leftovers == []


# --------------------------------------------------------------- owned-файлы


def test_generated_index_lists_owned_and_digests(tmp_path: Path) -> None:
    """``_generated.json`` описывает набор: owned, digests, отпечатки операций."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    artifacts = project.update()

    index = project.generated_index()

    assert index["artifact_format"] == ARTIFACT_FORMAT_VERSION
    assert tuple(index["owned"]) == artifacts.paths()
    assert GENERATED_INDEX in index["owned"]
    # digests описывают всё, кроме самого индекса: его отпечаток внутри него же
    # не имел бы смысла.
    assert set(index["digests"]) == set(artifacts.paths()) - {GENERATED_INDEX}
    for item in artifacts.files:
        if item.path == GENERATED_INDEX:
            continue
        assert index["digests"][item.path] == item.digest
    assert set(index["operations"]) == set(_FULL_OPERATIONS)
    assert index["output"] == {
        "directory": "demo_contracts/generated",
        "package": "demo_contracts.generated",
    }


def test_update_deletes_stale_artifacts_and_reports_them(tmp_path: Path) -> None:
    """Операция ушла из manifest — её contract- и d42-файлы удаляются и называются."""
    root = tmp_path / "workspace"
    project = demo_project(root, _FULL_OPERATIONS)
    project.update()
    stale = [
        project.output_dir / "contracts" / "multi__replace_content.json",
        project.output_dir / "_d42" / "multi__replace_content_request.py",
        project.output_dir / "_d42" / "multi__replace_content_response.py",
    ]
    assert all(path.is_file() for path in stale)

    reduced = demo_project(root, {"api.createDocument": _CREATE, "api.listDocuments": _LIST})
    removed = write_artifacts(reduced.output_dir, reduced.render())

    assert not any(path.exists() for path in stale)
    assert "contracts/multi__replace_content.json" in removed
    assert "_d42/multi__replace_content_request.py" in removed
    assert "_d42/multi__replace_content_response.py" in removed
    # d42 больше не нужен ни одной операции: каталог уезжает целиком.
    assert "_d42/__init__.py" in removed
    assert not (reduced.output_dir / "_d42").exists()
    assert reduced.check().is_clean


def test_update_leaves_foreign_files_alone(tmp_path: Path) -> None:
    """Файл, который генератор не создавал, не удаляется и не переписывается."""
    root = tmp_path / "workspace"
    project = demo_project(root, _FULL_OPERATIONS)
    project.update()
    foreign = project.output_dir / "NOTES.md"
    foreign.write_text("рукописный файл рядом с артефактами\n", encoding="utf-8")
    foreign_nested = project.output_dir / "contracts" / "README.md"
    foreign_nested.write_text("и ещё один\n", encoding="utf-8")

    reduced = demo_project(root, {"api.createDocument": _CREATE, "api.listDocuments": _LIST})
    removed = write_artifacts(reduced.output_dir, reduced.render())

    assert foreign.read_text(encoding="utf-8") == "рукописный файл рядом с артефактами\n"
    assert foreign_nested.read_text(encoding="utf-8") == "и ещё один\n"
    assert "NOTES.md" not in removed
    assert "contracts/README.md" not in removed


def test_read_generated_index_on_empty_directory(tmp_path: Path) -> None:
    """Отсутствие ``_generated.json`` — это пустой набор, а не ошибка."""
    assert read_generated_index(tmp_path / "nowhere") == {}


def test_read_generated_index_rejects_broken_json(tmp_path: Path) -> None:
    """Повреждённый индекс — явная ошибка с именем файла."""
    output = tmp_path / "generated"
    output.mkdir()
    (output / GENERATED_INDEX).write_text("{не json", encoding="utf-8")

    with pytest.raises(ArtifactError, match="повреждён"):
        read_generated_index(output)


# ------------------------------------------------------------------- drift


def test_check_detects_modified_file(tmp_path: Path) -> None:
    """Правка руками видна как ``отличается``."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    project.update()
    target = project.output_dir / "operations.py"
    target.write_text(target.read_text(encoding="utf-8") + "# правка руками\n", encoding="utf-8")

    report = project.check()

    assert not report.is_clean
    assert report.changed == ("operations.py",)
    assert report.missing == ()
    assert report.extra == ()
    assert "отличается" in report.describe()


def test_check_detects_deleted_file(tmp_path: Path) -> None:
    """Удалённый артефакт виден как ``отсутствует``."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    project.update()
    (project.output_dir / "contracts" / "api__list_documents.json").unlink()

    report = project.check()

    assert not report.is_clean
    assert report.missing == ("contracts/api__list_documents.json",)
    assert report.changed == ()
    assert "отсутствует" in report.describe()


def test_check_detects_extra_owned_file(tmp_path: Path) -> None:
    """Файл, который прошлый прогон записал, а новый не производит, — лишний."""
    root = tmp_path / "workspace"
    project = demo_project(root, _FULL_OPERATIONS)
    project.update()

    reduced = demo_project(root, {"api.createDocument": _CREATE, "api.listDocuments": _LIST})
    report = reduced.check()

    assert not report.is_clean
    assert "contracts/multi__replace_content.json" in report.extra
    assert "_d42/multi__replace_content_request.py" in report.extra
    assert "лишний" in report.describe()


def test_check_detects_artifact_format_mismatch(tmp_path: Path) -> None:
    """Чужая версия формата в ``_generated.json`` — отдельный вид расхождения."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    project.update()
    index_path = project.output_dir / GENERATED_INDEX
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["artifact_format"] = ARTIFACT_FORMAT_VERSION + 41
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    report = project.check()

    assert not report.is_clean
    assert report.format_mismatch is not None
    assert str(ARTIFACT_FORMAT_VERSION + 41) in report.format_mismatch
    assert "версия формата артефактов" in report.describe()


def test_check_is_clean_right_after_update(tmp_path: Path) -> None:
    """Свежесгенерированное дерево расхождений не даёт."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    project.update()

    report = project.check()

    assert report.is_clean
    assert report.describe() == ""


# --------------------------------------------------------------- безопасность


def test_write_rejects_parent_traversal(tmp_path: Path) -> None:
    """``..`` в пути артефакта останавливает запись."""
    output = tmp_path / "generated"
    outside = tmp_path / "escaped.py"
    artifacts = ArtifactSet(files=(Artifact(path="../escaped.py", content="x = 1\n"),))

    with pytest.raises(ArtifactError, match="недопустимый путь"):
        write_artifacts(output, artifacts)

    assert not outside.exists()


def test_write_rejects_nested_parent_traversal(tmp_path: Path) -> None:
    """``..`` в середине пути ловится так же, как в начале."""
    output = tmp_path / "generated"
    artifacts = ArtifactSet(files=(Artifact(path="contracts/../../escaped.py", content="x\n"),))

    with pytest.raises(ArtifactError, match="недопустимый путь"):
        write_artifacts(output, artifacts)

    assert not (tmp_path / "escaped.py").exists()


def test_write_rejects_absolute_path(tmp_path: Path) -> None:
    """Абсолютный путь артефакта — тоже ошибка, а не запись куда попало."""
    output = tmp_path / "generated"
    outside = tmp_path / "absolute.py"
    artifacts = ArtifactSet(files=(Artifact(path=str(outside), content="x = 1\n"),))

    with pytest.raises(ArtifactError, match="недопустимый путь"):
        write_artifacts(output, artifacts)

    assert not outside.exists()


def test_write_rejects_symlink_at_artifact_location(tmp_path: Path) -> None:
    """Symlink на месте артефакта не переписывается насквозь."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    victim = tmp_path / "victim.py"
    victim.write_text("важное содержимое\n", encoding="utf-8")
    project.output_dir.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(project.output_dir / "operations.py", victim)

    with pytest.raises(ArtifactError, match="символическая ссылка"):
        project.update()

    assert victim.read_text(encoding="utf-8") == "важное содержимое\n"


def test_write_rejects_symlinked_directory(tmp_path: Path) -> None:
    """Подкаталог-symlink выводит запись за каталог артефактов и потому запрещён."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    outside = tmp_path / "outside"
    outside.mkdir()
    project.output_dir.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(project.output_dir / "contracts", outside)

    with pytest.raises(ArtifactError, match="выводит за каталог артефактов"):
        project.update()

    assert list(outside.iterdir()) == []


def test_write_refuses_to_delete_through_symlink(tmp_path: Path) -> None:
    """Устаревший owned-файл, подменённый ссылкой, не удаляется вместе с целью."""
    root = tmp_path / "workspace"
    project = demo_project(root, _FULL_OPERATIONS)
    project.update()
    stale = project.output_dir / "contracts" / "multi__replace_content.json"
    victim = tmp_path / "victim.json"
    victim.write_text("{}\n", encoding="utf-8")
    stale.unlink()
    symlink_or_skip(stale, victim)

    reduced = demo_project(root, {"api.createDocument": _CREATE, "api.listDocuments": _LIST})
    with pytest.raises(ArtifactError, match="символическая ссылка"):
        write_artifacts(reduced.output_dir, reduced.render())

    assert victim.is_file()
    assert victim.read_text(encoding="utf-8") == "{}\n"


# --------------------------------------------------------- generated namespace


def test_operations_module_is_valid_python_and_importable(tmp_path: Path) -> None:
    """``operations.py`` компилируется, импортируется и отдаёт ручки операций."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    project.update()
    source = (project.output_dir / "operations.py").read_text(encoding="utf-8")
    compile(source, "operations.py", "exec")

    with project.importable() as module:
        operations = module.operations
        assert operations.keys() == tuple(sorted(_FULL_OPERATIONS))
        assert operations.api.create_document.key == "api.createDocument"
        assert operations.api.list_documents.key == "api.listDocuments"
        assert operations.multi.replace_content.key == "multi.replaceContent"
        # Доступ к операции — property на классе namespace, а не атрибут
        # экземпляра: тип виден IDE, документ читается лениво.
        namespace = type(operations.api)
        assert isinstance(namespace.__dict__["create_document"], property)
        assert isinstance(type(operations).__dict__["api"], property)
        assert operations.by_key("api.createDocument").key == "api.createDocument"


def test_nested_namespace_of_three_segments(tmp_path: Path) -> None:
    """Трёхсегментный ``python_path`` разворачивается во вложенные namespace."""
    operations = {
        "api.createDocument": {**_CREATE, "python_path": ["api", "documents", "create"]},
        "api.listDocuments": {**_LIST, "python_path": ["api", "documents", "listing"]},
    }
    project = demo_project(tmp_path / "workspace", operations)
    project.update()

    source = (project.output_dir / "operations.py").read_text(encoding="utf-8")
    compile(source, "operations.py", "exec")
    assert "class _NsApi_Documents:" in source

    with project.importable() as module:
        assert module.operations.api.documents.create.key == "api.createDocument"
        assert module.operations.api.documents.listing.key == "api.listDocuments"


def test_namespace_class_names_do_not_collide(tmp_path: Path) -> None:
    """Регрессия: ``a_b.c`` и ``a.b_c`` — разные namespace и разные классы.

    ``to_pascal_case`` вычищает подчёркивания, поэтому без разделителя оба пути
    давали класс ``_NsABC``, второе определение затирало первое, и половина
    операций молча исчезала из generated namespace.
    """
    operations = {
        "api.createDocument": {**_CREATE, "python_path": ["a", "b_c", "creating"]},
        "api.listDocuments": {**_LIST, "python_path": ["a_b", "c", "listing"]},
    }
    project = demo_project(tmp_path / "workspace", operations)
    project.update()

    source = (project.output_dir / "operations.py").read_text(encoding="utf-8")
    assert source.count("class _NsA_BC:") == 1
    assert source.count("class _NsAB_C:") == 1

    with project.importable() as module:
        assert module.operations.a.b_c.creating.key == "api.createDocument"
        assert module.operations.a_b.c.listing.key == "api.listDocuments"


def test_unresolvable_class_name_collision_is_an_error(tmp_path: Path) -> None:
    """Остаточная неоднозначность имени класса — ошибка генерации, а не тихая потеря.

    ``to_pascal_case`` не инъективен: ``aB`` и ``a_b`` дают одно ``AB``. Такой
    manifest встречается разве что случайно, но артефакты из него собирать нельзя.
    """
    operations = {
        "api.createDocument": {**_CREATE, "python_path": ["aB", "creating"]},
        "api.listDocuments": {**_LIST, "python_path": ["a_b", "listing"]},
    }
    project = demo_project(tmp_path / "workspace", operations)

    with pytest.raises(NamespaceCollisionError, match="один класс"):
        project.render()


def test_empty_manifest_renders_importable_namespace(tmp_path: Path) -> None:
    """Проект без операций — это валидный пустой namespace, а не сломанный модуль."""
    project = make_project(tmp_path / "workspace")
    relative, fixture = _SOURCES["main"]
    project.write_spec(relative, spec(*fixture))
    project.write_manifest(
        sources={"main": {"path": relative, "selection": "explicit"}}, operations={}
    )
    project.update()

    compile(
        (project.output_dir / "operations.py").read_text(encoding="utf-8"), "operations.py", "exec"
    )
    with project.importable() as module:
        assert module.operations.keys() == ()
    assert not (project.output_dir / "_d42").exists()
    assert project.check().is_clean


def test_registry_module_maps_keys_to_contract_files(tmp_path: Path) -> None:
    """``_registry.py`` держит индекс «ключ → слаг», и все файлы на месте."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)
    project.update()

    with project.importable() as module:
        index = module.REGISTRY  # реестр реэкспортируется из пакета
        assert index.keys() == tuple(sorted(_FULL_OPERATIONS))

    for slug in ("api__create_document", "api__list_documents", "multi__replace_content"):
        assert (project.output_dir / "contracts" / f"{slug}.json").is_file()
        assert project.contract_document(slug)["slug"] == slug


def test_render_artifacts_returns_sorted_paths(tmp_path: Path) -> None:
    """Порядок файлов в наборе стабилен и отсортирован — от него зависят диффы."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)

    artifacts = project.render()

    paths = [item.path for item in artifacts.files]
    assert paths == sorted(paths)
    assert artifacts.paths() == tuple(sorted(paths))
    assert set(artifacts.by_path()) == set(paths)


def test_check_reports_everything_missing_on_empty_tree(tmp_path: Path) -> None:
    """Если артефактов ещё нет, ``check`` называет весь набор отсутствующим."""
    project = demo_project(tmp_path / "workspace", _FULL_OPERATIONS)

    artifacts = project.render()
    report = check_artifacts(project.output_dir, artifacts)

    assert report.missing == artifacts.paths()
    assert report.format_mismatch is None
    assert not report.is_clean


def test_render_artifacts_matches_manifest_output_settings(tmp_path: Path) -> None:
    """Индекс запоминает каталог и пакет вывода — по ним потребитель себя находит."""
    project = make_project(tmp_path / "workspace", package="other_pkg")
    relative, fixture = _SOURCES["main"]
    project.write_spec(relative, spec(*fixture))
    project.write_manifest(
        sources={"main": {"path": relative, "selection": "explicit"}},
        operations={"api.listDocuments": _LIST},
    )

    project.update()

    index = project.generated_index()
    assert index["output"] == {
        "directory": "other_pkg/generated",
        "package": "other_pkg.generated",
    }
    assert render_artifacts(project.load(), project.build()).paths() == tuple(index["owned"])

"""Семантический diff: что считается изменением контракта, а что — оформлением.

Модуль :mod:`openapi_contracts.semantic_diff` отвечает на два вопроса:

* ``diff_operations`` — что именно разошлось между двумя наборами документов и
  меняет ли это контракт (``semantic``) или только оформление (``cosmetic``);
* ``semantic_fingerprint`` — отпечаток, который обязан молчать на правке
  ``description``/``example`` и обязан меняться, когда меняется ``required``.

Отпечаток и классификация проверяются не на выдуманных словарях, а на документах,
собранных из настоящих фикстур: пара ``basic/`` специально сделана семантически
эквивалентной в двух диалектах, поэтому разница между ней — ровно косметическая.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from openapi_contracts.semantic_diff import (
    Change,
    ChangeKind,
    diff_documents,
    diff_operations,
    semantic_fingerprint,
    semantic_view,
)
from support import Project, make_project, spec

#: Ключ операции, на которой гоняется большинство проверок.
KEY = "api.createDocument"

# ``d42: false`` оставляет тест сфокусированным на документах контрактов, а не
# на generated d42-модулях.
_OPERATION = {"source": "main", "operation_id": "createDocument", "d42": False}


def build_project(root: Path, fixture: tuple[str, str], *, base_path: str | None = None) -> Project:
    """Проект из одной фикстуры и одной операции."""
    project = make_project(root)
    relative = f"api/spec{Path(fixture[-1]).suffix}"
    project.write_spec(relative, spec(*fixture))
    source: dict[str, Any] = {"path": relative, "selection": "explicit"}
    if base_path is not None:
        source["base_path"] = base_path
    project.write_manifest(sources={"main": source}, operations={KEY: _OPERATION})
    return project


def document(project: Project) -> dict[str, Any]:
    """Канонический документ контракта операции :data:`KEY`."""
    return project.build().by_key(KEY).document


def edit_spec(project: Project, relative: str, mutate: Any) -> None:
    """Прочитать спецификацию проекта, изменить её через ``mutate`` и записать назад."""
    path = project.root / relative
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


# ------------------------------------------------------- наборы: added/removed


def test_added_and_removed_operations() -> None:
    """Ключ появился или исчез — это отдельные списки, а не «изменение»."""
    before = {"a.one": {"key": "a.one"}, "a.two": {"key": "a.two"}}
    after = {"a.two": {"key": "a.two"}, "a.three": {"key": "a.three"}}

    diff = diff_operations(before, after)

    assert diff.added == ("a.three",)
    assert diff.removed == ("a.one",)
    assert diff.changed == ()
    assert diff.is_semantic
    assert not diff.is_empty


def test_identical_sets_produce_empty_diff() -> None:
    """Одинаковые наборы дают пустой diff."""
    documents = {"a.one": {"key": "a.one", "method": "GET"}}

    diff = diff_operations(documents, dict(documents))

    assert diff.is_empty
    assert not diff.is_semantic
    assert diff.changed == ()


def test_changed_operation_is_listed_with_pointer() -> None:
    """Изменённое значение приходит с JSON-указателем и обеими сторонами."""
    before = {KEY: {"key": KEY, "method": "GET"}}
    after = {KEY: {"key": KEY, "method": "POST"}}

    diff = diff_operations(before, after)

    assert diff.is_semantic
    assert len(diff.changed) == 1
    (change,) = diff.changed[0].semantic
    assert change.kind is ChangeKind.CHANGED
    assert change.pointer == "/method"
    assert (change.before, change.after) == ("GET", "POST")


def test_added_and_removed_keys_inside_document() -> None:
    """Внутри документа появление и исчезновение ключа различаются по виду."""
    before = {KEY: {"key": KEY, "gone": 1}}
    after = {KEY: {"key": KEY, "fresh": 2}}

    (item,) = diff_operations(before, after).changed

    kinds = {change.pointer: change.kind for change in item.semantic}
    assert kinds == {"/gone": ChangeKind.REMOVED, "/fresh": ChangeKind.ADDED}


def test_list_length_change_is_reported_per_index() -> None:
    """Список сравнивается поэлементно, а лишние элементы называются по индексу."""
    before = {KEY: {"responses": [{"status": 200}, {"status": 400}]}}
    after = {KEY: {"responses": [{"status": 200}]}}

    (item,) = diff_operations(before, after).changed

    assert [change.pointer for change in item.semantic] == ["/responses/1"]
    assert item.semantic[0].kind is ChangeKind.REMOVED


def test_pointer_escapes_slash_and_tilde() -> None:
    """Имена свойств со слэшем и тильдой экранируются по RFC 6901."""
    before = {"schema": {"properties": {"a/b": {"type": "string"}, "c~d": {"type": "string"}}}}
    after = {"schema": {"properties": {"a/b": {"type": "integer"}, "c~d": {"type": "integer"}}}}

    pointers = [change.pointer for change in diff_documents(before, after, KEY).semantic]

    assert pointers == ["/schema/properties/a~1b/type", "/schema/properties/c~0d/type"]


# ---------------------------------------------- семантика против оформления


@pytest.mark.parametrize("cosmetic_key", ["artifact", "slug", "dialect", "d42"])
def test_non_semantic_keys_are_classified_as_cosmetic(cosmetic_key: str) -> None:
    """Изменение под несемантическим корнем не трогает контракт."""
    before = {"key": KEY, cosmetic_key: {"value": "было"}}
    after = {"key": KEY, cosmetic_key: {"value": "стало"}}

    item = diff_documents(before, after, KEY)

    assert item.semantic == ()
    assert len(item.cosmetic) == 1
    assert item.cosmetic[0].pointer == f"/{cosmetic_key}/value"
    assert not item.is_semantic


def test_schema_change_is_classified_as_semantic() -> None:
    """Изменение схемы — это изменение контракта."""
    before = {"responses": [{"schema": {"type": "string"}}]}
    after = {"responses": [{"schema": {"type": "integer"}}]}

    item = diff_documents(before, after, KEY)

    assert item.cosmetic == ()
    assert [change.pointer for change in item.semantic] == ["/responses/0/schema/type"]
    assert item.is_semantic


def test_semantic_and_cosmetic_changes_are_separated() -> None:
    """Смешанный diff раскладывается на две корзины, и семантика перевешивает."""
    before = {"slug": "old_slug", "method": "GET"}
    after = {"slug": "new_slug", "method": "POST"}

    item = diff_documents(before, after, KEY)

    assert [change.pointer for change in item.cosmetic] == ["/slug"]
    assert [change.pointer for change in item.semantic] == ["/method"]
    assert item.is_semantic


def test_dialect_switch_is_cosmetic_on_equivalent_specs(tmp_path: Path) -> None:
    """Пара ``basic/`` эквивалентна: смена диалекта — чистая косметика.

    Это самая честная проверка классификации: документы собираются из двух разных
    файлов (Swagger 2.0 и OpenAPI 3.0.3), описывающих одну и ту же API, и
    единственное различие между ними обязано быть несемантическим.
    """
    modern = build_project(tmp_path / "openapi30", ("basic", "openapi30.yaml"))
    legacy = build_project(tmp_path / "swagger2", ("basic", "swagger2.json"))

    diff = diff_operations({KEY: document(modern)}, {KEY: document(legacy)})

    assert not diff.is_semantic
    assert not diff.is_empty
    (item,) = diff.changed
    assert item.semantic == ()
    assert [change.pointer for change in item.cosmetic] == ["/dialect"]
    assert (item.cosmetic[0].before, item.cosmetic[0].after) == ("openapi30", "swagger2")


def test_fingerprint_matches_on_equivalent_specs(tmp_path: Path) -> None:
    """У эквивалентной пары спецификаций совпадает и отпечаток."""
    modern = build_project(tmp_path / "openapi30", ("basic", "openapi30.yaml"))
    legacy = build_project(tmp_path / "swagger2", ("basic", "swagger2.json"))

    assert semantic_fingerprint(document(modern)) == semantic_fingerprint(document(legacy))


# ------------------------------------------------------------------ отпечаток


def test_semantic_view_drops_non_semantic_keys() -> None:
    """``semantic_view`` оставляет только то, что влияет на контракт."""
    view = semantic_view(
        {
            "key": KEY,
            "method": "POST",
            "artifact": {"kind": "operation-contract"},
            "slug": "api__create_document",
            "dialect": "openapi30",
            "d42": {"enabled": True},
        }
    )

    assert view == {"key": KEY, "method": "POST"}


def test_fingerprint_ignores_non_semantic_keys() -> None:
    """Слаг, диалект и d42-настройки на отпечаток не влияют."""
    base = {"key": KEY, "method": "POST"}

    assert semantic_fingerprint(base) == semantic_fingerprint(
        {**base, "slug": "что угодно", "dialect": "swagger2", "d42": {"enabled": False}}
    )


def test_fingerprint_ignores_key_order() -> None:
    """Отпечаток считается по канонической форме, а не по порядку ключей."""
    assert semantic_fingerprint({"a": 1, "b": 2}) == semantic_fingerprint({"b": 2, "a": 1})


def test_fingerprint_stable_across_description_edits(tmp_path: Path) -> None:
    """Правка ``description``/``example`` в спецификации отпечаток не двигает."""
    project = build_project(tmp_path / "workspace", ("basic", "openapi30.yaml"))
    before = document(project)

    def annotate(data: dict[str, Any]) -> None:
        schemas = data["components"]["schemas"]
        title = schemas["DocumentBase"]["properties"]["title"]
        title["description"] = "Заголовок документа"
        title["example"] = "Черновик отчёта"
        schemas["Member"]["description"] = "Участник рабочего пространства"
        schemas["DocumentPage"]["properties"]["total"]["example"] = 42
        data["info"]["title"] = "Совсем другое название"

    edit_spec(project, "api/spec.yaml", annotate)
    after = document(project)

    assert semantic_fingerprint(after) == semantic_fingerprint(before)
    assert diff_operations({KEY: before}, {KEY: after}).is_empty


def test_fingerprint_changes_when_required_changes(tmp_path: Path) -> None:
    """Убрали поле из ``required`` — отпечаток обязан измениться."""
    project = build_project(tmp_path / "workspace", ("basic", "openapi30.yaml"))
    before = document(project)

    def relax(data: dict[str, Any]) -> None:
        data["components"]["schemas"]["DocumentBase"]["required"] = ["title"]

    edit_spec(project, "api/spec.yaml", relax)
    after = document(project)

    assert semantic_fingerprint(after) != semantic_fingerprint(before)
    diff = diff_operations({KEY: before}, {KEY: after})
    assert diff.is_semantic
    (item,) = diff.changed
    assert item.cosmetic == ()
    assert any("/required" in change.pointer for change in item.semantic)


def test_fingerprint_changes_when_property_type_changes(tmp_path: Path) -> None:
    """Смена типа свойства — тоже изменение контракта."""
    project = build_project(tmp_path / "workspace", ("basic", "openapi30.yaml"))
    before = document(project)

    def retype(data: dict[str, Any]) -> None:
        properties = data["components"]["schemas"]["DocumentBase"]["properties"]
        properties["title"] = {"type": "integer", "minimum": 1}

    edit_spec(project, "api/spec.yaml", retype)
    after = document(project)

    assert semantic_fingerprint(after) != semantic_fingerprint(before)
    assert diff_operations({KEY: before}, {KEY: after}).is_semantic


# -------------------------------------------------------------- представление


def test_change_describe_formats_every_kind() -> None:
    """Строковое описание изменения читаемо и однозначно по знаку."""
    added = Change(kind=ChangeKind.ADDED, pointer="/request", after={"b": 1})
    removed = Change(kind=ChangeKind.REMOVED, pointer="/responses/0", before=[1, 2])
    changed = Change(kind=ChangeKind.CHANGED, pointer="/method", before="GET", after="POST")

    assert added.describe() == '+ /request = {"b":1}'
    assert removed.describe() == "- /responses/0 = [1,2]"
    assert changed.describe() == '~ /method: "GET" → "POST"'


def test_root_level_replacement_is_reported_as_single_change() -> None:
    """Несовместимые по типу документы дают одно изменение в корне."""
    item = diff_documents({"a": 1}, [1, 2, 3], KEY)  # type: ignore[arg-type]

    assert [change.pointer for change in item.semantic] == ["/"]
    assert item.semantic[0].kind is ChangeKind.CHANGED

"""Слэш на конце маршрута: явная настройка ``accept_trailing_slash``.

По умолчанию маршрут сопоставляется строго. Настройка источника или операции
разрешает ровно один необязательный завершающий слэш, не трогая остальные
маршруты. Проверяются все три места, где маршрут сопоставляется: разбор
перехваченного запроса, JJ-матчер ``ContractMock`` и drift-guard manifest.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from geas.errors import ManifestError, RequestContractError
from geas.manifest import Manifest, OperationSpec, SourceSpec, TemporarySetting
from geas.runtime.operation import (
    extract_path_params,
    route_templates_overlap,
    route_variants,
)
from jj_server import JJServer, jj_server  # noqa: F401 - сессионная фикстура
from support import Project, make_project

SPEC = """\
openapi: "3.0.3"

info:
  title: Slash API
  version: "1.0.0"

paths:
  /search/tree:
    post:
      operationId: loadTree
      responses:
        "204":
          description: Готово
  /items/{itemId}:
    get:
      operationId: getItem
      parameters:
        - name: itemId
          in: path
          required: true
          schema:
            type: integer
        - name: expand
          in: query
          schema:
            type: string
        - name: X-Trace
          in: header
          schema:
            type: string
      responses:
        "204":
          description: Готово
  /declared/:
    post:
      operationId: declared
      responses:
        "204":
          description: Готово
  /strict:
    get:
      operationId: strictRoute
      responses:
        "204":
          description: Готово
  /overridden:
    get:
      operationId: overridden
      responses:
        "204":
          description: Готово
"""


def build_project(root: Path, *, package: str) -> Project:
    project = make_project(root, package=package)
    project.write_spec("api/lax.yaml", SPEC)
    project.write_spec("api/strict.yaml", SPEC)
    project.write_manifest(
        sources={
            "lax": {
                "path": "api/lax.yaml",
                "selection": "all",
                "root": "api",
                "accept_trailing_slash": True,
            },
            "strict": {"path": "api/strict.yaml", "selection": "all", "root": "api"},
        },
        operations={
            "lax.loadTree": {"source": "lax", "operation_id": "loadTree", "d42": False},
            "lax.strictRoute": {
                "source": "lax",
                "operation_id": "strictRoute",
                "accept_trailing_slash": False,
            },
            "strict.overridden": {
                "source": "strict",
                "operation_id": "overridden",
                "accept_trailing_slash": True,
            },
        },
    )
    project.update()
    return project


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    return build_project(tmp_path_factory.mktemp("slash"), package="slash_contracts")


@pytest.fixture(scope="module")
def operations(project: Project) -> Iterator[Any]:
    with project.importable() as module:
        yield module.operations


def recorded(path: str, **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "method": "POST",
        "path": path,
        "segments": {},
        "params": [],
        "headers": [],
        "body": None,
        "index": 0,
    }
    request.update(overrides)
    return request


# ------------------------------------------------------------------ manifest


def test_default_is_strict() -> None:
    assert SourceSpec.from_dict("main", {"path": "a.yaml"}).accept_trailing_slash is False
    assert OperationSpec.from_dict("a.b", {"source": "main"}).accept_trailing_slash is None


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_source_setting_must_be_boolean(value: Any) -> None:
    with pytest.raises(ManifestError, match="accept_trailing_slash"):
        SourceSpec.from_dict("main", {"path": "a.yaml", "accept_trailing_slash": value})


def test_operation_setting_must_be_boolean() -> None:
    with pytest.raises(ManifestError, match="accept_trailing_slash"):
        OperationSpec.from_dict("a.b", {"source": "main", "accept_trailing_slash": "yes"})


def test_manifest_round_trips_the_setting(project: Project) -> None:
    manifest = project.load()
    restored = Manifest.from_dict(manifest.to_dict(), base_dir=manifest.base_dir)

    assert restored.source("lax").accept_trailing_slash is True
    assert restored.source("strict").accept_trailing_slash is False
    assert restored.operation("lax.strictRoute").accept_trailing_slash is False
    assert restored.operation("lax.loadTree").accept_trailing_slash is None


# ------------------------------------------------------------ extract / route


def test_route_variants() -> None:
    assert route_variants("/a/b") == ("/a/b", "/a/b/")
    assert route_variants("/a/b/") == ("/a/b/", "/a/b")
    assert route_variants("/") == ("/",)


def test_extract_path_params_is_strict_by_default() -> None:
    assert extract_path_params("/a/{id}", "/a/1/") is None


def test_extract_path_params_accepts_one_slash_only() -> None:
    template = "/a/{id}"
    assert extract_path_params(template, "/a/1", accept_trailing_slash=True) == {"id": "1"}
    assert extract_path_params(template, "/a/1/", accept_trailing_slash=True) == {"id": "1"}
    assert extract_path_params(template, "/a/1//", accept_trailing_slash=True) is None
    assert extract_path_params(template, "/a//", accept_trailing_slash=True) is None
    assert extract_path_params("/a/", "/a", accept_trailing_slash=True) == {}
    assert extract_path_params("/", "", accept_trailing_slash=True) is None


# ----------------------------------------------------------------- артефакты


def test_setting_is_resolved_per_operation(project: Project, operations: Any) -> None:
    assert operations.lax.load_tree.accept_trailing_slash is True
    assert operations.lax.get_item.accept_trailing_slash is True
    assert operations.lax.strict_route.accept_trailing_slash is False
    assert operations.strict.load_tree.accept_trailing_slash is False
    assert operations.strict.overridden.accept_trailing_slash is True


def test_contract_keeps_the_route_of_the_spec(project: Project, operations: Any) -> None:
    """Drift-guard и manifest видят путь из спецификации, настройка его не меняет."""
    assert operations.lax.load_tree.path == "/search/tree"
    assert operations.lax.declared.path == "/declared/"
    document = project.contract_document("lax__load_tree")
    assert document["path"] == "/search/tree"
    assert document["accept_trailing_slash"] is True


def test_strict_artifacts_do_not_change(project: Project) -> None:
    """Без настройки в документе нет нового ключа: прежние артефакты не меняются."""
    assert "accept_trailing_slash" not in project.contract_document("strict__load_tree")


def test_manifest_may_pin_the_spec_route(tmp_path: Path) -> None:
    """Закрепление ``path`` в manifest работает вместе с настройкой."""
    project = make_project(tmp_path, package="pinned_contracts")
    project.write_spec("api/openapi.yaml", SPEC)
    project.write_manifest(
        sources={
            "main": {"path": "api/openapi.yaml", "root": "api", "accept_trailing_slash": True}
        },
        operations={
            "main.loadTree": {
                "source": "main",
                "operation_id": "loadTree",
                "method": "POST",
                "path": "/search/tree",
            }
        },
    )
    project.update()
    assert project.check().is_clean


def test_changing_the_setting_is_artifact_drift(tmp_path: Path) -> None:
    project = build_project(tmp_path, package="drift_contracts")
    document = project._manifest_document
    document["sources"]["lax"]["accept_trailing_slash"] = False
    project.write_manifest(sources=document["sources"], operations=document["operations"])
    assert not project.check().is_clean


# ------------------------------------------------- временная настройка операции


def in_days(days: int) -> dt.date:
    return dt.date.today() + dt.timedelta(days=days)


def temporary(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "issue": "SAMSARA-24848",
        "reason": "спецификация потеряла слэш из @Path бэкенда",
        "expires_at": in_days(30).isoformat(),
    }
    data.update(overrides)
    return data


def test_temporary_setting_enables_the_operation() -> None:
    spec = OperationSpec.from_dict("a.b", {"source": "main", "accept_trailing_slash": temporary()})
    assert spec.accept_trailing_slash is True
    assert spec.trailing_slash_until == TemporarySetting(
        issue="SAMSARA-24848",
        reason="спецификация потеряла слэш из @Path бэкенда",
        expires_at=in_days(30),
    )
    # Срок остаётся датой: dump_manifest пишет его голым, как в исходном YAML.
    assert spec.to_dict()["accept_trailing_slash"] == temporary(expires_at=in_days(30))


def test_temporary_setting_accepts_a_yaml_date() -> None:
    """YAML сам превращает ``2026-12-31`` в дату — это тоже валидный срок."""
    spec = OperationSpec.from_dict(
        "a.b", {"source": "main", "accept_trailing_slash": temporary(expires_at=in_days(5))}
    )
    assert spec.trailing_slash_until is not None
    assert spec.trailing_slash_until.expires_at == in_days(5)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"issue": ""}, "issue"),
        ({"reason": None}, "reason"),
        ({"expires_at": "завтра"}, "expires_at"),
        ({"expires_at": dt.datetime(2026, 12, 31, 10, 0)}, "без времени"),
        ({"owner": "team"}, "неизвестные ключи"),
    ],
)
def test_temporary_setting_is_validated(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ManifestError, match=message):
        OperationSpec.from_dict(
            "a.b", {"source": "main", "accept_trailing_slash": temporary(**overrides)}
        )


def test_explicit_null_is_rejected() -> None:
    """Пустое значение не наследуется молча — как у ``d42`` и у источника."""
    with pytest.raises(ManifestError, match="accept_trailing_slash"):
        OperationSpec.from_dict("a.b", {"source": "main", "accept_trailing_slash": None})
    with pytest.raises(ManifestError, match="accept_trailing_slash"):
        SourceSpec.from_dict("main", {"path": "a.yaml", "accept_trailing_slash": None})


def test_temporary_setting_requires_expiry() -> None:
    data = temporary()
    del data["expires_at"]
    with pytest.raises(ManifestError, match="expires_at"):
        OperationSpec.from_dict("a.b", {"source": "main", "accept_trailing_slash": data})


def test_source_setting_cannot_be_temporary() -> None:
    """У источника настройка — свойство сервера, срока у неё нет."""
    with pytest.raises(ManifestError, match="только у операции"):
        SourceSpec.from_dict("main", {"path": "a.yaml", "accept_trailing_slash": temporary()})


def temporary_project(root: Path, expires_at: dt.date) -> Project:
    project = make_project(root, package="temporary_contracts")
    project.write_spec("api/openapi.yaml", SPEC)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "root": "api"}},
        operations={
            "main.loadTree": {
                "source": "main",
                "operation_id": "loadTree",
                "accept_trailing_slash": temporary(expires_at=expires_at.isoformat()),
            }
        },
    )
    return project


def test_temporary_setting_reaches_the_contract(tmp_path: Path) -> None:
    """В документ контракта попадает только факт включения, а не срок."""
    project = temporary_project(tmp_path, in_days(30))
    project.update()
    document = project.contract_document("main__load_tree")
    assert document["accept_trailing_slash"] is True
    assert "SAMSARA-24848" not in str(document)


def test_extending_the_term_is_not_artifact_drift(tmp_path: Path) -> None:
    project = temporary_project(tmp_path, in_days(30))
    project.update()
    later = temporary_project(tmp_path, in_days(60))
    assert later.check().is_clean


def test_expired_temporary_setting_fails_the_build(tmp_path: Path) -> None:
    project = temporary_project(tmp_path, in_days(-1))
    with pytest.raises(ManifestError, match="срок истёк"):
        project.load().check_temporary_settings(today=dt.date.today())


@pytest.mark.parametrize(
    "command",
    [
        ("update",),
        ("check",),
        ("diff",),
        ("add", "main.getItem", "--source", "main", "--operation-id", "getItem"),
    ],
    ids=lambda command: command[0],
)
def test_expired_temporary_setting_fails_every_command(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    project = temporary_project(tmp_path, in_days(-1))
    before = project.manifest_bytes()

    result = project.cli(*command)

    assert result.returncode == 1, result.stderr
    assert "срок истёк" in result.stderr
    assert project.manifest_bytes() == before


@pytest.mark.parametrize(
    "command",
    [("check",), ("add", "main.getItem", "--source", "main", "--operation-id", "getItem")],
    ids=lambda command: command[0],
)
def test_impossible_yaml_date_is_a_manifest_error(tmp_path: Path, command: tuple[str, ...]) -> None:
    """PyYAML сам строит дату и падает на 2026-11-31 — это ошибка manifest, не traceback."""
    project = temporary_project(tmp_path, in_days(30))
    project.manifest_path.write_text(
        project.manifest_path.read_text(encoding="utf-8").replace(
            f"'{in_days(30).isoformat()}'", "2026-11-31"
        ),
        encoding="utf-8",
    )

    result = project.cli(*command)

    assert result.returncode == 1, result.stderr
    assert "не разбирается" in result.stderr
    assert "Traceback" not in result.stderr


def test_impossible_yaml_date_in_waivers_is_a_waiver_error(tmp_path: Path) -> None:
    from geas.errors import WaiverError
    from geas.waivers import load_waivers

    path = tmp_path / "waivers.yaml"
    path.write_text("version: 1\nwaivers:\n  - expires_at: 2026-02-30\n", encoding="utf-8")
    with pytest.raises(WaiverError, match="не разбирается"):
        load_waivers(path)


def test_temporary_setting_obeys_max_days(tmp_path: Path) -> None:
    project = temporary_project(tmp_path, in_days(91))
    with pytest.raises(ManifestError, match="waiver_max_days"):
        project.load().check_temporary_settings(today=dt.date.today())


@pytest.mark.parametrize(("days", "phrase"), [(3, "осталось дней: 3"), (0, "истекает сегодня")])
def test_expiring_temporary_setting_is_a_warning(tmp_path: Path, days: int, phrase: str) -> None:
    project = temporary_project(tmp_path, in_days(days))
    result = project.cli("update")
    assert result.returncode == 0, result.stderr
    assert "accept_trailing_slash операции main.loadTree скоро истекает" in result.stderr
    assert phrase in result.stderr
    assert "SAMSARA-24848" in result.stderr


def test_far_temporary_setting_is_silent(tmp_path: Path) -> None:
    project = temporary_project(tmp_path, in_days(30))
    result = project.cli("update")
    assert result.returncode == 0, result.stderr
    assert "скоро истекает" not in result.stderr


# ------------------------------------------------------ пересечение маршрутов

TWIN_SPEC = """\
openapi: "3.0.3"

info:
  title: Twin API
  version: "1.0.0"

paths:
  /twin:
    get:
      operationId: twinPlain
      responses:
        "204":
          description: Готово
  /twin/:
    get:
      operationId: twinSlash
      responses:
        "204":
          description: Готово
    post:
      operationId: twinSlashPost
      responses:
        "204":
          description: Готово
  /things/{id}:
    get:
      operationId: getThing
      parameters:
        - {name: id, in: path, required: true, schema: {type: string}}
      responses:
        "204":
          description: Готово
  /things/{thingId}/:
    get:
      operationId: getThingSlash
      parameters:
        - {name: thingId, in: path, required: true, schema: {type: string}}
      responses:
        "204":
          description: Готово
"""


def twin_project(
    root: Path, *, source: dict[str, Any], operations: dict[str, Any] | None = None
) -> Project:
    project = make_project(root, package="twin_contracts")
    project.write_spec("api/openapi.yaml", TWIN_SPEC)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "root": "api", "selection": "all", **source}},
        operations=operations or {},
    )
    return project


def test_strict_twins_coexist(tmp_path: Path) -> None:
    """Без настройки ``/twin`` и ``/twin/`` — разные маршруты, как и в спецификации."""
    twin_project(tmp_path, source={}).build()


def test_setting_that_merges_two_operations_is_rejected(tmp_path: Path) -> None:
    project = twin_project(tmp_path, source={"accept_trailing_slash": True})
    with pytest.raises(ManifestError, match="принимают один и тот же маршрут") as info:
        project.build()
    assert "main.getThing" in str(info.value)


def test_overlap_is_found_through_path_parameter_names(tmp_path: Path) -> None:
    project = twin_project(
        tmp_path,
        source={},
        operations={
            "main.getThing": {
                "source": "main",
                "operation_id": "getThing",
                "accept_trailing_slash": True,
            }
        },
    )
    with pytest.raises(ManifestError, match=r"main\.getThingSlash"):
        project.build()


def test_overlap_names_every_operation_to_switch_off(tmp_path: Path) -> None:
    """Маршруты-близнецы: настройка любого из них заходит на маршрут другого."""
    project = twin_project(tmp_path, source={"accept_trailing_slash": True})
    with pytest.raises(ManifestError) as info:
        project.build()
    assert "accept_trailing_slash: false у 'main.getThing', 'main.getThingSlash'" in str(info.value)


def test_overlap_is_resolved_by_switching_twins_off(tmp_path: Path) -> None:
    switched_off = {
        key: {"source": "main", "operation_id": operation_id, "accept_trailing_slash": False}
        for key, operation_id in {
            "main.twinPlain": "twinPlain",
            "main.twinSlash": "twinSlash",
            "main.getThing": "getThing",
            "main.getThingSlash": "getThingSlash",
        }.items()
    }
    project = twin_project(
        tmp_path, source={"accept_trailing_slash": True}, operations=switched_off
    )
    built = project.build()
    assert "accept_trailing_slash" not in built.by_key("main.twinPlain").document
    # Другой метод на том же маршруте не пересекается и сохраняет настройку источника.
    assert built.by_key("main.twinSlashPost").document["accept_trailing_slash"] is True


def test_overlap_across_sources_is_rejected(tmp_path: Path) -> None:
    """Все моки регистрируются на одном JJ-сервере, источник их не разделяет."""
    project = make_project(tmp_path, package="cross_contracts")
    project.write_spec("api/a.yaml", TWIN_SPEC)
    project.write_spec("api/b.yaml", TWIN_SPEC)
    project.write_manifest(
        sources={
            "a": {"path": "api/a.yaml", "root": "api", "accept_trailing_slash": True},
            "b": {"path": "api/b.yaml", "root": "api"},
        },
        operations={
            "a.twinPlain": {"source": "a", "operation_id": "twinPlain"},
            "b.twinSlash": {"source": "b", "operation_id": "twinSlash"},
        },
    )
    with pytest.raises(ManifestError, match=r"'a\.twinPlain'"):
        project.build()


def test_sources_with_different_base_paths_do_not_overlap(tmp_path: Path) -> None:
    project = make_project(tmp_path, package="based_contracts")
    project.write_spec("api/a.yaml", TWIN_SPEC)
    project.write_spec("api/b.yaml", TWIN_SPEC)
    project.write_manifest(
        sources={
            "a": {
                "path": "api/a.yaml",
                "root": "api",
                "base_path": "/a",
                "accept_trailing_slash": True,
            },
            "b": {"path": "api/b.yaml", "root": "api", "base_path": "/b"},
        },
        operations={
            "a.twinPlain": {"source": "a", "operation_id": "twinPlain"},
            "b.twinSlash": {"source": "b", "operation_id": "twinSlash"},
        },
    )
    project.build()


def test_one_route_under_several_keys_is_not_an_overlap(tmp_path: Path) -> None:
    """Совпадение есть и без настройки — его она не создаёт и не ловит."""
    project = make_project(tmp_path, package="keys_contracts")
    project.write_spec("api/openapi.yaml", TWIN_SPEC)
    project.write_manifest(
        sources={
            "main": {"path": "api/openapi.yaml", "root": "api", "accept_trailing_slash": True}
        },
        operations={
            "main.thing": {"source": "main", "operation_id": "getThing"},
            "main.thingAgain": {
                "source": "main",
                "method": "GET",
                "path": "/things/{id}",
            },
        },
    )
    project.build()


LITERAL_SPEC = """\
openapi: "3.0.3"

info:
  title: Literal API
  version: "1.0.0"

paths:
  /docs/{id}:
    get:
      operationId: getDoc
      parameters:
        - {name: id, in: path, required: true, schema: {type: string}}
      responses:
        "204":
          description: Готово
  /docs/new/:
    get:
      operationId: newDoc
      responses:
        "204":
          description: Готово
  /docs/draft:
    get:
      operationId: draftDoc
      responses:
        "204":
          description: Готово
"""


def literal_project(root: Path, operations: dict[str, Any]) -> Project:
    project = make_project(root, package="literal_contracts")
    project.write_spec("api/openapi.yaml", LITERAL_SPEC)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "root": "api"}},
        operations={
            key: {"source": "main", "operation_id": operation_id, **extra}
            for key, (operation_id, extra) in operations.items()
        },
    )
    return project


def test_template_that_starts_catching_a_literal_route_is_rejected(tmp_path: Path) -> None:
    """С настройкой ``/docs/{id}`` принял бы ``/docs/new/`` — маршрут другой операции."""
    project = literal_project(
        tmp_path,
        {
            "main.getDoc": ("getDoc", {"accept_trailing_slash": True}),
            "main.newDoc": ("newDoc", {}),
        },
    )
    with pytest.raises(ManifestError, match=r"accept_trailing_slash: false у 'main\.getDoc'$"):
        project.build()


def test_literal_overlap_without_the_setting_is_left_to_the_spec(tmp_path: Path) -> None:
    """``/docs/{id}`` и ``/docs/draft`` пересекаются и без настройки."""
    project = literal_project(
        tmp_path,
        {
            "main.getDoc": ("getDoc", {"accept_trailing_slash": True}),
            "main.draftDoc": ("draftDoc", {}),
        },
    )
    project.build()


def test_disjoint_templated_segments_do_not_block_the_setting(tmp_path: Path) -> None:
    """``/files/{id}.json`` со слэшем не принимает ни одного пути ``/files/{name}.xml/``."""
    spec = """\
openapi: "3.0.3"

info:
  title: Files API
  version: "1.0.0"

paths:
  /files/{id}.json:
    get:
      operationId: asJson
      parameters:
        - {name: id, in: path, required: true, schema: {type: string}}
      responses:
        "204":
          description: Готово
  /files/{name}.xml/:
    get:
      operationId: asXml
      parameters:
        - {name: name, in: path, required: true, schema: {type: string}}
      responses:
        "204":
          description: Готово
"""
    project = make_project(tmp_path, package="files_contracts")
    project.write_spec("api/openapi.yaml", spec)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "root": "api", "selection": "all"}},
        operations={
            "main.asJson": {
                "source": "main",
                "operation_id": "asJson",
                "accept_trailing_slash": True,
            }
        },
    )
    project.build()


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("/a/b", "/a/b", True),
        ("/a/b", "/a/b/", False),
        ("/a/{id}", "/a/{other}", True),
        ("/a/{id}", "/a/new", True),
        ("/a/{id}", "/a/", False),
        ("/a/{id}.json", "/a/x.json", True),
        ("/a/{id}.json", "/a/x.xml", False),
        ("/a/{id}", "/b/{id}", False),
        ("/a/{x}.json", "/a/{y}.xml", False),
        ("/a/{x}.json", "/a/{y}", True),
        ("/a/v{x}", "/a/v1{y}", True),
        ("/a/v{x}", "/a/w{y}", False),
        ("/a/{x}-{y}", "/a/{z}", True),
    ],
)
def test_route_templates_overlap(first: str, second: str, expected: bool) -> None:
    assert route_templates_overlap(first, second) is expected
    assert route_templates_overlap(second, first) is expected


def test_add_keeps_a_temporary_setting(tmp_path: Path) -> None:
    project = temporary_project(tmp_path, in_days(30))
    project.update()

    result = project.cli("add", "main.getItem", "--source", "main", "--operation-id", "getItem")

    assert result.returncode == 0, result.stderr
    restored = project.load().operation("main.loadTree")
    assert restored is not None
    assert restored.trailing_slash_until is not None
    assert restored.trailing_slash_until.issue == "SAMSARA-24848"
    text = project.manifest_path.read_text(encoding="utf-8")
    assert f"expires_at: {in_days(30).isoformat()}\n" in text
    assert project.cli("update").returncode == 0


def test_add_refuses_an_operation_merged_by_the_setting(tmp_path: Path) -> None:
    """Новая операция, которую склеила бы уже включённая настройка, не добавляется."""
    project = twin_project(
        tmp_path,
        source={"selection": "explicit", "accept_trailing_slash": True},
        operations={"main.twinPlain": {"source": "main", "operation_id": "twinPlain"}},
    )
    before = project.manifest_bytes()

    result = project.cli("add", "main.twinSlash", "--source", "main", "--operation-id", "twinSlash")

    assert result.returncode == 1, result.stderr
    assert "принимают один и тот же маршрут" in result.stderr
    assert "'main.twinPlain', 'main.twinSlash'" in result.stderr
    assert project.manifest_bytes() == before


def test_add_reports_a_conflict_with_a_temporary_setting(tmp_path: Path) -> None:
    """Сырой YAML с датой печатается в сообщении о конфликте, а не роняет ``add``."""
    project = temporary_project(tmp_path, in_days(30))
    project.manifest_path.write_text(
        project.manifest_path.read_text(encoding="utf-8").replace(
            f"'{in_days(30).isoformat()}'", in_days(30).isoformat()
        ),
        encoding="utf-8",
    )
    before = project.manifest_bytes()

    result = project.cli("add", "main.loadTree", "--source", "main", "--operation-id", "getItem")

    assert result.returncode == 2, result.stderr
    assert "уже есть в manifest" in result.stderr
    assert in_days(30).isoformat() in result.stderr
    assert project.manifest_bytes() == before


# ----------------------------------------------- validate_recorded_request


@pytest.mark.parametrize("path", ["/search/tree", "/search/tree/"])
def test_recorded_request_accepts_both_forms(operations: Any, path: str) -> None:
    operations.lax.load_tree.validate_recorded_request(**recorded(path))


def test_recorded_request_rejects_double_slash(operations: Any) -> None:
    with pytest.raises(RequestContractError) as info:
        operations.lax.load_tree.validate_recorded_request(**recorded("/search/tree//"))
    assert info.value.json_pointer == "/path"


def test_strict_operation_rejects_slash(operations: Any) -> None:
    with pytest.raises(RequestContractError):
        operations.strict.load_tree.validate_recorded_request(**recorded("/search/tree/"))
    with pytest.raises(RequestContractError):
        operations.lax.strict_route.validate_recorded_request(**recorded("/strict/", method="GET"))


@pytest.mark.parametrize("path", ["/items/7", "/items/7/"])
def test_path_params_are_parsed_the_same(operations: Any, path: str) -> None:
    operations.lax.get_item.validate_recorded_request(**recorded(path, method="GET"))


def test_path_param_schema_still_applies(operations: Any) -> None:
    with pytest.raises(RequestContractError, match="itemId"):
        operations.lax.get_item.validate_recorded_request(**recorded("/items/x/", method="GET"))


@pytest.mark.parametrize("path", ["/declared/", "/declared"])
def test_spec_slash_accepts_the_form_without_it(operations: Any, path: str) -> None:
    operations.lax.declared.validate_recorded_request(**recorded(path))


# ---------------------------------------------------------------- ContractMock


async def call(server: JJServer, method: str, path: str) -> int:
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, f"{server.url}{path}") as response,
    ):
        return response.status


@pytest.mark.parametrize("path", ["/search/tree", "/search/tree/"])
async def test_mock_intercepts_both_forms(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
    path: str,
) -> None:
    async with operations.lax.load_tree.mock(status=204, wait_for_requests=1) as mock:
        assert await call(jj_server, "POST", path) == 204

    assert [item.path for item in mock.requests] == [path]


async def test_mock_with_path_param_intercepts_both_forms(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    for path in ("/items/5", "/items/5/"):
        async with operations.lax.get_item.mock(
            status=204, path_params={"itemId": 5}, wait_for_requests=1
        ) as mock:
            assert await call(jj_server, "GET", path) == 204
        assert [item.path for item in mock.requests] == [path]


@pytest.mark.parametrize("path", ["/items/5", "/items/5/"])
async def test_mock_with_unpinned_param_query_and_headers(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
    path: str,
) -> None:
    """Обе формы получают одни и те же query и заголовки; параметр разбирает JJ."""
    async with (
        operations.lax.get_item.mock(
            status=204,
            query_params={"expand": "all"},
            headers={"X-Trace": "t-1"},
            wait_for_requests=1,
        ) as mock,
        aiohttp.ClientSession() as session,
    ):
        async with session.get(f"{jj_server.url}{path}") as response:
            assert response.status == 404
        async with session.get(
            f"{jj_server.url}{path}", params={"expand": "all"}, headers={"X-Trace": "t-1"}
        ) as response:
            assert response.status == 204

    assert [item.path for item in mock.requests] == [path]


async def test_mock_does_not_intercept_other_routes(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    async with operations.lax.load_tree.mock(status=204):
        assert await call(jj_server, "POST", "/search/tree//") == 404
        assert await call(jj_server, "POST", "/search/tre/") == 404


@pytest.mark.parametrize("path", ["/declared/", "/declared"])
async def test_mock_of_a_slash_route_intercepts_the_form_without_it(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
    path: str,
) -> None:
    async with operations.lax.declared.mock(status=204, wait_for_requests=1) as mock:
        assert await call(jj_server, "POST", path) == 204

    assert [item.path for item in mock.requests] == [path]


async def test_strict_mock_does_not_intercept_slash(
    jj_server: JJServer,  # noqa: F811 - фикстура
    operations: Any,
) -> None:
    async with operations.strict.load_tree.mock(status=204):
        assert await call(jj_server, "POST", "/search/tree/") == 404

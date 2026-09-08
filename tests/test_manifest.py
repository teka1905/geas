"""Тесты строгого разбора manifest.

Manifest — единственный вход генератора и одновременно закреплённая привязка к
спецификации. Поэтому разбор обязан быть строгим: неизвестный ключ, неверный тип
и противоречивая комбинация — это ошибка, а не «поле проигнорировано». Молчаливо
принятая опечатка означает, что часть контракта просто не проверяется.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from openapi_contracts.errors import ContractError, ManifestError, NamespaceCollisionError
from openapi_contracts.manifest import (
    MANIFEST_VERSION,
    Manifest,
    NonWaivableRule,
    OperationSpec,
    Policies,
    Selection,
    SourceSpec,
    UnknownFormatPolicy,
    default_manifest_document,
    dump_manifest,
    load_manifest,
)


def document(**overrides: Any) -> dict[str, Any]:
    """Минимальный валидный документ manifest с точечными правками."""
    data: dict[str, Any] = {
        "version": 1,
        "output": {"directory": "pkg/generated", "package": "pkg.generated"},
        "sources": {"main": {"path": "api/openapi.yaml"}},
        "operations": {"api.createDocument": {"source": "main", "operation_id": "createDocument"}},
    }
    data.update(overrides)
    return data


def parse(data: dict[str, Any], base_dir: Path | None = None) -> Manifest:
    return Manifest.from_dict(data, base_dir=base_dir or Path("/tmp/project"))


def operations(**entries: Any) -> dict[str, Any]:
    """Секция ``operations`` с одной записью, дополненной обязательным source."""
    return {key: {"source": "main", **value} for key, value in entries.items()}


# ------------------------------------------------------------------ базовое


def test_minimal_manifest_defaults() -> None:
    manifest = parse(document())

    assert manifest.output_directory == "pkg/generated"
    assert manifest.output_package == "pkg.generated"
    # Значения по умолчанию: файл waiver'ов, политика форматов, срок waiver'а.
    assert manifest.waivers_path == "waivers.yaml"
    assert manifest.policies == Policies()
    assert manifest.policies.waiver_max_days == 90
    assert manifest.policies.unknown_formats is UnknownFormatPolicy.REJECT
    # Источник по умолчанию — allowlist, корень $ref — каталог спецификации.
    source = manifest.source("main")
    assert source.selection is Selection.EXPLICIT
    assert source.root is None
    assert source.base_path is None
    # Операция по умолчанию генерирует d42 и берёт Python path из ключа.
    spec = manifest.operation("api.createDocument")
    assert spec is not None
    assert spec.d42 is True
    assert spec.python_path == ()
    assert spec.resolved_python_path() == ("api", "create_document")
    assert spec.responses == ()


def test_optional_fields_are_parsed() -> None:
    manifest = parse(
        document(
            waivers="config/waivers.yaml",
            policies={"waiver_max_days": 14, "unknown_formats": "annotate"},
            sources={
                "main": {
                    "path": "api/openapi.yaml",
                    "root": "api",
                    "selection": "explicit",
                    "base_path": "/api/v1",
                }
            },
            operations=operations(
                **{
                    "api.createDocument": {
                        "operation_id": "createDocument",
                        "method": "post",
                        "path": "/api/v1/documents",
                        "request": {"content_type": "application/json"},
                        "responses": [
                            {"status": 200, "content_type": "application/json"},
                            {"status": "default"},
                        ],
                        "python_path": ["api", "create_document_v2"],
                        "d42": False,
                        "non_waivable": [
                            {
                                "direction": "response",
                                "json_pointer": "/body/rc",
                                "rules": ["required", "non_null", "non_empty_enum"],
                            }
                        ],
                    }
                }
            ),
        )
    )

    assert manifest.waivers_path == "config/waivers.yaml"
    assert manifest.policies.waiver_max_days == 14
    assert manifest.policies.unknown_formats is UnknownFormatPolicy.ANNOTATE
    source = manifest.source("main")
    assert source.root == "api"
    assert source.base_path == "/api/v1"
    spec = manifest.operation("api.createDocument")
    assert spec is not None
    # Метод нормализуется к верхнему регистру, чтобы binding не зависел от записи.
    assert spec.method == "POST"
    assert spec.request_content_type == "application/json"
    assert [(item.status, item.content_type) for item in spec.responses] == [
        (200, "application/json"),
        ("default", None),
    ]
    assert spec.resolved_python_path() == ("api", "create_document_v2")
    assert spec.d42 is False
    assertion = spec.non_waivable[0]
    assert assertion.path == ("body", "rc")
    # Правила дедуплицируются и упорядочиваются детерминированно.
    assert set(assertion.rules) == {
        NonWaivableRule.REQUIRED,
        NonWaivableRule.NON_NULL,
        NonWaivableRule.NON_EMPTY_ENUM,
    }


def test_non_waivable_rules_are_deduplicated() -> None:
    manifest = parse(
        document(
            operations=operations(
                **{
                    "api.createDocument": {
                        "non_waivable": [
                            {
                                "direction": "response",
                                "json_pointer": "/body/rc",
                                "rules": ["required", "required", "non_null"],
                            }
                        ]
                    }
                }
            )
        )
    )
    spec = manifest.operation("api.createDocument")
    assert spec is not None
    assert spec.non_waivable[0].rules == (NonWaivableRule.NON_NULL, NonWaivableRule.REQUIRED)


# -------------------------------------------------------------------- версия


@pytest.mark.parametrize("version", [1])
def test_version_accepts_supported_int(version: int) -> None:
    assert parse(document(version=version)).to_dict()["version"] == MANIFEST_VERSION


@pytest.mark.parametrize(
    "version",
    [
        # bool — подкласс int, а True == 1: без явной проверки типа значение
        # прошло бы как поддерживаемая версия.
        True,
        False,
        # 1.0 == 1, но версия формата — целое, а не число.
        1.0,
        "1",
        2,
        0,
        None,
        [1],
    ],
)
def test_version_rejects_everything_but_the_supported_int(version: Any) -> None:
    with pytest.raises(ManifestError, match=re.escape("manifest.version")):
        parse(document(version=version))


def test_version_is_required() -> None:
    data = document()
    del data["version"]
    with pytest.raises(ManifestError, match=re.escape("manifest.version")):
        parse(data)


# ------------------------------------------------------------ неизвестные ключи


@pytest.mark.parametrize(
    ("data", "where"),
    [
        (document(oops=1), "manifest"),
        (
            document(output={"directory": "d", "package": "p", "oops": 1}),
            "manifest.output",
        ),
        (document(sources={"main": {"path": "a.yaml", "oops": 1}}), "sources.main"),
        (
            document(operations=operations(**{"api.createDocument": {"oops": 1}})),
            "operations.api.createDocument",
        ),
        (
            document(
                operations=operations(
                    **{"api.createDocument": {"request": {"content_type": "a", "oops": 1}}}
                )
            ),
            "operations.api.createDocument.request",
        ),
        (
            document(
                operations=operations(
                    **{"api.createDocument": {"responses": [{"status": 200, "oops": 1}]}}
                )
            ),
            "operations.api.createDocument.responses[0]",
        ),
        (
            document(
                operations=operations(
                    **{
                        "api.createDocument": {
                            "non_waivable": [
                                {
                                    "direction": "response",
                                    "json_pointer": "/body",
                                    "rules": ["present"],
                                    "oops": 1,
                                }
                            ]
                        }
                    }
                )
            ),
            "operations.api.createDocument.non_waivable[0]",
        ),
        (document(policies={"waiver_max_days": 30, "oops": 1}), "policies"),
    ],
    ids=[
        "top-level",
        "output",
        "source",
        "operation",
        "request",
        "response-selector",
        "non-waivable",
        "policies",
    ],
)
def test_unknown_key_is_rejected_at_every_level(data: dict[str, Any], where: str) -> None:
    with pytest.raises(ManifestError) as info:
        parse(data)
    message = str(info.value)
    assert message.startswith(f"{where}: неизвестные ключи ['oops']")
    # Ошибка обязана подсказывать, что здесь вообще можно писать.
    assert "Допустимые:" in message


# ------------------------------------------------------------------- типы


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (document(output=["pkg"]), "manifest.output: ожидался объект"),
        (document(sources=["main"]), "manifest.sources: ожидался объект"),
        (document(sources={}, operations={}), "должен быть объявлен хотя бы один источник"),
        (document(operations=["api.x"]), "manifest.operations: ожидался объект"),
        (document(operations={"api.x": "main"}), "operations.api.x: ожидался объект"),
        (document(operations={1: {"source": "main"}}), "ключ 1 должен быть строкой"),
        (document(waivers=3), "manifest.waivers: ожидалась непустая строка"),
        (document(waivers=""), "manifest.waivers: ожидалась непустая строка"),
        (
            document(sources={"main": {"path": "a.yaml", "root": 1}}),
            "sources.main.root: ожидалась строка",
        ),
        (
            document(sources={"main": {"path": "a.yaml", "base_path": ["/api"]}}),
            "sources.main.base_path: ожидалась строка",
        ),
        (
            document(operations=operations(**{"api.x": {"method": 123}})),
            "operations.api.x.method: ожидалась строка",
        ),
        (
            document(operations=operations(**{"api.x": {"path": ["/x"]}})),
            "operations.api.x.path: ожидалась строка",
        ),
        (
            document(operations=operations(**{"api.x": {"operation_id": 7}})),
            "operations.api.x.operation_id: ожидалась строка",
        ),
        (
            document(operations=operations(**{"api.x": {"request": {"content_type": 7}}})),
            "operations.api.x.request.content_type: ожидалась строка",
        ),
        (
            document(operations=operations(**{"api.x": {"responses": {"status": 200}}})),
            "operations.api.x.responses: ожидался список",
        ),
        # d42 — строго булево: "false" и 1 не считаются выключением.
        (
            document(operations=operations(**{"api.x": {"d42": "false"}})),
            "operations.api.x.d42: ожидалось булево значение",
        ),
        (
            document(operations=operations(**{"api.x": {"d42": 1}})),
            "operations.api.x.d42: ожидалось булево значение",
        ),
        (
            document(
                operations=operations(**{"api.x": {"non_waivable": {"direction": "response"}}})
            ),
            "operations.api.x.non_waivable: ожидался список",
        ),
        (
            document(policies={"waiver_max_days": 0}),
            "policies.waiver_max_days: ожидалось положительное целое",
        ),
        (
            document(policies={"waiver_max_days": -1}),
            "policies.waiver_max_days: ожидалось положительное целое",
        ),
        # bool снова: True прошёл бы как «1 день».
        (
            document(policies={"waiver_max_days": True}),
            "policies.waiver_max_days: ожидалось положительное целое",
        ),
        (
            document(policies={"waiver_max_days": 30.0}),
            "policies.waiver_max_days: ожидалось положительное целое",
        ),
        (document(policies=["waiver_max_days"]), "policies: ожидался объект"),
        (document(policies={"unknown_formats": "maybe"}), "policies.unknown_formats"),
        (
            document(sources={"main": {"path": "a.yaml", "selection": "some"}}),
            "sources.main.selection",
        ),
    ],
)
def test_wrong_types_are_rejected(data: dict[str, Any], why: str) -> None:
    with pytest.raises(ManifestError) as info:
        parse(data)
    assert why in str(info.value)


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (document(output={"package": "pkg.generated"}), "поле 'directory' обязательно"),
        (document(output={"directory": "pkg/generated"}), "поле 'package' обязательно"),
        (document(output={"directory": "", "package": "p"}), "поле 'directory' обязательно"),
        (
            document(sources={"main": {"selection": "all"}}, operations={}),
            "поле 'path' обязательно",
        ),
        (
            document(operations={"api.x": {"operation_id": "x"}}),
            "поле 'source' обязательно",
        ),
        (
            document(
                operations=operations(
                    **{"api.x": {"non_waivable": [{"json_pointer": "/body", "rules": ["present"]}]}}
                )
            ),
            "поле 'direction' обязательно",
        ),
        (
            document(
                operations=operations(
                    **{"api.x": {"non_waivable": [{"direction": "response", "rules": ["present"]}]}}
                )
            ),
            "поле 'json_pointer' обязательно",
        ),
        (
            document(
                operations=operations(
                    **{
                        "api.x": {
                            "non_waivable": [{"direction": "response", "json_pointer": "/body"}]
                        }
                    }
                )
            ),
            "rules: ожидался непустой список правил",
        ),
    ],
)
def test_missing_required_fields(data: dict[str, Any], why: str) -> None:
    with pytest.raises(ManifestError) as info:
        parse(data)
    assert why in str(info.value)


def test_output_is_required() -> None:
    data = document()
    del data["output"]
    with pytest.raises(ManifestError, match=re.escape("manifest.output")):
        parse(data)


@pytest.mark.parametrize(
    ("pointer", "why"),
    [
        ("body", "должен начинаться с '/'"),
        ("/a//b", "пустой сегмент"),
    ],
)
def test_broken_contract_path_is_reported_as_manifest_error(pointer: str, why: str) -> None:
    """Битый указатель — ошибка manifest с именем поля, а не безымянная ContractError."""
    data = document(
        operations=operations(
            **{
                "api.x": {
                    "non_waivable": [
                        {"direction": "response", "json_pointer": pointer, "rules": ["present"]}
                    ]
                }
            }
        )
    )
    with pytest.raises(ManifestError) as info:
        parse(data)
    message = str(info.value)
    assert "operations.api.x.non_waivable[0].json_pointer" in message
    assert why in message


# ------------------------------------------------------------- селекторы и источники


def test_duplicate_response_selector() -> None:
    data = document(
        operations=operations(
            **{
                "api.x": {
                    "responses": [
                        {"status": 200, "content_type": "application/json"},
                        {"status": 200, "content_type": "application/json"},
                    ]
                }
            }
        )
    )
    with pytest.raises(ManifestError, match="дубликат варианта 200:application/json"):
        parse(data)


def test_duplicate_response_selector_without_content_type() -> None:
    data = document(
        operations=operations(**{"api.x": {"responses": [{"status": 204}, {"status": 204}]}})
    )
    with pytest.raises(ManifestError, match="дубликат варианта 204:-"):
        parse(data)


def test_response_selectors_differing_by_content_type_are_allowed() -> None:
    manifest = parse(
        document(
            operations=operations(
                **{
                    "api.x": {
                        "responses": [
                            {"status": 200, "content_type": "application/json"},
                            {"status": 200, "content_type": "application/pdf"},
                        ]
                    }
                }
            )
        )
    )
    spec = manifest.operation("api.x")
    assert spec is not None
    assert len(spec.responses) == 2


@pytest.mark.parametrize(
    ("status", "why"),
    [
        (True, "ожидалось целое или 'default'"),
        (None, "ожидалось целое или 'default'"),
        ("200", "строковый статус допустим только как 'default'"),
        ("ok", "строковый статус допустим только как 'default'"),
    ],
)
def test_response_status_validation(status: Any, why: str) -> None:
    data = document(operations=operations(**{"api.x": {"responses": [{"status": status}]}}))
    with pytest.raises(ManifestError) as info:
        parse(data)
    assert why in str(info.value)


def test_operation_referencing_undeclared_source() -> None:
    data = document(
        operations={
            "api.x": {"source": "main"},
            "api.y": {"source": "legacy"},
        }
    )
    with pytest.raises(ManifestError, match="источник 'legacy' не объявлен в sources"):
        parse(data)


def test_explicit_source_nobody_references() -> None:
    data = document(
        sources={"main": {"path": "a.yaml"}, "legacy": {"path": "b.yaml"}},
        operations={"api.x": {"source": "main"}},
    )
    with pytest.raises(ManifestError, match=r"источники \['legacy'\]"):
        parse(data)


def test_empty_explicit_source_is_allowed_right_after_init() -> None:
    """Сразу после ``init`` операций ещё нет — пустой allowlist это не ошибка."""
    manifest = parse(document(operations={}))
    assert manifest.operations == ()


def test_source_in_all_mode_needs_no_operations() -> None:
    manifest = parse(
        document(
            sources={"main": {"path": "a.yaml"}, "extra": {"path": "b.yaml", "selection": "all"}},
            operations={"api.x": {"source": "main"}},
        )
    )
    assert manifest.source("extra").selection is Selection.ALL


def test_unknown_source_lookup() -> None:
    manifest = parse(document())
    with pytest.raises(ManifestError, match="источник 'legacy' не объявлен"):
        manifest.source("legacy")
    assert manifest.operation("api.nope") is None


@pytest.mark.parametrize("name", ["2fa", "class", "по-русски"])
def test_source_name_must_be_usable_as_identifier(name: str) -> None:
    data = document(sources={name: {"path": "a.yaml"}}, operations={"api.x": {"source": name}})
    with pytest.raises(NamespaceCollisionError, match="имя источника"):
        parse(data)


@pytest.mark.parametrize("package", ["pkg.class", "pkg.2generated", "pkg._private"])
def test_output_package_segments_must_be_identifiers(package: str) -> None:
    with pytest.raises(NamespaceCollisionError, match=re.escape("manifest.output.package")):
        parse(document(output={"directory": "pkg/generated", "package": package}))


# ----------------------------------------------------------------- python_path


@pytest.mark.parametrize(
    "python_path",
    [
        ["only_one"],
        [],
        "api.create",
        ["api", 2],
        [["api"], ["create"]],
    ],
)
def test_python_path_must_be_at_least_two_strings(python_path: Any) -> None:
    data = document(operations=operations(**{"api.x": {"python_path": python_path}}))
    with pytest.raises(ManifestError, match="python_path: ожидался список минимум из двух строк"):
        parse(data)


@pytest.mark.parametrize("segment", ["class", "2fa", "_private", "with-dash", ""])
def test_python_path_segments_must_be_identifiers(segment: str) -> None:
    data = document(operations=operations(**{"api.x": {"python_path": ["api", segment]}}))
    with pytest.raises(NamespaceCollisionError) as info:
        parse(data)
    assert "operations.api.x.python_path[1]" in str(info.value)


@pytest.mark.parametrize("segment", ["by_key", "keys", "registry"])
def test_python_path_cannot_take_reserved_namespace_attribute(segment: str) -> None:
    """Явный python_path попадает в тот же namespace, что и выведенный из ключа."""
    data = document(operations=operations(**{"api.x": {"python_path": [segment, "create"]}}))
    with pytest.raises(NamespaceCollisionError, match="зарезервировано"):
        parse(data)


# ------------------------------------------------------------------ коллизии


def test_two_keys_with_the_same_python_path() -> None:
    data = document(
        operations={"api.getItem": {"source": "main"}, "api.get_item": {"source": "main"}}
    )
    with pytest.raises(NamespaceCollisionError) as info:
        parse(data)
    message = str(info.value)
    assert "одинаковый Python path api.get_item" in message
    # Никаких молчаливых суффиксов: чинится только явным python_path.
    assert "Задайте python_path в manifest" in message


def test_explicit_python_path_resolves_the_collision() -> None:
    manifest = parse(
        document(
            operations={
                "api.getItem": {"source": "main"},
                "api.get_item": {"source": "main", "python_path": ["api", "get_item_snake"]},
            }
        )
    )
    assert {spec.resolved_python_path() for spec in manifest.operations} == {
        ("api", "get_item"),
        ("api", "get_item_snake"),
    }


def test_operation_name_used_as_namespace_prefix() -> None:
    data = document(
        operations={
            "api.documents": {"source": "main"},
            "api.documents.create": {"source": "main"},
        }
    )
    with pytest.raises(NamespaceCollisionError) as info:
        parse(data)
    assert "используется как namespace" in str(info.value)


def test_duplicate_operation_key_is_rejected() -> None:
    """Дубликат ключа ловится и при программной сборке manifest."""
    spec = OperationSpec.from_dict("api.x", {"source": "main"})
    with pytest.raises(ManifestError, match=re.escape("дубликат ключа 'api.x'")):
        Manifest(
            base_dir=Path("/tmp/project"),
            output_directory="pkg/generated",
            output_package="pkg.generated",
            policies=Policies(),
            sources=(SourceSpec.from_dict("main", {"path": "a.yaml"}),),
            operations=(spec, spec),
        )


# -------------------------------------------------------------------- to_dict


def test_to_dict_round_trips() -> None:
    data = document(
        policies={"waiver_max_days": 30, "unknown_formats": "annotate"},
        sources={"main": {"path": "api/openapi.yaml", "root": "api", "base_path": "/api/v1"}},
        operations=operations(
            **{
                "api.createDocument": {
                    "operation_id": "createDocument",
                    "method": "POST",
                    "path": "/api/v1/documents",
                    "request": {"content_type": "application/json"},
                    "responses": [{"status": 200, "content_type": "application/json"}],
                    "python_path": ["api", "create_document"],
                    "d42": False,
                    "non_waivable": [
                        {
                            "direction": "response",
                            "json_pointer": "/body/rc",
                            "rules": ["required"],
                        }
                    ],
                }
            }
        ),
    )
    manifest = parse(data)
    canonical = manifest.to_dict()

    again = Manifest.from_dict(canonical, base_dir=manifest.base_dir)
    assert again.to_dict() == canonical
    assert again == manifest


def test_to_dict_is_deterministic_regardless_of_input_order() -> None:
    forward = parse(
        document(
            sources={"alpha": {"path": "a.yaml"}, "beta": {"path": "b.yaml"}},
            operations={"api.b": {"source": "alpha"}, "api.a": {"source": "beta"}},
        )
    )
    backward = parse(
        document(
            sources={"beta": {"path": "b.yaml"}, "alpha": {"path": "a.yaml"}},
            operations={"api.a": {"source": "beta"}, "api.b": {"source": "alpha"}},
        )
    )

    assert forward.to_dict() == backward.to_dict()
    # Сравнение словарей не видит порядка — проверяем его явно.
    assert list(forward.to_dict()["sources"]) == ["alpha", "beta"]
    assert list(forward.to_dict()["operations"]) == ["api.a", "api.b"]
    assert dump_manifest(forward) == dump_manifest(backward)


def test_to_dict_omits_absent_optional_fields() -> None:
    canonical = parse(document()).to_dict()
    entry = canonical["operations"]["api.createDocument"]
    assert entry == {"source": "main", "operation_id": "createDocument"}
    assert canonical["sources"]["main"] == {"path": "api/openapi.yaml", "selection": "explicit"}


def test_dump_manifest_is_loadable(tmp_path: Path) -> None:
    manifest = parse(document(), base_dir=tmp_path)
    path = tmp_path / "manifest.yaml"
    path.write_text(dump_manifest(manifest), encoding="utf-8")
    assert load_manifest(path).to_dict() == manifest.to_dict()


def test_default_manifest_document_is_valid() -> None:
    data = default_manifest_document(
        directory="pkg/generated", package="pkg.generated", source_path="api/openapi.yaml"
    )
    manifest = parse(data)
    assert manifest.operations == ()
    assert manifest.source("main").path == "api/openapi.yaml"


# -------------------------------------------------------------------- загрузка


def test_load_manifest_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(document(), allow_unicode=True), encoding="utf-8")

    manifest = load_manifest(path)

    assert manifest.base_dir == tmp_path.resolve()
    assert manifest.source_path("main") == (tmp_path / "api/openapi.yaml").resolve()
    # Корень межфайловых $ref по умолчанию — каталог самой спецификации.
    assert manifest.source_root("main") == (tmp_path / "api").resolve()
    assert manifest.output_dir() == (tmp_path / "pkg/generated").resolve()


def test_load_manifest_from_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document()), encoding="utf-8")

    manifest = load_manifest(path)

    assert manifest.output_package == "pkg.generated"
    assert manifest.operation("api.createDocument") is not None


def test_source_root_can_be_pinned(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    data = document(sources={"main": {"path": "api/v1/openapi.yaml", "root": "api"}})
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    manifest = load_manifest(path)

    assert manifest.source_root("main") == (tmp_path / "api").resolve()


def test_missing_manifest_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="manifest не найден"):
        load_manifest(tmp_path / "nowhere.yaml")


def test_directory_instead_of_manifest(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="manifest не найден"):
        load_manifest(tmp_path)


@pytest.mark.parametrize(
    ("name", "text"),
    [("manifest.yaml", "version: [1\n"), ("manifest.json", "{'version': 1,}")],
)
def test_broken_document(tmp_path: Path, name: str, text: str) -> None:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ManifestError, match="не разбирается"):
        load_manifest(path)


def test_scalar_document_is_not_a_manifest(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="manifest: ожидался объект"):
        load_manifest(path)


def test_manifest_errors_are_contract_errors() -> None:
    """Потребителю достаточно одного ``except ContractError``."""
    with pytest.raises(ContractError):
        parse(document(version=2))
    with pytest.raises(ContractError):
        parse(
            document(
                operations={"api.getItem": {"source": "main"}, "api.get_item": {"source": "main"}}
            )
        )

"""Тесты жизненного цикла waiver'ов.

Waiver — единственный способ пропустить конструкцию, которую нормализация иначе
отклонила бы, и он намеренно неудобен. Здесь проверяется весь цикл: библиотека
печатает готовую заготовку → её копируют в ``waivers.yaml`` → генерация проходит →
через срок или после правки спецификации waiver перестаёт действовать.

Отдельно проверяется изоляция: послабление на общий ``$ref`` не имеет права
ослабить другую операцию, которая ссылается на ту же именованную схему.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest

from geas.errors import (
    ContractError,
    ManifestError,
    UnsupportedConstructError,
    WaiverError,
)
from geas.fingerprints import semantic_source_digest
from geas.waivers import waiver_source_digest
from support import Project, make_project, spec

#: Ключ операции из фикстуры ``features/no_type.yaml``.
NO_TYPE_KEY = "api.getNote"
#: Contract path свойства без контракта в той же фикстуре.
NO_TYPE_POINTER = "/body/payload"

#: Спецификация с двумя операциями поверх одной общей схемы: свойство ``note``
#: объявлено обязательным, и ослаблять его можно только точечно.
SHARED_STRICT = """openapi: "3.0.3"
info:
  title: Shared component fixture
  version: "1.0.0"
paths:
  /alpha:
    get:
      operationId: getAlpha
      responses:
        "200":
          description: Карточка
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Card"
  /beta:
    get:
      operationId: getBeta
      responses:
        "200":
          description: Та же карточка
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Card"
components:
  schemas:
    Card:
      type: object
      required: [id, note]
      properties:
        id:
          type: string
        note:
          type: string
          maxLength: 10
"""

#: То же самое, но общее свойство вообще без контракта: без waiver'а операция
#: не собирается.
SHARED_UNCONSTRAINED = SHARED_STRICT.replace(
    """    Card:
      type: object
      required: [id, note]
      properties:
        id:
          type: string
        note:
          type: string
          maxLength: 10
""",
    """    Card:
      type: object
      required: [id]
      properties:
        id:
          type: string
        payload:
          description: Ограничений нет
""",
)

#: Спецификация без единой проблемы: на ней проверяются лишние waiver'ы и
#: правила ``non_waivable``.
CLEAN = """openapi: "3.0.3"
info:
  title: Clean fixture
  version: "1.0.0"
paths:
  /cards:
    get:
      operationId: getCard
      responses:
        "200":
          description: Карточка
          content:
            application/json:
              schema:
                type: object
                required: [rc]
                properties:
                  rc:
                    type: string
                    enum: [ok, fail]
                  note:
                    type: string
                    nullable: true
"""

#: Свойство без контракта, вынесенное в отдельный файл, чтобы его можно было
#: править прямо в тесте (проверка устаревания waiver'а).
MUTABLE = """openapi: "3.0.3"
info:
  title: Mutable fixture
  version: "1.0.0"
paths:
  /notes:
    get:
      operationId: getNote
      responses:
        "200":
          description: Заметка
          content:
            application/json:
              schema:
                type: object
                properties:
                  payload:
                    description: ОПИСАНИЕ
"""


def build_project(
    root: Path,
    *,
    source: Path | str,
    operations: dict[str, Any],
    policies: dict[str, Any] | None = None,
) -> Project:
    """Изолированный проект с одной спецификацией и заданным manifest."""
    project = make_project(root)
    project.write_spec("api/openapi.yaml", source)
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "selection": "explicit"}},
        operations=operations,
        policies=policies,
    )
    return project


def no_type_project(root: Path, **kwargs: Any) -> Project:
    """Проект на фикстуре ``features/no_type.yaml``."""
    return build_project(
        root,
        source=spec("features", "no_type.yaml"),
        operations={NO_TYPE_KEY: {"source": "main", "operation_id": "getNote"}},
        **kwargs,
    )


def parse_stanza(message: str) -> dict[str, str]:
    """Разобрать заготовку waiver'а из сообщения об ошибке.

    Это ровно то, что делает человек: находит блок ``- operation: ...`` и
    копирует его в ``waivers.yaml``. Хвост с координатами ошибки, который
    печатается после последнего поля, отбрасывается.
    """
    lines = message.splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.lstrip().startswith("- operation:")
    )
    stanza: dict[str, str] = {}
    for line in lines[start:]:
        text = line.strip().removeprefix("- ")
        key, _, value = text.partition(":")
        stanza[key.strip()] = value.split(" [", 1)[0].strip()
    return stanza


def today() -> dt.date:
    return dt.date.today()


def in_days(days: int) -> dt.date:
    return today() + dt.timedelta(days=days)


def waiver(**overrides: Any) -> dict[str, Any]:
    """Полностью заполненный waiver для фикстуры ``no_type``."""
    data: dict[str, Any] = {
        "operation": NO_TYPE_KEY,
        "direction": "response",
        "json_pointer": NO_TYPE_POINTER,
        "rule": "allow_any",
        "reason": "бэкенд обещал описать поле в следующем релизе",
        "owner": "team-documents",
        "issue": "ISSUE-1",
        "expires_at": in_days(30),
        "expected_source": semantic_source_digest({}),
    }
    data.update(overrides)
    return data


# ------------------------------------------------- цикл «ошибка → waiver → сборка»


def test_generation_prints_ready_to_paste_stanza(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    message = str(info.value)
    stanza = parse_stanza(message)
    assert stanza["operation"] == NO_TYPE_KEY
    assert stanza["direction"] == "response"
    assert stanza["json_pointer"] == NO_TYPE_POINTER
    assert stanza["rule"] == "allow_any"
    # Отпечаток печатается готовым значением, а не «посчитайте сами».
    assert re.fullmatch(r"[0-9a-f]{64}", stanza["expected_source"])
    assert stanza["expected_source"] == semantic_source_digest({})
    # Поля, которые обязан заполнить человек, оставлены плейсхолдерами.
    assert stanza["reason"].startswith("<")
    assert stanza["owner"].startswith("<")
    assert stanza["issue"].startswith("<")
    assert stanza["expires_at"].startswith("<")
    # Рядом с contract path печатается исходный JSON Pointer.
    assert info.value.json_pointer is not None
    assert "properties/payload" in info.value.json_pointer


def test_copy_pasted_stanza_unblocks_generation(tmp_path: Path) -> None:
    """Скопированная из ошибки заготовка + четыре поля от человека = рабочий waiver."""
    project = no_type_project(tmp_path / "project")
    with pytest.raises(UnsupportedConstructError) as info:
        project.build()
    stanza = parse_stanza(str(info.value))

    stanza["reason"] = "поле придёт в следующей версии спецификации"
    stanza["owner"] = "team-documents"
    stanza["issue"] = "ISSUE-42"
    project.write_waivers([{**stanza, "expires_at": in_days(30)}])

    result = project.build()

    document = result.by_key(NO_TYPE_KEY).document
    # Точка, накрытая waiver'ом, становится «любым значением» — и только она.
    schema = document["responses"][0]["schema"]
    assert schema["properties"] == {"payload": {}}


def test_waiver_narrowed_to_another_variant_does_not_apply(tmp_path: Path) -> None:
    """Сужение по статусу — часть области действия, а не украшение."""
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(status=404)])

    with pytest.raises(UnsupportedConstructError):
        project.build()


# ------------------------------------------------------------------- сроки


def test_expired_waiver(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(expires_at=in_days(-1))])

    with pytest.raises(WaiverError) as info:
        project.waivers().validate(project.load(), today=today())

    message = str(info.value)
    assert "waiver истёк" in message
    assert in_days(-1).isoformat() in message


def test_expired_waiver_fails_the_cli(tmp_path: Path) -> None:
    """Сквозная проверка: просроченный waiver роняет ``geas check``."""
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(expires_at=in_days(-1))])

    result = project.cli("check")

    assert result.returncode == 1
    assert "waiver истёк" in result.stderr


def test_waiver_expiring_today_is_still_valid(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(expires_at=today())])

    project.waivers().validate(project.load(), today=today())


def test_waiver_beyond_policy_horizon(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(expires_at=in_days(200))])

    with pytest.raises(WaiverError) as info:
        project.waivers().validate(project.load(), today=today())

    assert "policies.waiver_max_days" in str(info.value)


def test_policy_horizon_is_configurable(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project", policies={"waiver_max_days": 365})
    project.write_waivers([waiver(expires_at=in_days(200))])

    project.waivers().validate(project.load(), today=today())


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        # YAML сам разбирает дату со временем в datetime — подкласс date,
        # который без явной проверки проскочил бы и сравнивался бы криво.
        ("2099-01-01T10:00:00Z", "ожидалась календарная дата без времени"),
        ("2099-01-01 10:00:00", "ожидалась календарная дата без времени"),
        ("'позавчера'", "не разбирается как YYYY-MM-DD"),
        ("'2099-13-01'", "не разбирается как YYYY-MM-DD"),
        ("20990101", "ожидалась дата YYYY-MM-DD"),
        ("[2099, 1, 1]", "ожидалась дата YYYY-MM-DD"),
    ],
)
def test_expires_at_must_be_a_bare_date(tmp_path: Path, raw: str, why: str) -> None:
    project = no_type_project(tmp_path / "project")
    project.write(
        "waivers.yaml",
        f"""
        version: 1
        waivers:
          - operation: {NO_TYPE_KEY}
            direction: response
            json_pointer: {NO_TYPE_POINTER}
            rule: allow_any
            reason: причина
            owner: team-documents
            issue: ISSUE-1
            expires_at: {raw}
            expected_source: {semantic_source_digest({})}
        """,
    )

    with pytest.raises(WaiverError) as info:
        project.waivers()

    assert why in str(info.value)


# -------------------------------------------------------------- неполнота


@pytest.mark.parametrize(
    "field",
    ["operation", "direction", "json_pointer", "rule", "reason", "owner", "expires_at"],
)
def test_incomplete_waiver_is_rejected(tmp_path: Path, field: str) -> None:
    project = no_type_project(tmp_path / "project")
    incomplete = waiver()
    del incomplete[field]
    project.write_waivers([incomplete])

    with pytest.raises(WaiverError) as info:
        project.waivers()

    assert "Неполный waiver не принимается" in str(info.value)
    assert field in str(info.value)


@pytest.mark.parametrize("field", ["reason", "owner", "issue"])
def test_empty_string_counts_as_missing(tmp_path: Path, field: str) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(**{field: ""})])

    with pytest.raises(WaiverError):
        project.waivers()


def test_issue_is_required(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    without_issue = waiver()
    del without_issue["issue"]
    project.write_waivers([without_issue])

    with pytest.raises(WaiverError, match="обязательно поле 'issue'"):
        project.waivers()


def test_issue_and_ticket_are_mutually_exclusive(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(ticket="ISSUE-2")])

    with pytest.raises(WaiverError, match="но не оба"):
        project.waivers()


def test_ticket_is_accepted_as_an_alias(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    aliased = waiver()
    aliased["ticket"] = aliased.pop("issue")
    project.write_waivers([aliased])

    assert project.waivers().waivers[0].issue == "ISSUE-1"


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("", "sha256-отпечаток"),
        ("deadbeef", "sha256-отпечаток"),
        (None, "sha256-отпечаток"),
    ],
)
def test_expected_source_is_mandatory(tmp_path: Path, value: Any, why: str) -> None:
    project = no_type_project(tmp_path / "project")
    data = waiver()
    if value is None:
        del data["expected_source"]
    else:
        data["expected_source"] = value
    project.write_waivers([data])

    with pytest.raises(WaiverError) as info:
        project.waivers()

    assert why in str(info.value)


def test_unknown_field_in_waiver(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(comment="почему бы и нет")])

    with pytest.raises(WaiverError, match=r"неизвестные поля \['comment'\]"):
        project.waivers()


@pytest.mark.parametrize(
    ("data", "why"),
    [
        ({"rule": "replace_schema"}, "требует поля 'replacement'"),
        ({"replacement": {"type": "string"}}, "допустимо только для правил"),
        ({"rule": "make_it_work"}, "не входит в"),
        ({"direction": "sideways"}, "ожидалось 'request' или 'response'"),
    ],
)
def test_malformed_waiver_fields(tmp_path: Path, data: dict[str, Any], why: str) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(**data)])

    with pytest.raises(WaiverError) as info:
        project.waivers()

    assert why in str(info.value)


def test_replace_schema_substitutes_the_fragment(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers(
        [waiver(rule="replace_schema", replacement={"type": "string", "maxLength": 64})]
    )

    document = project.build().by_key(NO_TYPE_KEY).document

    assert document["responses"][0]["schema"]["properties"]["payload"] == {
        "type": "string",
        "maxLength": 64,
    }


# ------------------------------------------------- лишние и противоречивые


def test_unused_waiver_is_an_error(tmp_path: Path) -> None:
    project = build_project(
        tmp_path / "project",
        source=CLEAN,
        operations={"api.getCard": {"source": "main", "operation_id": "getCard"}},
    )
    project.write_waivers(
        [
            waiver(
                operation="api.getCard",
                json_pointer="/body/nowhere",
                expected_source="0" * 64,
            )
        ]
    )

    with pytest.raises(WaiverError) as info:
        project.build()

    message = str(info.value)
    assert "waiver'ы больше не нужны" in message
    assert "/body/nowhere" in message


def test_waiver_for_unknown_operation(tmp_path: Path) -> None:
    project = build_project(
        tmp_path / "project",
        source=CLEAN,
        operations={"api.getCard": {"source": "main", "operation_id": "getCard"}},
    )
    project.write_waivers([waiver(operation="api.ghost", json_pointer="/body/note")])

    with pytest.raises(WaiverError, match=re.escape("неизвестную операцию 'api.ghost'")):
        project.build()


def test_duplicate_waiver(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(), waiver()])

    with pytest.raises(WaiverError, match="объявлен дважды"):
        project.waivers()


def test_compatible_rules_on_one_scope_are_allowed(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(rule="allow_null"), waiver(rule="relax_required")])

    assert len(project.waivers().waivers) == 2


def test_schema_replacement_conflicts_with_another_rule_on_same_scope(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers(
        [
            waiver(rule="replace_schema", replacement={"type": "string"}),
            waiver(rule="relax_required"),
        ]
    )

    with pytest.raises(WaiverError, match="несовместимые правила"):
        project.waivers()


def test_same_path_different_variants_is_not_a_conflict(tmp_path: Path) -> None:
    """Область действия включает вариант ответа, поэтому статусы не конфликтуют."""
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(status=200), waiver(rule="relax_required", status=400)])

    assert len(project.waivers().waivers) == 2


# ---------------------------------------------------------------- устаревание


def mutable_project(root: Path, description: str = "ОПИСАНИЕ", extra: str = "") -> Project:
    """Проект, спецификацию которого тест правит прямо по месту."""
    text = MUTABLE.replace("ОПИСАНИЕ", description)
    if extra:
        text = text.replace(
            "                    description: " + description,
            "                    description: " + description + "\n                    " + extra,
        )
    return build_project(
        root,
        source=text,
        operations={NO_TYPE_KEY: {"source": "main", "operation_id": "getNote"}},
    )


def test_waiver_goes_stale_when_the_source_changes(tmp_path: Path) -> None:
    project = mutable_project(tmp_path / "project")
    project.write_waivers([waiver()])
    project.build()  # исходное состояние собирается

    # Правка спецификации по смыслу: nullable — это уже другой фрагмент.
    project.write_spec(
        "api/openapi.yaml", MUTABLE.replace("description: ОПИСАНИЕ", "nullable: true")
    )

    with pytest.raises(WaiverError) as info:
        project.build()

    message = str(info.value)
    assert "устарел" in message
    assert waiver()["expected_source"] in message
    assert semantic_source_digest({"nullable": True}) in message


def test_annotation_only_edit_keeps_the_waiver_valid(tmp_path: Path) -> None:
    """Отпечаток считается по смыслу: правка ``description`` waiver не ломает."""
    project = mutable_project(tmp_path / "project")
    project.write_waivers([waiver()])
    project.build()

    project.write_spec("api/openapi.yaml", MUTABLE.replace("ОПИСАНИЕ", "совсем другой текст"))

    project.build()


# ------------------------------------------------------------- non_waivable


def clean_project(root: Path, non_waivable: list[dict[str, Any]] | None = None) -> Project:
    entry: dict[str, Any] = {"source": "main", "operation_id": "getCard"}
    if non_waivable is not None:
        entry["non_waivable"] = non_waivable
    return build_project(root, source=CLEAN, operations={"api.getCard": entry})


def test_satisfied_non_waivable_rules_pass(tmp_path: Path) -> None:
    project = clean_project(
        tmp_path / "project",
        [
            {
                "direction": "response",
                "json_pointer": "/body/rc",
                "rules": ["present", "required", "non_null", "non_empty_enum"],
            }
        ],
    )

    assert project.build().by_key("api.getCard").contract.key == "api.getCard"


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        ("required", "оставался обязательным"),
        ("non_null", "запрещает null"),
        ("non_empty_enum", "требует непустой enum"),
    ],
)
def test_violated_non_waivable_rule(tmp_path: Path, rule: str, why: str) -> None:
    # /body/note необязателен, nullable и без enum — нарушает все три правила.
    project = clean_project(
        tmp_path / "project",
        [{"direction": "response", "json_pointer": "/body/note", "rules": [rule]}],
    )

    with pytest.raises(ManifestError) as info:
        project.build()

    assert why in str(info.value)


def test_non_waivable_path_must_exist(tmp_path: Path) -> None:
    project = clean_project(
        tmp_path / "project",
        [{"direction": "response", "json_pointer": "/body/ghost", "rules": ["present"]}],
    )

    with pytest.raises(ManifestError, match="не найден в контракте"):
        project.build()


def test_non_waivable_direction_must_exist(tmp_path: Path) -> None:
    project = clean_project(
        tmp_path / "project",
        [{"direction": "request", "json_pointer": "/body/rc", "rules": ["present"]}],
    )

    with pytest.raises(ManifestError, match="нет тела в направлении request"):
        project.build()


@pytest.mark.parametrize("pointer", ["/body/rc", "/body", "/"])
def test_waiver_overlapping_non_waivable(tmp_path: Path, pointer: str) -> None:
    """Пересечение считается по префиксу: waiver выше по дереву тоже запрещён."""
    project = clean_project(
        tmp_path / "project",
        [{"direction": "response", "json_pointer": "/body/rc", "rules": ["present"]}],
    )
    project.write_waivers(
        [waiver(operation="api.getCard", json_pointer=pointer, expected_source="0" * 64)]
    )

    with pytest.raises(WaiverError) as info:
        project.waivers().validate(project.load(), today=today())

    assert "пересекается с non_waivable" in str(info.value)


def test_waiver_beside_non_waivable_is_allowed(tmp_path: Path) -> None:
    """Соседняя ветка контракта под non_waivable не попадает."""
    project = clean_project(
        tmp_path / "project",
        [{"direction": "response", "json_pointer": "/body/rc", "rules": ["present"]}],
    )
    project.write_waivers(
        [
            waiver(
                operation="api.getCard",
                json_pointer="/body/note",
                expected_source=semantic_source_digest({"type": "string", "nullable": True}),
            )
        ]
    )

    project.waivers().validate(project.load(), today=today())
    project.build()


# ------------------------------------------------------------------ изоляция


def shared_project(root: Path, source: str) -> Project:
    return build_project(
        root,
        source=source,
        operations={
            "api.getAlpha": {"source": "main", "operation_id": "getAlpha"},
            "api.getBeta": {"source": "main", "operation_id": "getBeta"},
        },
    )


def test_waiver_on_shared_ref_does_not_weaken_the_other_operation(tmp_path: Path) -> None:
    """``relax_required`` для одной операции не трогает контракт второй."""
    project = shared_project(tmp_path / "project", SHARED_STRICT)
    project.write_waivers(
        [
            waiver(
                operation="api.getAlpha",
                json_pointer="/body/note",
                rule="relax_required",
                expected_source=semantic_source_digest({"type": "string", "maxLength": 10}),
            )
        ]
    )

    result = project.build()

    alpha = result.by_key("api.getAlpha").document["responses"][0]["schema"]
    beta = result.by_key("api.getBeta").document["responses"][0]["schema"]
    # У операции с waiver'ом $ref развёрнут по месту и note стал необязательным.
    assert alpha["required"] == ["id"]
    assert "note" in alpha["properties"]
    # Вторая операция продолжает ссылаться на общую схему в исходном виде.
    assert beta["$defs"]["Card"]["required"] == ["id", "note"]


def test_waiver_on_shared_ref_does_not_unblock_the_other_operation(tmp_path: Path) -> None:
    """``allow_any`` для одной операции не делает общую схему допустимой везде."""
    project = shared_project(tmp_path / "project", SHARED_UNCONSTRAINED)
    project.write_waivers([waiver(operation="api.getAlpha", json_pointer="/body/payload")])

    with pytest.raises(UnsupportedConstructError) as info:
        project.build()

    # Падает именно вторая операция — первая уже собрана с послаблением.
    assert info.value.operation_key == "api.getBeta"


def test_the_same_waiver_for_both_operations_is_written_twice(tmp_path: Path) -> None:
    """Общий ``$ref`` не даёт скидки: послабление выписывается на каждую операцию."""
    project = shared_project(tmp_path / "project", SHARED_UNCONSTRAINED)
    project.write_waivers(
        [
            waiver(operation="api.getAlpha", json_pointer="/body/payload"),
            waiver(operation="api.getBeta", json_pointer="/body/payload"),
        ]
    )

    result = project.build()

    for key in ("api.getAlpha", "api.getBeta"):
        schema = result.by_key(key).document["responses"][0]["schema"]
        assert schema["properties"]["payload"] == {}


# ------------------------------------------------------------------ файл


def test_missing_waivers_file_is_an_empty_set(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.waivers_path.unlink()

    assert project.waivers().waivers == ()


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", None),
        ("version: 1\nwaivers:\n", None),
        ("version: 1\nwaivers: []\n", None),
        ("version: 2\nwaivers: []\n", "version должен быть целым 1"),
        ("version: true\nwaivers: []\n", "version должен быть целым 1"),
        ("version: 1\nwaivers: {}\n", None),
        ("version: 1\nwaivers: []\nextra: 1\n", "неизвестные ключи ['extra']"),
        ("version: 1\nwaivers: 'нет'\n", "ожидался список"),
        ("- just a list\n", "ожидался объект"),
    ],
)
def test_waivers_document_shape(tmp_path: Path, text: str, why: str | None) -> None:
    project = no_type_project(tmp_path / "project")
    project.waivers_path.write_text(text, encoding="utf-8")

    if why is None:
        assert project.waivers().waivers == ()
    else:
        with pytest.raises(WaiverError) as info:
            project.waivers()
        assert why in str(info.value)


def test_waiver_set_to_dict_is_canonical(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers(
        [
            waiver(operation="api.zeta", json_pointer="/body/b"),
            waiver(operation="api.alpha", json_pointer="/body/a"),
        ]
    )

    document = project.waivers().to_dict()

    assert document["version"] == 1
    assert [item["operation"] for item in document["waivers"]] == ["api.alpha", "api.zeta"]
    # Дата сериализуется обратно в строгий календарный вид.
    assert document["waivers"][0]["expires_at"] == in_days(30).isoformat()


def test_broken_waivers_yaml(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.waivers_path.write_text("version: [1\n", encoding="utf-8")

    with pytest.raises(WaiverError, match="не разбирается"):
        project.waivers()


def test_broken_contract_path_in_waiver(tmp_path: Path) -> None:
    """Битый указатель — ошибка waiver'а с именем файла и индексом записи."""
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(json_pointer="body/payload")])

    with pytest.raises(WaiverError) as info:
        project.waivers()

    message = str(info.value)
    assert "waivers.yaml.waivers[0].json_pointer" in message
    assert "должен начинаться с '/'" in message


def test_waiver_errors_are_contract_errors(tmp_path: Path) -> None:
    project = no_type_project(tmp_path / "project")
    project.write_waivers([waiver(expires_at=in_days(-1))])

    with pytest.raises(ContractError):
        project.waivers().validate(project.load(), today=today())


# --------------------------------------- исправление дефектных request-моделей

DEFECTIVE_REQUEST = """swagger: "2.0"
info: {title: Defective request, version: "1.0.0"}
basePath: /api
paths:
  /tickets:
    post:
      operationId: addTicket
      consumes: [application/json]
      produces: [application/json]
      parameters:
        - in: body
          name: body
          required: true
          schema: {$ref: "#/definitions/CreateTicket"}
      responses:
        "200":
          description: ok
          schema: {type: string}
definitions:
  CreateTicket:
    type: object
    additionalProperties: false
    required: [id, kind]
    properties:
      id: {type: string, readOnly: true}
      kind: {type: string, enum: [BASE]}
      note: {type: string}
"""


def defective_request_project(root: Path) -> Project:
    """Swagger-проект с типичными дефектами generated request-модели."""
    return build_project(
        root,
        source=DEFECTIVE_REQUEST,
        operations={"ws.addTicket": {"source": "main", "operation_id": "addTicket"}},
    )


def request_waiver(*, pointer: str, rule: str, expected: str, **extra: Any) -> dict[str, Any]:
    """Полный waiver для дефектной request-модели."""
    return {
        "operation": "ws.addTicket",
        "direction": "request",
        "json_pointer": pointer,
        "rule": rule,
        "reason": "исходный Swagger неверно описывает фактический запрос",
        "owner": "team-api",
        "issue": "BUG-42",
        "expires_at": in_days(30),
        "expected_source": expected,
        **extra,
    }


def test_scoped_request_waivers_repair_defective_swagger(tmp_path: Path) -> None:
    """Direction, nullable, enum и отсутствующее поле исправляются точечно."""
    project = defective_request_project(tmp_path / "project")
    project.write_waivers(
        [
            request_waiver(
                pointer="/body/id",
                rule="ignore_read_only",
                expected=waiver_source_digest(
                    {"type": "string", "readOnly": True}, "ignore_read_only"
                ),
            ),
            request_waiver(
                pointer="/body/note",
                rule="allow_null",
                expected=semantic_source_digest({"type": "string"}),
            ),
            request_waiver(
                pointer="/body/kind",
                rule="extend_enum",
                expected=semantic_source_digest({"type": "string", "enum": ["BASE"]}),
                values=["IN", "OUT"],
            ),
            request_waiver(
                pointer="/body/contentType",
                rule="add_property",
                expected=semantic_source_digest(None),
                replacement={"type": "string", "enum": ["HTML", "PLAIN"]},
            ),
        ]
    )

    schema = project.build().by_key("ws.addTicket").document["request"]["bodies"][0]["schema"]

    assert schema["required"] == ["id", "kind"]
    assert schema["properties"]["note"]["type"] == ["string", "null"]
    assert schema["properties"]["kind"]["enum"] == ["BASE", "IN", "OUT"]
    assert schema["properties"]["contentType"]["enum"] == ["HTML", "PLAIN"]
    assert "contentType" not in schema["required"]


def test_direction_waiver_becomes_unused_after_source_is_fixed(tmp_path: Path) -> None:
    """После удаления ошибочного readOnly старый waiver обязан сломать генерацию."""
    project = defective_request_project(tmp_path / "project")
    project.write_waivers(
        [
            request_waiver(
                pointer="/body/id",
                rule="ignore_read_only",
                expected=waiver_source_digest(
                    {"type": "string", "readOnly": True}, "ignore_read_only"
                ),
            )
        ]
    )
    project.write_spec("api/openapi.yaml", DEFECTIVE_REQUEST.replace(", readOnly: true", ""))

    with pytest.raises(WaiverError, match="больше не нужны"):
        project.build()

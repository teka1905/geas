"""Overlay'и генераторов: подмена листа без потери контракта.

Смысл overlay'я — сделать фикстуру осмысленной, ничего не ослабив. Поэтому здесь
проверяется не «подменилось», а именно граница: что подменить **можно**, что
**нельзя**, и что происходит, когда ручной генератор выдаёт значение вне
контракта.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from d42 import fake, optional, schema
from d42.utils import make_required

from geas.errors import ContractOverlayError
from geas.integrations.d42 import EACH, build_fixture, overlay_generators
from geas.integrations.d42.overlays import original_contract
from geas.models import Direction
from geas.runtime.validation import validate_instance
from support import make_project, spec


@pytest.fixture()
def generated() -> Any:
    """Типичная generated-схема: объект, вложенный массив, enum, nullable."""
    return schema.dict(
        {
            "name": schema.str.len(1, 16),
            optional("colour"): schema.str("red") | schema.str("green") | schema.none,
            "groups": schema.list(
                schema.dict(
                    {
                        "title": schema.str.len(1, 8),
                        "members": schema.list(schema.dict({"login": schema.str.len(1, 12)})),
                    }
                )
            ),
            ...: ...,
        }
    )


# ------------------------------------------------------------ что работает


def test_leaf_generator_is_replaced(generated: Any) -> None:
    """Подменяется ровно указанный лист."""
    result = overlay_generators(generated, {("name",): schema.str("Отдел продаж")})

    assert build_fixture(result, seed=1)["name"] == "Отдел продаж"


def test_structure_and_optionality_stay_generated(generated: Any) -> None:
    """Overlay не трогает ни структуру, ни обязательность ключей."""
    result = overlay_generators(generated, {("name",): schema.str("Отдел")})
    keys = result.props.keys

    assert set(keys) == set(generated.props.keys)
    optionality = {name: value[1] for name, value in keys.items() if isinstance(name, str)}
    assert optionality == {
        "name": False,
        "colour": True,
        "groups": False,
    }


def test_input_schema_is_not_mutated(generated: Any) -> None:
    """Исходная generated-схема остаётся нетронутой."""
    before = repr(generated)
    overlay_generators(generated, {("name",): schema.str("Отдел")})

    assert repr(generated) == before


def test_each_descends_into_an_array(generated: Any) -> None:
    """``EACH`` — это «каждый элемент массива»."""
    result = overlay_generators(generated, {("groups", EACH, "title"): schema.str("Гр")})

    assert {item["title"] for item in build_fixture(result, seed=2)["groups"]} == {"Гр"}


def test_nested_each_works(generated: Any) -> None:
    """Вложенные массивы адресуются несколькими ``EACH``."""
    result = overlay_generators(
        generated, {("groups", EACH, "members", EACH, "login"): schema.str("ivan")}
    )
    fixture = build_fixture(result, seed=3)

    logins = {member["login"] for group in fixture["groups"] for member in group["members"]}
    assert logins <= {"ivan"}


def test_overlay_through_nullable_keeps_the_null_branch(generated: Any) -> None:
    """Спуск через nullable сохраняет ветку ``schema.none``."""
    result = overlay_generators(generated, {("colour",): schema.str("red")})
    colour = dict(result.props.keys)["colour"][0]

    assert "schema.none" in repr(colour)
    assert "schema.str('red')" in repr(colour)


def test_several_overrides_apply_together(generated: Any) -> None:
    """Несколько подмен применяются независимо друг от друга."""
    result = overlay_generators(
        generated,
        {("name",): schema.str("Отдел"), ("groups", EACH, "title"): schema.str("Гр")},
    )
    fixture = build_fixture(result, seed=4)

    assert fixture["name"] == "Отдел"
    assert all(item["title"] == "Гр" for item in fixture["groups"])


def test_fake_substitute_and_make_required_still_work(generated: Any) -> None:
    """Результат overlay'я — обычная схема d42."""
    result = overlay_generators(generated, {("name",): schema.str("Отдел")})

    assert fake(result)["name"] == "Отдел"
    assert "schema.str('Отдел')" in repr(result % {"name": "Отдел"})
    assert make_required(result, {"colour"}).props.keys["colour"][1] is False


def test_original_contract_is_remembered(generated: Any) -> None:
    """Исходный контракт остаётся доступен — по нему проверяются значения."""
    result = overlay_generators(generated, {("name",): schema.str("Отдел")})

    assert original_contract(result) is not None


# --------------------------------------------------------------- что нельзя


def test_missing_path_fails_at_build_time(generated: Any) -> None:
    """Переименованное поле ломает overlay сразу, а не через неделю в тесте."""
    with pytest.raises(ContractOverlayError) as info:
        overlay_generators(generated, {("nmae",): schema.str("x")})

    assert "/nmae" in str(info.value)
    assert "'name'" in str(info.value)


def test_forgotten_each_fails(generated: Any) -> None:
    """Путь через массив без ``EACH`` — ошибка с указанием пути."""
    with pytest.raises(ContractOverlayError) as info:
        overlay_generators(generated, {("groups", "title"): schema.str("x")})

    assert "/groups/title" in str(info.value)


def test_descending_into_a_leaf_fails(generated: Any) -> None:
    """Внутрь листа спускаться некуда."""
    with pytest.raises(ContractOverlayError):
        overlay_generators(generated, {("name", "inner"): schema.str("x")})


def test_replacing_a_container_is_rejected(generated: Any) -> None:
    """Структура всегда остаётся сгенерированной — контейнер подменять нельзя."""
    with pytest.raises(ContractOverlayError) as info:
        overlay_generators(generated, {("groups",): schema.list(schema.str)})

    assert "лист" in str(info.value)


def test_type_mismatch_is_rejected_eagerly(generated: Any) -> None:
    """Тип ручной схемы обязан совпасть с типом листа — проверка немедленная."""
    with pytest.raises(ContractOverlayError) as info:
        overlay_generators(generated, {("name",): schema.int(5)})

    assert "тип" in str(info.value)


def test_value_outside_the_generated_enum_is_rejected_eagerly(generated: Any) -> None:
    """Значение вне enum контракта отвергается при сборке overlay'я."""
    with pytest.raises(ContractOverlayError) as info:
        overlay_generators(generated, {("colour",): schema.str("purple")})

    assert "enum" in str(info.value)


def test_value_violating_bounds_is_rejected_at_generation(generated: Any) -> None:
    """Границы разрешимы только на значении — проверка происходит при генерации."""
    result = overlay_generators(generated, {("name",): schema.str("имя-длиннее-шестнадцати")})

    with pytest.raises(ContractOverlayError) as info:
        build_fixture(result, seed=5)

    assert "контракт" in str(info.value)


def test_non_schema_override_is_rejected(generated: Any) -> None:
    """В значении overlay'я обязана быть схема d42, а не что попало."""
    with pytest.raises(ContractOverlayError):
        overlay_generators(generated, {("name",): "просто строка"})  # type: ignore[dict-item]


# ---------------------------------------------- контракт остаётся широким


def test_overlaid_schema_still_validates_the_whole_contract(generated: Any) -> None:
    """Подмена одного листа не отключает проверку остальных полей."""
    result = overlay_generators(generated, {("name",): schema.str("Отдел")})
    from d42 import ValidationException, validate_or_fail

    # groups обязателен и остаётся обязательным.
    with pytest.raises(ValidationException):
        validate_or_fail(result, {"name": "Отдел"})
    # title внутри groups по-прежнему обязан быть строкой.
    with pytest.raises(ValidationException):
        validate_or_fail(result, {"name": "Отдел", "groups": [{"title": 1, "members": []}]})


def test_overlay_fixture_is_still_checked_against_the_generated_contract(tmp_path: Path) -> None:
    """Сквозная гарантия: фикстура из overlay'я проверяется контрактом операции.

    Даже если ручной генератор шире контракта, значение не доедет до мока: перед
    регистрацией ответа ``validate_response`` проверяет его по **сгенерированной**
    схеме и по JSON Schema, а не по overlay'ю.
    """
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("basic", "openapi30.yaml"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={"api.createDocument": {"source": "main", "operation_id": "createDocument"}},
    )
    built = project.build().by_key("api.createDocument")
    response = next(item for item in built.document["responses"] if item["schema"])

    with pytest.raises(Exception) as info:
        validate_instance(
            response["schema"],
            {"id": 1},
            operation_key="api.createDocument",
            direction=Direction.RESPONSE,
            part="тело ответа",
        )

    assert "api.createDocument" in str(info.value)

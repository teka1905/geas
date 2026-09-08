"""Детерминированная генерация фикстур.

Фикстура обязана быть стабильной: если она «дрожит» между прогонами, тест
перестаёт быть проверкой контракта и превращается в лотерею. Здесь проверяются
три свойства, ради которых генерация вообще обёрнута своим кодом, а не сводится
к вызову ``fake()``:

1. один и тот же seed даёт один и тот же результат;
2. добавление **необязательного** поля не меняет уже стабильную фикстуру;
3. добавление варианта **в конец** объединения не переключает фикстуру на него.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from d42 import optional, schema, validate_or_fail

from geas.errors import ContractOverlayError
from geas.integrations.d42 import build_fixture, overlay_generators
from geas.integrations.d42.fixtures import (
    project_first_variant,
    validate_overlay_fixture,
)
from geas.models import Direction
from geas.runtime.validation import validate_instance
from support import make_project, spec

SAMPLE = schema.dict(
    {
        "id": schema.int.min(1).max(10_000),
        "code": schema.str.regex(r"[a-z]{3}-\d{2}"),
        "tags": schema.list(schema.str.len(1, 6)),
    }
)


def test_same_seed_gives_the_same_value() -> None:
    """Фикстура детерминирована при фиксированном seed."""
    assert build_fixture(SAMPLE, seed=7) == build_fixture(SAMPLE, seed=7)


def test_different_seeds_give_different_values() -> None:
    """Seed действительно управляет генерацией, а не игнорируется."""
    values = {repr(build_fixture(SAMPLE, seed=seed)) for seed in range(6)}

    assert len(values) > 1


def test_generation_does_not_disturb_global_random() -> None:
    """Генерация не трогает глобальный ``random``.

    Иначе одна фикстура сдвигала бы все остальные источники случайности в
    процессе, и тесты начали бы влиять друг на друга через глобальное состояние.
    """
    random.seed(1234)
    expected = [random.random() for _ in range(3)]

    random.seed(1234)
    first = random.random()
    build_fixture(SAMPLE, seed=1)
    rest = [random.random() for _ in range(2)]

    assert [first, *rest] == expected


def test_optional_keys_are_never_generated() -> None:
    """Необязательные ключи не попадают в фикстуру."""
    value = build_fixture(schema.dict({"a": schema.int(1), optional("b"): schema.str("x")}), seed=1)

    assert value == {"a": 1}


def test_adding_an_optional_field_does_not_change_the_fixture() -> None:
    """Расширение контракта необязательным полем не двигает стабильную фикстуру."""
    before = schema.dict({"a": schema.int.min(1).max(9)})
    after = schema.dict({"a": schema.int.min(1).max(9), optional("b"): schema.str.len(1, 4)})

    assert build_fixture(before, seed=11) == build_fixture(after, seed=11)


def test_union_is_projected_to_its_first_variant() -> None:
    """Объединение сворачивается в первый вариант в порядке контракта."""
    union = schema.str("alpha") | schema.str("beta") | schema.str("gamma")

    assert {build_fixture(union, seed=seed) for seed in range(8)} == {"alpha"}


def test_appending_a_union_variant_does_not_switch_the_fixture() -> None:
    """Новый вариант в конце объединения не переключает фикстуру."""
    before = schema.dict({"kind": schema.str("alpha") | schema.str("beta")})
    after = schema.dict({"kind": schema.str("alpha") | schema.str("beta") | schema.str("gamma")})

    assert build_fixture(before, seed=2) == build_fixture(after, seed=2)


def test_project_first_variant_is_stable() -> None:
    """Проекция объединения — чистая функция от схемы."""
    union = schema.int(1) | schema.int(2)

    assert repr(project_first_variant(union)) == repr(project_first_variant(union))


def test_fixture_satisfies_its_own_schema() -> None:
    """Сгенерированное значение обязано проходить свою же d42-схему."""
    validate_or_fail(SAMPLE, build_fixture(SAMPLE, seed=5))


def test_validate_overlay_fixture_rejects_out_of_contract_values() -> None:
    """Значение ручного генератора вне контракта отвергается."""
    generated = schema.dict({"name": schema.str.len(1, 4)})
    overlaid = overlay_generators(generated, {("name",): schema.str("слишком длинное имя")})

    with pytest.raises(ContractOverlayError):
        validate_overlay_fixture(overlaid, {"name": "слишком длинное имя"})


def test_validate_overlay_fixture_accepts_values_inside_the_contract() -> None:
    """Значение внутри контракта проходит."""
    generated = schema.dict({"name": schema.str.len(1, 8)})
    overlaid = overlay_generators(generated, {("name",): schema.str("Отдел")})

    validate_overlay_fixture(overlaid, {"name": "Отдел"})


def test_unique_lists_are_generated_within_the_contract() -> None:
    """Список с ``unique`` заполняется ровно настолько, насколько требует контракт."""
    value = build_fixture(schema.list(schema.int.min(1).max(50)).unique().len(3), seed=1)

    assert len(value) == 3
    assert len(set(value)) == 3


_D42_FRIENDLY_SPEC = """
openapi: "3.0.3"
info: {title: Fixtures, version: "1.0"}
paths:
  /items:
    get:
      operationId: listItems
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {$ref: '#/components/schemas/ItemPage'}
components:
  schemas:
    ItemPage:
      type: object
      required: [items, total]
      properties:
        total: {type: integer, minimum: 0, maximum: 1000}
        items:
          type: array
          items: {$ref: '#/components/schemas/Item'}
    Item:
      type: object
      required: [id, code, kind]
      properties:
        id: {type: integer, minimum: 1}
        code: {type: string, pattern: '^[a-z]{3}-[0-9]{2}$'}
        kind: {type: string, enum: [draft, review, published]}
        note: {type: string, nullable: true, minLength: 1, maxLength: 40}
"""


def _fixtures_project(root: Path) -> object:
    project = make_project(root)
    project.write("api/spec.yaml", _D42_FRIENDLY_SPEC)
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={"api.listItems": {"source": "main", "operation_id": "listItems"}},
    )
    return project


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_generated_fixtures_satisfy_the_generated_json_schema(tmp_path: Path, seed: int) -> None:
    """Главная перекрёстная проверка: d42 и JSON Schema согласны друг с другом.

    Фикстура строится по generated d42-схеме и проверяется **другим**
    представлением того же контракта. Если бы конвертер где-то расходился с
    транслятором JSON Schema, этот тест бы это поймал.
    """
    project = _fixtures_project(tmp_path / "project")
    project.update()

    checked = 0
    with project.importable() as generated_package:
        operations = generated_package.operations
        for key in operations.keys():  # noqa: SIM118 - публичный метод, не dict
            handle = operations.by_key(key)
            for variant in handle.responses:
                if variant.json_schema is None or variant.d42_export is None:
                    continue
                d42_schema = handle.d42_schema(Direction.RESPONSE, export=variant.d42_export)
                value = build_fixture(d42_schema, seed=seed)
                validate_instance(
                    dict(variant.json_schema),
                    value,
                    operation_key=key,
                    direction=Direction.RESPONSE,
                    part=f"фикстура {variant.label()}",
                )
                checked += 1

    assert checked > 0, "не проверено ни одной фикстуры — тест ничего не доказал"


def test_generated_fixture_is_byte_stable_between_runs(tmp_path: Path) -> None:
    """Одна и та же generated-схема даёт одну и ту же фикстуру."""
    project = _fixtures_project(tmp_path / "project")
    project.update()

    with project.importable() as generated_package:
        handle = generated_package.operations.api.list_items
        variant = handle.response(status=200)
        d42_schema = handle.d42_schema(Direction.RESPONSE, export=variant.d42_export)

        assert build_fixture(d42_schema, seed=1) == build_fixture(d42_schema, seed=1)


def test_d42_is_disabled_with_a_reason_when_it_cannot_express_the_contract(
    tmp_path: Path,
) -> None:
    """Невыразимая в d42 конструкция отключает d42 точечно, а не роняет операцию.

    В тестовой спецификации остаётся невыразимое сочетание ``pattern`` и границ
    длины. Ронять из-за этого всю операцию было бы неправильно: ядро не обязано
    зависеть от опциональной интеграции. Причина обязана быть записана в артефакт
    и всплыть при обращении к d42-схеме.
    """
    project = make_project(tmp_path / "project")
    project.write_spec("api/spec.yaml", spec("basic", "openapi30.yaml"))
    project.write_manifest(
        sources={"main": {"path": "api/spec.yaml", "selection": "explicit"}},
        operations={"api.createDocument": {"source": "main", "operation_id": "createDocument"}},
    )
    artifacts = project.update()

    assert dict(artifacts.d42_disabled).get("api.createDocument")
    document = project.contract_document("api__create_document")
    assert document["d42"]["enabled"] is False
    assert "pattern" in document["d42"]["reason"]
    assert any("d42:" in item for item in document["unsupported"])
    assert not (project.output_dir / "_d42").exists()

    with project.importable() as generated_package:
        handle = generated_package.operations.api.create_document
        assert handle.response(status=200).d42_export is None
        with pytest.raises(Exception) as info:
            handle.d42_schema(Direction.RESPONSE, export="GeneratedAnythingSchema")
        assert "pattern" in str(info.value)

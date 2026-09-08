"""Детерминированная генерация фикстур по схемам d42.

Задача модуля — сделать так, чтобы закоммиченная фикстура менялась **только**
когда меняется контракт, и никогда — сама по себе.

Три источника недетерминизма и что с ними сделано
-------------------------------------------------

1. **Глобальный ``random``.** Штатный ``d42.fake`` ходит в модульный
   ``random``: два запуска дают разные данные. Здесь генератор собирается
   вручную — ``Generator(_SeededRandom(seed), RegexGenerator(_SeededRandom(seed)))``
   — поверх приватного ``random.Random(seed)``. Глобальное состояние не трогается,
   поэтому фикстуры не зависят ни от порядка тестов, ни от чужих вызовов ``random``.

2. **Выбор варианта объединения.** ``fake(schema.int(1) | schema.int(2))`` каждый
   раз выбирает вариант случайно, а любая новая ветка ``oneOf`` в спецификации
   сдвигала бы выбор и переписывала бы все фикстуры разом. Поэтому перед генерацией
   схема проецируется: каждый ``AnySchema`` заменяется на свой **первый** вариант.
   Добавление нового варианта **в конец** объединения фикстуру не меняет.
   Полезный побочный эффект: ``X | schema.none`` (nullable) проецируется в ``X``,
   и фикстура содержит осмысленное значение, а не ``null``.

3. **Опциональные ключи.** Отдельная механика не нужна: генератор d42 никогда не
   заполняет ключи, объявленные через ``optional(...)`` (см. ``visit_dict``).
   Значит, новое необязательное поле в контракте стабильную фикстуру не двигает.
   Это свойство закреплено проверкой в self-check'е интеграции.

Проекция — чистое преобразование «схема → схема» на публичных ``props``:
``props.update(...)`` возвращает новый объект, ни одна исходная схема не мутируется.

Отдельный случай: ``uniqueItems``
---------------------------------

Список с ``.unique()`` конфликтует с проекцией «на первый вариант»: из одного
литерала нельзя набрать несколько различных элементов, и генератор d42 честно
падает ``RuntimeError`` после 1000 попыток. Штатная случайная длина 0..16 делает то
же самое даже без проекции — набрать 16 различных значений из ``enum`` на три
элемента невозможно.

Поэтому для списков с ``uniqueItems``:

* длина фиксируется минимально допустимой контрактом (``len``, иначе
  ``max(minItems, 1)``, но не больше ``maxItems``) — так генератор не обязан
  выдумывать больше различных значений, чем требует контракт, и добавление
  ``maxItems`` в спецификацию фикстуру не двигает;
* из объединения-элемента остаются **первые N** вариантов, где N — та самая длина.
  Свойство стабильности сохраняется: добавление варианта **в конец** объединения
  первые N не меняет.

Если различных значений всё равно не хватает (``minItems`` больше размера ``enum``),
``RuntimeError`` d42 переводится в
:class:`~geas.errors.UnsupportedConstructError` с объяснением.

Проверка результата
-------------------

Проекция широкая (это не сужение контракта: она лишь фиксирует выбор ветки), но
доверять ей на слово нельзя. Поэтому сгенерированное значение валидируется по
**исходной, непроецированной** схеме через ``d42.validate_or_fail``; расхождение —
:class:`~geas.errors.ValidationFailedError`.

Если схема получена из :func:`~geas.integrations.d42.overlays.overlay_generators`,
дополнительно проверяется контракт **до** overlay'я: ручной генератор мог выдать
значение, нарушающее ``pattern`` или границы сгенерированной схемы (заранее это
неразрешимо — см. докстринг ``overlays``). Такое расхождение —
:class:`~geas.errors.ContractOverlayError`.

Оговорка про типы вне OpenAPI: ``schema.uuid4`` и ``schema.datetime`` генерируются
средствами d42 (``uuid4()``, ``datetime.utcnow()``) и детерминированными не будут.
Конвертер таких узлов не порождает; если схема пришла извне — детерминизм на ней
не гарантируется.
"""

from __future__ import annotations

import random
from typing import Any

from d42 import ValidationException, validate_or_fail
from d42.declaration import GenericSchema
from d42.declaration.types import AnySchema, DictSchema, ListSchema, Schema
from d42.generation import Generator, Random, RegexGenerator
from d42.utils import is_ellipsis
from niltype import Nil, Nilable

from geas.errors import (
    ContractOverlayError,
    UnsupportedConstructError,
    ValidationFailedError,
)
from geas.integrations.d42.overlays import original_contract

__all__ = ["DEFAULT_SEED", "build_fixture", "project_first_variant", "validate_overlay_fixture"]

#: Сид по умолчанию: любое фиксированное число, лишь бы оно не менялось.
DEFAULT_SEED = 0


class _SeededRandom(Random):
    """``d42.generation.Random`` поверх приватного ``random.Random``.

    Штатная реализация дергает модульный ``random``, то есть делит состояние со
    всем процессом. Здесь состояние своё, поэтому одинаковый ``seed`` всегда даёт
    одинаковую фикстуру — независимо от того, что происходило в процессе до этого.
    """

    def __init__(self, seed: int = DEFAULT_SEED) -> None:
        self._random = random.Random(seed)

    def set_seed(self, seed: Any) -> None:
        self._random.seed(seed)

    def random_int(self, start: int, end: int) -> int:
        return self._random.randint(start, end)

    def random_float(self, start: float, end: float, precision: Nilable[int] = Nil) -> float:
        if start > end:
            raise ValueError("random_float: start must be <= end")
        if precision is Nil:
            return self._random.uniform(start, end)
        scale = 10**precision
        result = self.random_int(int(start * scale), int(end * scale)) / scale
        return float(round(result, precision))

    def random_str(self, length: int, alphabet: str) -> str:
        return "".join(self._random.choice(alphabet) for _ in range(length))

    def random_choice(self, sequence: Any) -> Any:
        return self._random.choice(sequence)

    def shuffle_list(self, elements: list[Any]) -> None:
        self._random.shuffle(elements)


def build_fixture(schema: GenericSchema, *, seed: int = DEFAULT_SEED) -> Any:
    """Сгенерировать значение по схеме детерминированно.

    Одна и та же схема с одним и тем же ``seed`` всегда даёт одно и то же значение.
    Результат валидируется по исходной схеме (и, для overlay'ев, по контракту до
    overlay'я), поэтому невалидная фикстура до мока не доезжает.

    :raises ValidationFailedError: значение не соответствует переданной схеме.
    :raises ContractOverlayError: ручной генератор overlay'я вышел за пределы
        сгенерированного контракта.
    """
    if not isinstance(schema, Schema):
        raise ValidationFailedError(
            f"ожидалась схема d42, получено {type(schema).__name__}",
            expected="d42 GenericSchema",
            actual=type(schema).__name__,
        )

    shared = _SeededRandom(seed)
    generator = Generator(shared, RegexGenerator(shared))
    try:
        value = project_first_variant(schema).__accept__(generator)
    except RuntimeError as error:
        # Единственный RuntimeError генератора d42 — исчерпание различных значений
        # для списка с uniqueItems. Переводим его в ошибку контракта с объяснением.
        raise UnsupportedConstructError(
            "не удалось сгенерировать список с uniqueItems: различных значений в "
            "контракте меньше, чем требует minItems. Ослабьте minItems, расширьте "
            f"тип элемента или оформите waiver. Исходная ошибка d42: {error}"
        ) from error

    try:
        validate_or_fail(schema, value)
    except ValidationException as error:
        raise ValidationFailedError(
            "сгенерированная фикстура не соответствует собственной схеме d42",
            expected=str(schema),
            actual=repr(value),
            validator="d42",
        ) from error

    validate_overlay_fixture(schema, value)
    return value


def validate_overlay_fixture(schema: GenericSchema, value: Any) -> None:
    """Проверить значение по контракту, который был до overlay'ев.

    Для схемы без overlay'ев проверка вырождается в валидацию по ней самой.
    Именно здесь ловится то, что нельзя проверить при сборке overlay'я: ручной
    генератор выдал строку, не подходящую под ``pattern`` контракта, число вне
    границ и т.п.

    :raises ContractOverlayError: значение нарушает сгенерированный контракт.
    """
    contract = original_contract(schema)
    if contract is None:
        return
    try:
        validate_or_fail(contract, value)
    except ValidationException as error:
        raise ContractOverlayError(
            "значение, выданное ручным генератором overlay'я, нарушает "
            "сгенерированный контракт: " + str(error).strip()
        ) from error


def project_first_variant(schema: GenericSchema) -> GenericSchema:
    """Заменить каждое объединение на его первый вариант.

    Преобразование «схема → схема» на публичных ``props``: исходная схема не
    меняется. Нужно ради стабильности фикстур — см. докстринг модуля.
    """
    return _project(schema, keep=1)


def _project(schema: GenericSchema, *, keep: int) -> GenericSchema:
    """Проекция с указанием, сколько первых вариантов объединения оставить."""
    if isinstance(schema, AnySchema):
        types = schema.props.types
        if types is Nil or not types:
            return schema
        selected = tuple(types[:keep]) or (types[0],)
        if len(selected) == 1:
            return _project(selected[0], keep=1)
        return AnySchema()(*(_project(variant, keep=1) for variant in selected))

    if isinstance(schema, DictSchema):
        keys = schema.props.keys
        if keys is Nil:
            return schema
        projected = {
            key: (value if is_ellipsis(value) else _project(value, keep=1), is_optional)
            for key, (value, is_optional) in keys.items()
        }
        return type(schema)(schema.props.update(keys=projected))

    if isinstance(schema, ListSchema):
        return _project_list(schema)

    return schema


def _project_list(schema: ListSchema) -> GenericSchema:
    """Проекция списка; списки с ``unique()`` требуют отдельного обращения."""
    props = schema.props
    updates: dict[str, Any] = {}
    # Уникальность и «первый вариант» конфликтуют: из одного литерала нельзя
    # набрать несколько различных элементов. Поэтому для unique-списков длина
    # фиксируется минимально допустимой контрактом, а из объединения остаётся
    # ровно столько первых вариантов, сколько элементов предстоит сгенерировать.
    # Добавление варианта в конец объединения фикстуру по-прежнему не меняет.
    length = _unique_length(props) if props.unique else None
    if length is not None:
        updates["len"] = length
    if props.type is not Nil:
        updates["type"] = _project(props.type, keep=max(length or 1, 1))
    if props.elements is not Nil:
        updates["elements"] = [
            element if is_ellipsis(element) else _project(element, keep=1)
            for element in props.elements
        ]
    return schema if not updates else type(schema)(props.update(**updates))


def _unique_length(props: Any) -> int:
    """Длина, которую безопасно сгенерировать для списка с ``uniqueItems``.

    Берётся минимум, требуемый контрактом (но хотя бы один элемент, если это
    разрешено): случайная длина из d42 упирается в исчерпание различных значений
    тем чаще, чем она больше.
    """
    if props.len is not Nil:
        return int(props.len)
    length = max(int(props.min_len) if props.min_len is not Nil else 0, 1)
    if props.max_len is not Nil:
        length = min(length, int(props.max_len))
    return max(length, 0)

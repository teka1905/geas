"""IR → живые объекты схем d42 2.x.

Модуль переводит нейтральное IR (:mod:`openapi_contracts.models`) в объекты
``d42.declaration`` — те самые ``schema.dict(...)`` / ``schema.list(...)``, которые
потребитель кладёт в моки и подставляет через ``%``.

Что важно знать про точность перевода
-------------------------------------

d42 — язык генерации, а не язык ограничений: часть конструкций OpenAPI в нём
невыразима. Библиотека валидирует каждый payload **двумя** путями — d42 и JSON
Schema, — поэтому каждое расхождение классифицируется явно:

* **сужение** (d42 строже контракта) — запрещено всегда: тест начал бы падать на
  валидном ответе бэкенда;
* **расширение** (d42 мягче контракта) — допустимо, только если оно
  задокументировано *и* соответствующее ограничение полностью покрыто JSON Schema,
  а генерируемая фикстура при этом остаётся валидной;
* всё остальное — :class:`~openapi_contracts.errors.UnsupportedConstructError`
  с точным keyword'ом, JSON Pointer'ом и contract path (fail closed).

Принятые решения
----------------

``oneOf`` → ``schema.any(...)`` — **расширение**, принято осознанно. У d42 нет
эксклюзивного объединения: ``schema.any`` — это ``anyOf``. Значение, подходящее
сразу под два варианта ``oneOf``, d42 пропустит, а JSON Schema отвергнет —
эксклюзивность целиком держится на JSON-Schema-пути. Сама фикстура при этом
остаётся корректной: она генерируется по одному конкретному варианту.
``discriminator`` в d42 не выражается вовсе и тоже остаётся на JSON Schema.

``uniqueItems`` → ``.unique()`` — **точно**, без расширения. В d42 2.x у
``ListSchema`` есть ``unique()``, который и валидатор, и генератор honor'ят
(``UniqueValidationError`` при дубликатах; генератор перебирает элементы, пока не
получит уникальные). Отказываться от него было бы неправильно: без ``.unique()``
сгенерированная фикстура могла бы содержать дубликаты и падала бы на собственной
JSON-Schema-проверке. Единственный побочный эффект — генератор шумно падает
``RuntimeError``, если уникальных значений физически не хватает
(``schema.list(schema.bool).len(5).unique()``); это громкий отказ, а не тихая порча.

``format`` (``date-time``, ``email``, ``uuid``, ...) в d42 не переносится: d42 его
не проверяет, а подделывать проверку строкой-заглушкой значило бы соврать про
контракт. ``format`` остаётся на JSON Schema.

Fail closed (список полный)
---------------------------

* ``allOf`` — у d42 нет пересечения типов;
* ``exclusiveMinimum`` / ``exclusiveMaximum`` у ``number`` — вещественную границу
  «строго больше» нельзя выразить через ``min``/``max`` без потери точности
  (у ``integer`` то же самое разворачивается точно: ``N+1`` / ``N-1``);
* ``multipleOf`` — у d42 нет такого ограничения, а сгенерированное значение
  почти наверняка нарушило бы его и упало бы на JSON-Schema-проверке фикстуры;
* ``pattern`` вместе с ``minLength``/``maxLength`` — в d42 ``regex()`` и ``len()``
  взаимоисключающи (``DeclarationError: already declared``);
* ``minProperties`` / ``maxProperties`` — у ``DictSchema`` таких свойств нет;
* типизированный ``additionalProperties`` — ``schema.dict`` умеет только «открыт /
  закрыт», без схемы для лишних ключей;
* рекурсивные ``$ref`` — d42-схема строится «по значению», рекурсия развернулась бы
  бесконечно (:class:`~openapi_contracts.errors.RecursiveSchemaError`);
* ``enum``, чьи литералы противоречат соседним ограничениям (``pattern``, границам,
  ``multipleOf``): противоречие разрешимо на этапе сборки, поэтому проверяется сразу.

Дополнительно: план (внутреннее дерево вызовов d42) — общий с
:mod:`openapi_contracts.integrations.d42.renderer`. Благодаря этому
сгенерированный исходник и схема, собранная в памяти, не могут разъехаться:
у них один источник решений.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from d42 import optional, schema
from d42.declaration import GenericSchema

from openapi_contracts.errors import (
    RecursiveSchemaError,
    RefResolutionError,
    UnsupportedConstructError,
    ValidationFailedError,
)
from openapi_contracts.models import (
    AdditionalProperties,
    AllOfNode,
    AnyNode,
    ArrayNode,
    BooleanNode,
    Direction,
    IntegerNode,
    NullNode,
    NumberNode,
    ObjectNode,
    RefNode,
    SchemaNode,
    StringNode,
    UnionNode,
)
from openapi_contracts.paths import ARRAY_ITEMS, ContractPath, format_contract_path, variant_segment

__all__ = ["to_d42"]


class _NoLiteral:
    """Маркер «литеральное значение не задано» для :class:`_Leaf`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<no-literal>"


#: Единственный экземпляр маркера (сравнивается по типу, не по значению).
_NO_LITERAL = _NoLiteral()

#: Один шаг цепочки вызовов: ``("len", (1, 5))`` → ``.len(1, 5)``.
_Call = tuple[str, tuple[Any, ...]]


@dataclass(frozen=True, slots=True)
class _Ref:
    """Ссылка на именованное определение бандла."""

    name: str


@dataclass(frozen=True, slots=True)
class _Leaf:
    """Скалярная схема: ``schema.<base>[(literal)][.call(...)...]``."""

    base: str
    literal: Any = _NO_LITERAL
    calls: tuple[_Call, ...] = ()


@dataclass(frozen=True, slots=True)
class _DictPlan:
    """``schema.dict({...})``; ``entries`` — ``(имя, план, optional)``."""

    entries: tuple[tuple[str, _Plan, bool], ...]
    open: bool


@dataclass(frozen=True, slots=True)
class _ListPlan:
    """``schema.list(items)`` с цепочкой ``.len(...)`` / ``.unique()``."""

    items: _Plan
    calls: tuple[_Call, ...] = ()


@dataclass(frozen=True, slots=True)
class _UnionPlan:
    """``schema.any(...)`` — объединение вариантов."""

    variants: tuple[_Plan, ...]


#: План — дерево вызовов d42, из которого собирается и объект, и исходный текст.
_Plan = _Ref | _Leaf | _DictPlan | _ListPlan | _UnionPlan

#: План значения ``null``: выносится в константу, чтобы не плодить объекты.
_NONE_PLAN = _Leaf(base="none")


def to_d42(node: SchemaNode, definitions: Mapping[str, SchemaNode]) -> GenericSchema:
    """Собрать живую d42-схему для узла IR.

    ``definitions`` — именованные определения бандла операции; ``RefNode``
    разрешается через них. Рекурсия отклоняется
    :class:`~openapi_contracts.errors.RecursiveSchemaError`, отсутствующее
    определение — :class:`~openapi_contracts.errors.RefResolutionError`.
    """
    root_plan, plans = plan_bundle(node, definitions)
    built: dict[str, GenericSchema] = {}
    for name in topological_order(plans):
        built[name] = build(plans[name], built)
    return build(root_plan, built)


# --------------------------------------------------------------------------------------
# Планирование
# --------------------------------------------------------------------------------------


def plan_node(
    node: SchemaNode,
    definitions: Mapping[str, SchemaNode],
    *,
    path: ContractPath = (),
    definition: str | None = None,
) -> _Plan:
    """Построить план для узла IR, не разворачивая ``$ref``."""
    return _nullable(_plan_inner(node, definitions, path, definition), node)


def plan_bundle(
    node: SchemaNode, definitions: Mapping[str, SchemaNode]
) -> tuple[_Plan, dict[str, _Plan]]:
    """План корня плюс планы всех определений, достижимых из него."""
    root_plan = plan_node(node, definitions)
    plans: dict[str, _Plan] = {}
    pending = sorted(referenced_names(root_plan))
    while pending:
        name = pending.pop()
        if name in plans:
            continue
        plans[name] = plan_node(definitions[name], definitions, definition=name)
        pending.extend(sorted(referenced_names(plans[name])))
    return root_plan, plans


def referenced_names(plan: _Plan) -> set[str]:
    """Имена определений, на которые ссылается план (без транзитивности)."""
    if isinstance(plan, _Ref):
        return {plan.name}
    if isinstance(plan, _DictPlan):
        names: set[str] = set()
        for _, sub, _ in plan.entries:
            names |= referenced_names(sub)
        return names
    if isinstance(plan, _ListPlan):
        return referenced_names(plan.items)
    if isinstance(plan, _UnionPlan):
        names = set()
        for variant in plan.variants:
            names |= referenced_names(variant)
        return names
    return set()


def topological_order(plans: Mapping[str, _Plan]) -> tuple[str, ...]:
    """Определения в порядке «сначала зависимости».

    Независимые определения идут по алфавиту — порядок артефакта детерминирован.
    Цикл отклоняется :class:`~openapi_contracts.errors.RecursiveSchemaError`
    с перечислением участников.
    """
    order: list[str] = []
    done: set[str] = set()
    stack: list[str] = []

    def visit(name: str) -> None:
        if name in done:
            return
        if name in stack:
            cycle = " -> ".join([*stack[stack.index(name) :], name])
            raise RecursiveSchemaError(
                f"схема рекурсивна и не выражается в d42: {cycle}. "
                f"Разорвите цикл в спецификации или оформите waiver на этот узел"
            )
        stack.append(name)
        for dependency in sorted(referenced_names(plans[name])):
            visit(dependency)
        stack.pop()
        done.add(name)
        order.append(name)

    for name in sorted(plans):
        visit(name)
    return tuple(order)


def _plan_inner(
    node: SchemaNode,
    definitions: Mapping[str, SchemaNode],
    path: ContractPath,
    definition: str | None,
) -> _Plan:
    if isinstance(node, RefNode):
        if node.name not in definitions:
            raise _error(
                RefResolutionError,
                f"определение {node.name!r} отсутствует в бандле операции",
                node,
                path,
                definition,
            )
        return _Ref(name=node.name)
    if isinstance(node, AnyNode):
        # Узел появляется только по явному waiver: «что угодно» здесь — решение,
        # записанное в waiver, а не молчаливая деградация.
        return _Leaf(base="any")
    if isinstance(node, NullNode):
        return _NONE_PLAN
    if isinstance(node, BooleanNode):
        return _plan_boolean(node, path, definition)
    if isinstance(node, StringNode):
        return _plan_string(node, path, definition)
    if isinstance(node, IntegerNode):
        return _plan_integer(node, path, definition)
    if isinstance(node, NumberNode):
        return _plan_number(node, path, definition)
    if isinstance(node, ArrayNode):
        return _plan_array(node, definitions, path, definition)
    if isinstance(node, ObjectNode):
        return _plan_object(node, definitions, path, definition)
    if isinstance(node, UnionNode):
        return _plan_union(node, definitions, path, definition)
    if isinstance(node, AllOfNode):
        raise _error(
            UnsupportedConstructError,
            "allOf не выражается в d42: у языка нет пересечения типов. "
            "Слейте части в один объект в спецификации либо оформите waiver",
            node,
            path,
            definition,
        )
    raise _error(
        UnsupportedConstructError,
        f"неизвестный узел IR: {type(node).__name__}",
        node,
        path,
        definition,
    )


def _plan_boolean(node: BooleanNode, path: ContractPath, definition: str | None) -> _Plan:
    if node.enum is None:
        return _Leaf(base="bool")
    _require_enum(node, node.enum, path, definition)
    return _literals("bool", node.enum)


def _plan_string(node: StringNode, path: ContractPath, definition: str | None) -> _Plan:
    if node.enum is not None:
        _require_enum(node, node.enum, path, definition)
        for value in node.enum:
            _check_string_value(node, value, path, definition)
        return _literals("str", node.enum)

    if node.pattern is not None:
        if node.min_length is not None or node.max_length is not None:
            raise _error(
                UnsupportedConstructError,
                "pattern вместе с minLength/maxLength не выражается в d42: "
                "regex() и len() взаимоисключающи. Уберите одно из ограничений "
                "в спецификации либо оформите waiver",
                node,
                path,
                definition,
            )
        return _Leaf(base="str", calls=(("regex", (node.pattern,)),))

    calls = _length_calls(node.min_length, node.max_length)
    if node.min_length is not None and node.max_length is not None:
        _require_range(node, node.min_length, node.max_length, "minLength", path, definition)
    return _Leaf(base="str", calls=calls)


def _plan_integer(node: IntegerNode, path: ContractPath, definition: str | None) -> _Plan:
    minimum = _tighter(node.minimum, _shift(node.exclusive_minimum, +1), max)
    maximum = _tighter(node.maximum, _shift(node.exclusive_maximum, -1), min)

    if node.enum is not None:
        _require_enum(node, node.enum, path, definition)
        for value in node.enum:
            _check_number_value(node, value, minimum, maximum, path, definition)
        return _literals("int", node.enum)

    if node.multiple_of is not None:
        raise _error(
            UnsupportedConstructError,
            "multipleOf не выражается в d42: сгенерированное значение нарушило бы "
            "ограничение и упало бы на JSON-Schema-проверке фикстуры",
            node,
            path,
            definition,
        )
    if minimum is not None and maximum is not None:
        _require_range(node, minimum, maximum, "minimum", path, definition)
    return _Leaf(base="int", calls=_bound_calls(minimum, maximum))


def _plan_number(node: NumberNode, path: ContractPath, definition: str | None) -> _Plan:
    for keyword, value in (
        ("exclusiveMinimum", node.exclusive_minimum),
        ("exclusiveMaximum", node.exclusive_maximum),
    ):
        if value is not None:
            raise _error(
                UnsupportedConstructError,
                f"{keyword} у number не выражается в d42: границы там только "
                f"включающие, а вещественное «строго больше» точно не переводится "
                f"(у integer то же самое разворачивается в N+1 / N-1)",
                node,
                path,
                definition,
            )

    minimum = None if node.minimum is None else float(node.minimum)
    maximum = None if node.maximum is None else float(node.maximum)

    if node.enum is not None:
        _require_enum(node, node.enum, path, definition)
        for value in node.enum:
            _check_number_value(node, float(value), minimum, maximum, path, definition)
        return _literals("float", tuple(float(value) for value in node.enum))

    if node.multiple_of is not None:
        raise _error(
            UnsupportedConstructError,
            "multipleOf не выражается в d42: сгенерированное значение нарушило бы "
            "ограничение и упало бы на JSON-Schema-проверке фикстуры",
            node,
            path,
            definition,
        )
    if minimum is not None and maximum is not None:
        _require_range(node, minimum, maximum, "minimum", path, definition)
    return _Leaf(base="float", calls=_bound_calls(minimum, maximum))


def _plan_array(
    node: ArrayNode,
    definitions: Mapping[str, SchemaNode],
    path: ContractPath,
    definition: str | None,
) -> _Plan:
    if node.min_items is not None and node.max_items is not None:
        _require_range(node, node.min_items, node.max_items, "minItems", path, definition)
    items = plan_node(node.items, definitions, path=(*path, ARRAY_ITEMS), definition=definition)
    calls = _length_calls(node.min_items, node.max_items)
    if node.unique_items:
        # uniqueItems выражается точно: и валидатор, и генератор d42 его honor'ят.
        calls = (*calls, ("unique", ()))
    return _ListPlan(items=items, calls=calls)


def _plan_object(
    node: ObjectNode,
    definitions: Mapping[str, SchemaNode],
    path: ContractPath,
    definition: str | None,
) -> _Plan:
    for keyword, value in (
        ("minProperties", node.min_properties),
        ("maxProperties", node.max_properties),
    ):
        if value is not None:
            raise _error(
                UnsupportedConstructError,
                f"{keyword} не выражается в d42: у schema.dict нет ограничения на "
                f"количество ключей",
                node,
                path,
                definition,
            )
    if isinstance(node.additional_properties, SchemaNode):
        raise _error(
            UnsupportedConstructError,
            "типизированный additionalProperties не выражается в d42: schema.dict "
            "умеет только «открыт»/«закрыт», без схемы для лишних ключей",
            node,
            path,
            definition,
        )

    seen: set[str] = set()
    entries: list[tuple[str, _Plan, bool]] = []
    for prop in sorted(node.properties, key=lambda item: item.name):
        if prop.name in seen:
            raise _error(
                UnsupportedConstructError,
                f"свойство {prop.name!r} объявлено в объекте дважды",
                node,
                path,
                definition,
            )
        seen.add(prop.name)
        entries.append(
            (
                prop.name,
                plan_node(prop.schema, definitions, path=(*path, prop.name), definition=definition),
                not prop.required,
            )
        )
    return _DictPlan(
        entries=tuple(entries),
        open=node.additional_properties is AdditionalProperties.ALLOWED,
    )


def _plan_union(
    node: UnionNode,
    definitions: Mapping[str, SchemaNode],
    path: ContractPath,
    definition: str | None,
) -> _Plan:
    if not node.variants:
        raise _error(
            UnsupportedConstructError,
            f"{node.kind.value} без вариантов не выражается в d42",
            node,
            path,
            definition,
        )
    # oneOf и anyOf схлопываются в schema.any: эксклюзивность oneOf и discriminator
    # остаются на JSON-Schema-пути (см. докстринг модуля).
    variants = tuple(
        plan_node(variant, definitions, path=(*path, variant_segment(index)), definition=definition)
        for index, variant in enumerate(node.variants)
    )
    return _UnionPlan(variants=variants)


def _nullable(plan: _Plan, node: SchemaNode) -> _Plan:
    """Добавить вариант ``null``, если узел помечен ``nullable``."""
    if not node.nullable:
        return plan
    if isinstance(node, (NullNode, AnyNode)):
        # schema.none и «что угодно» уже допускают null.
        return plan
    if isinstance(plan, _UnionPlan):
        if _NONE_PLAN in plan.variants:
            return plan
        return _UnionPlan(variants=(*plan.variants, _NONE_PLAN))
    return _UnionPlan(variants=(plan, _NONE_PLAN))


def _literals(base: str, values: tuple[Any, ...]) -> _Plan:
    """Литерал или объединение литералов в порядке контракта."""
    leaves = tuple(_Leaf(base=base, literal=value) for value in values)
    return leaves[0] if len(leaves) == 1 else _UnionPlan(variants=leaves)


def _length_calls(minimum: int | None, maximum: int | None) -> tuple[_Call, ...]:
    """``.len(...)`` для строк и списков; односторонняя граница — через ``...``."""
    if minimum is None and maximum is None:
        return ()
    if minimum is not None and maximum is not None:
        return (("len", (minimum, maximum)),)
    if minimum is not None:
        return (("len", (minimum, Ellipsis)),)
    return (("len", (Ellipsis, maximum)),)


def _bound_calls(minimum: Any, maximum: Any) -> tuple[_Call, ...]:
    """``.min(...)`` / ``.max(...)`` для чисел."""
    calls: list[_Call] = []
    if minimum is not None:
        calls.append(("min", (minimum,)))
    if maximum is not None:
        calls.append(("max", (maximum,)))
    return tuple(calls)


def _shift(value: int | None, delta: int) -> int | None:
    """Точный перевод исключающей целочисленной границы во включающую."""
    return None if value is None else value + delta


def _tighter(first: Any, second: Any, chooser: Any) -> Any:
    """Выбрать более строгую из двух границ (любая может отсутствовать)."""
    if first is None:
        return second
    if second is None:
        return first
    return chooser(first, second)


def _require_enum(
    node: SchemaNode, values: tuple[Any, ...], path: ContractPath, definition: str | None
) -> None:
    if not values:
        raise _error(
            UnsupportedConstructError,
            "пустой enum не выражается в d42: такому контракту не соответствует ни одно значение",
            node,
            path,
            definition,
        )


def _require_range(
    node: SchemaNode,
    minimum: Any,
    maximum: Any,
    keyword: str,
    path: ContractPath,
    definition: str | None,
) -> None:
    if minimum > maximum:
        raise _error(
            UnsupportedConstructError,
            f"{keyword}={minimum!r} больше верхней границы {maximum!r}: контракт пуст",
            node,
            path,
            definition,
        )


def _check_string_value(
    node: StringNode, value: str, path: ContractPath, definition: str | None
) -> None:
    """Литерал enum обязан удовлетворять соседним ограничениям строки."""
    if node.pattern is not None and re.search(node.pattern, value) is None:
        raise _error(
            UnsupportedConstructError,
            f"значение enum {value!r} не соответствует pattern {node.pattern!r}: "
            f"контракт противоречив",
            node,
            path,
            definition,
        )
    if node.min_length is not None and len(value) < node.min_length:
        raise _error(
            UnsupportedConstructError,
            f"значение enum {value!r} короче minLength={node.min_length}",
            node,
            path,
            definition,
        )
    if node.max_length is not None and len(value) > node.max_length:
        raise _error(
            UnsupportedConstructError,
            f"значение enum {value!r} длиннее maxLength={node.max_length}",
            node,
            path,
            definition,
        )


def _check_number_value(
    node: IntegerNode | NumberNode,
    value: Any,
    minimum: Any,
    maximum: Any,
    path: ContractPath,
    definition: str | None,
) -> None:
    """Литерал enum обязан попадать в границы и делиться на ``multipleOf``."""
    if minimum is not None and value < minimum:
        raise _error(
            UnsupportedConstructError,
            f"значение enum {value!r} меньше нижней границы {minimum!r}",
            node,
            path,
            definition,
        )
    if maximum is not None and value > maximum:
        raise _error(
            UnsupportedConstructError,
            f"значение enum {value!r} больше верхней границы {maximum!r}",
            node,
            path,
            definition,
        )
    # Делимость проверяется только для целых: для float остаток считается неточно,
    # и проверка давала бы ложные срабатывания на корректном контракте.
    if (
        isinstance(node, IntegerNode)
        and node.multiple_of is not None
        and value % node.multiple_of != 0
    ):
        raise _error(
            UnsupportedConstructError,
            f"значение enum {value!r} не делится на multipleOf={node.multiple_of!r}",
            node,
            path,
            definition,
        )


def _error(
    error_type: type[Exception],
    message: str,
    node: SchemaNode,
    path: ContractPath,
    definition: str | None,
) -> Exception:
    """Собрать ошибку с contract path и координатами узла."""
    where = format_contract_path(path)
    if definition is not None:
        where = f"{definition}:{where}"
    return error_type(  # type: ignore[call-arg]
        f"{message} [contract path {where}]",
        source=node.origin.source or None,
        json_pointer=node.origin.pointer or None,
    )


# --------------------------------------------------------------------------------------
# Сборка объектов d42
# --------------------------------------------------------------------------------------


def build(plan: _Plan, built: Mapping[str, GenericSchema]) -> GenericSchema:
    """Собрать объект d42 по плану; ``built`` — уже собранные определения."""
    if isinstance(plan, _Ref):
        return built[plan.name]
    if isinstance(plan, _Leaf):
        result: Any = getattr(schema, plan.base)
        if not isinstance(plan.literal, _NoLiteral):
            result = result(plan.literal)
        for name, args in plan.calls:
            result = getattr(result, name)(*args)
        return result  # type: ignore[no-any-return]
    if isinstance(plan, _DictPlan):
        keys: dict[Any, Any] = {}
        for name, sub, is_optional in plan.entries:
            keys[optional(name) if is_optional else name] = build(sub, built)
        if plan.open:
            keys[...] = ...
        return schema.dict(keys)
    if isinstance(plan, _ListPlan):
        listed: Any = schema.list(build(plan.items, built))
        for name, args in plan.calls:
            listed = getattr(listed, name)(*args)
        return listed  # type: ignore[no-any-return]
    return schema.any(*(build(variant, built) for variant in plan.variants))


def validate_with_d42(
    d42_schema: GenericSchema,
    value: Any,
    *,
    operation_key: str,
    direction: Direction,
) -> None:
    """Проверить значение по generated d42-схеме.

    Это **второй, независимый** путь валидации рядом с JSON Schema. Он ловит то,
    что видит генератор фикстур, и его ошибки формулируются в терминах d42.
    Точную семантику ``oneOf``, ``discriminator`` и ``format`` держит JSON Schema:
    d42-проекция объединений шире исходного контракта (см. шапку модуля).
    """
    from d42 import ValidationException, validate_or_fail

    try:
        validate_or_fail(d42_schema, value)
    except ValidationException as error:
        raise ValidationFailedError(
            f"значение не соответствует generated d42-схеме: {error}",
            operation_key=operation_key,
            direction=direction.value,
            validator="d42",
            actual=repr(value)[:200],
        ) from error

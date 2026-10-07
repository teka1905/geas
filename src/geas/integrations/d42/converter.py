"""IR → живые объекты схем d42 2.x.

Модуль переводит нейтральное IR (:mod:`geas.models`) в объекты
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
* всё остальное — :class:`~geas.errors.UnsupportedConstructError`
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

``number`` → ``schema.any(schema.float, json_int)`` — **точно**. В JSON Schema
``number`` включает целые, а d42 ``schema.float`` целое не принимает, ``schema.int``
— дробное; единого числового типа в d42 нет. Целочисленная ветка —
:data:`~geas.integrations.d42.json_int.json_int`: ``schema.int`` без ``bool``
(``True`` в Python — тоже ``int``, а для JSON Schema это не число). Границы
действуют на обе ветки, у целой — округлённые внутрь (``minimum: 0.5`` → ``.min(1)``);
если целых в диапазоне нет, остаётся одна ``schema.float``. Литерал ``enum`` со
значением ``1`` или ``1.0`` даёт оба литерала: ``float(1.0)`` и ``json_int(1)``. Первой
всегда идёт ``float``: фикстура проецируется на первый вариант и остаётся дробной.

``format`` (``date-time``, ``email``, ``uuid``, ...) в d42 не переносится: d42 его
не проверяет, а подделывать проверку строкой-заглушкой значило бы соврать про
контракт. ``format`` остаётся на JSON Schema.

``required`` на ключ без схемы в ``properties`` (локальная часть наследования
``{required: [argList], allOf: [...]}``) → обязательный ключ со значением
``schema.any``, а при типизированном ``additionalProperties`` — с его схемой. Это
**точно**: сам объект значение такого ключа больше ничем не ограничивает, а схему
даёт пересечение с другой частью ``allOf``.

``allOf`` → распределение по объединению — **точно**. У d42 нет пересечения
типов, поэтому пересечение считается на уровне IR
(:mod:`~geas.integrations.d42.intersection`) и распределяется по вариантам::

    allOf(base, oneOf(V1, ..., Vn))  →  schema.any(base ∧ V1, ..., base ∧ Vn)

Значение проходит ``base`` и хотя бы один вариант тогда и только тогда, когда оно
проходит хотя бы одну ветку, — ничего не теряется и ничего не добавляется.
Эксклюзивность ``oneOf`` и ``discriminator`` остаются на JSON-Schema-пути, как у
любого ``oneOf`` выше. Ветка, равная своему варианту, остаётся ссылкой на его
generated-схему; ветка, которая не допускает ни одного значения (``enum`` варианта
вне ``enum`` базы), выбрасывается — это тоже точно, а ``geas update`` называет её
как ошибку спецификации. Распределение идёт до отсечки цикла: ссылки внутри веток
— обычные ``$ref``, и рекурсия через вариант отсекается общим механизмом. JSON
Schema контракта ``allOf`` сохраняет как есть.

Fail closed (список полный)
---------------------------

* ``allOf``, пересечение которого не доказуемо (два разных ``pattern``,
  ``multipleOf`` у ``number``, ``integer`` против ``number``, ключ, который в
  открытом объекте обязан отсутствовать, ``allOf``, ссылающийся сам на себя) или
  пусто целиком — сообщение называет ветку и contract path свойства;
* ``exclusiveMinimum`` / ``exclusiveMaximum`` у ``number`` — вещественную границу
  «строго больше» нельзя выразить через ``min``/``max`` без потери точности
  (у ``integer`` то же самое разворачивается точно: ``N+1`` / ``N-1``);
* ``multipleOf`` — у d42 нет такого ограничения, а сгенерированное значение
  почти наверняка нарушило бы его и упало бы на JSON-Schema-проверке фикстуры;
* ``pattern`` вместе с ``minLength``/``maxLength`` — в d42 ``regex()`` и ``len()``
  взаимоисключающи (``DeclarationError: already declared``);
* ``minProperties`` / ``maxProperties`` — у ``DictSchema`` таких свойств нет;
* типизированный ``additionalProperties`` выражается через
  :class:`~geas.integrations.d42.typed_dict.TypedDictSchema`: обычный
  d42 ``schema.dict`` не умеет проверять значения динамических ключей;
* ``pattern``, который регулярные выражения Python не разбирают (ECMA/Java
  ``\\p{Lu}``): d42 его не проверит и не сгенерирует. Из спецификации такой паттерн
  сюда не доходит — его отклоняет нормализация;
* ``enum``, чьи литералы противоречат соседним ограничениям (``pattern``, границам,
  ``multipleOf``): противоречие разрешимо на этапе сборки, поэтому проверяется сразу.

Рекурсия
--------

d42-схема строится «по значению», и рекурсивный ``$ref`` развернулся бы бесконечно.
Поэтому цикл отсекается (:func:`cut_cycles`): граф определений обходится в глубину
от корней, и ссылка, которая возвращает обход в определение с текущего пути,
становится листом :class:`~geas.integrations.d42.recursion.RecursiveRefSchema`.
Лист проверяет значение по JSON Schema определения, ``%`` и ``fake()`` делегирует
типизированной схеме того же определения. Это не расширение контракта: всё, что
d42 не развернул, проверяет JSON Schema. Порядок обхода детерминирован: корни — в
переданном порядке, дальше свойства по имени и варианты объединения по порядку,
поэтому место отсечки зависит только от контракта и набора корней.

Дополнительно: план (внутреннее дерево вызовов d42) — общий с
:mod:`geas.integrations.d42.renderer`. Благодаря этому
сгенерированный исходник и схема, собранная в памяти, не могут разъехаться:
у них один источник решений.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from d42 import optional, schema
from d42.declaration import GenericSchema

from geas.errors import (
    RecursiveSchemaError,
    RefResolutionError,
    UnsupportedConstructError,
    ValidationFailedError,
)
from geas.jsonschema_gen import definitions_json_schema
from geas.models import (
    INTEGER_FORMAT_BOUNDS,
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
from geas.paths import (
    ADDITIONAL_PROPERTIES,
    ARRAY_ITEMS,
    ContractPath,
    format_contract_path,
    variant_segment,
)

from .intersection import (
    Empty,
    IntersectionError,
    MissingDefinitionError,
    distribute,
)
from .json_int import json_int
from .recursion import RecursionContract
from .typed_dict import typed_dict

__all__ = ["DroppedBranchNote", "cut_cycles", "to_d42"]


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
class _Cut:
    """Ссылка, замыкающая цикл: лист, который проверяется по JSON Schema определения."""

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
    additional: _Plan | None = None


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
_Plan = _Ref | _Cut | _Leaf | _DictPlan | _ListPlan | _UnionPlan

#: План значения ``null``: выносится в константу, чтобы не плодить объекты.
_NONE_PLAN = _Leaf(base="none")

#: План «любое значение»: ключ, который ``required`` называет без схемы.
_ANY_PLAN = _Leaf(base="any")

#: База листа целочисленной ветки ``number``: не атрибут ``schema``, а
#: :data:`~geas.integrations.d42.json_int.json_int`.
_JSON_INT = "json_int"


@dataclass(frozen=True, slots=True)
class DroppedBranchNote:
    """Ветка ``allOf``, выброшенная из d42 как пустая: где, какая и почему.

    ``where`` — contract path ``allOf`` (с именем определения), ``branch`` —
    вариант объединения, ``reason`` — почему пересечение с остальными частями пусто.
    """

    where: str
    branch: str
    reason: str


#: Сборщик выброшенных веток; ``None`` — не собирать.
_Dropped = list[DroppedBranchNote] | None


def to_d42(node: SchemaNode, definitions: Mapping[str, SchemaNode]) -> GenericSchema:
    """Собрать живую d42-схему для узла IR.

    ``definitions`` — именованные определения бандла операции; ``RefNode``
    разрешается через них, отсутствующее определение —
    :class:`~geas.errors.RefResolutionError`. Цикл отсекается от ``node`` как от
    единственного корня (см. раздел «Рекурсия» в докстринге модуля).
    """
    root_plan, plans = plan_bundle(node, definitions)
    contract = recursion_contract(plans, definitions)
    built: dict[str, GenericSchema] = {}
    for name in topological_order(plans):
        built[name] = build(plans[name], built, contract)
    if contract is not None:
        contract.bind({name: built[name] for name in sorted(cut_targets(plans.values()))})
    return build(root_plan, built, contract)


# --------------------------------------------------------------------------------------
# Планирование
# --------------------------------------------------------------------------------------


def plan_node(
    node: SchemaNode,
    definitions: Mapping[str, SchemaNode],
    *,
    path: ContractPath = (),
    definition: str | None = None,
    dropped: _Dropped = None,
) -> _Plan:
    """Построить план для узла IR, не разворачивая ``$ref``.

    ``dropped``, если передан, получает ветки ``allOf``, выброшенные как пустые
    (см. раздел «allOf» в докстринге модуля): план от него не зависит.
    """
    return _nullable(_plan_inner(node, definitions, path, definition, dropped), node)


def plan_bundle(
    node: SchemaNode, definitions: Mapping[str, SchemaNode]
) -> tuple[_Plan, dict[str, _Plan]]:
    """План корня плюс планы всех определений, достижимых из него, с отсечёнными циклами."""
    root_plan = plan_node(node, definitions)
    plans: dict[str, _Plan] = {}
    pending = sorted(referenced_names(root_plan))
    while pending:
        name = pending.pop()
        if name in plans:
            continue
        plans[name] = plan_node(definitions[name], definitions, definition=name)
        pending.extend(sorted(referenced_names(plans[name])))
    cut, _ = cut_cycles([root_plan], plans)
    return root_plan, cut


def cut_cycles(
    roots: Sequence[_Plan], plans: Mapping[str, _Plan]
) -> tuple[dict[str, _Plan], tuple[tuple[str, str], ...]]:
    """Разорвать циклы в графе определений.

    Граф обходится в глубину: сначала от корней в переданном порядке, затем от
    оставшихся определений по алфавиту. Ссылка ``A → B``, где ``B`` уже лежит на
    текущем пути обхода, замыкает цикл: в плане ``A`` все ссылки на ``B``
    заменяются листом :class:`_Cut`. Остальные ссылки не трогаются, поэтому
    определение типизировано вплоть до места, где цикл замкнулся.

    Возвращает новые планы и отсечённые рёбра ``(A, B)`` в стабильном порядке.
    """
    on_path: set[str] = set()
    done: set[str] = set()
    back: set[tuple[str, str]] = set()

    def visit(start: str) -> None:
        # Итеративный обход: в реальных спецификациях сотни определений, и рекурсия
        # Python упёрлась бы в лимит глубины раньше, чем закончился бы граф.
        on_path.add(start)
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(ordered_references(plans[start])))]
        while stack:
            owner, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                on_path.discard(owner)
                done.add(owner)
                continue
            if child not in plans:
                continue
            if child in on_path:
                back.add((owner, child))
            elif child not in done:
                on_path.add(child)
                stack.append((child, iter(ordered_references(plans[child]))))

    for root in roots:
        for name in ordered_references(root):
            if name in plans and name not in done:
                visit(name)
    for name in sorted(plans):
        if name not in done:
            visit(name)

    cut = {
        name: _replace_refs(plan, {target for owner, target in back if owner == name})
        for name, plan in plans.items()
    }
    return cut, tuple(sorted(back))


def ordered_references(plan: _Plan) -> tuple[str, ...]:
    """Имена определений, на которые ссылается план, в порядке обхода и без повторов.

    Порядок — тот же, что у рендера: свойства по имени, затем тип дополнительных
    значений, элементы массива, варианты объединения по порядку.
    """
    found: dict[str, None] = {}

    def walk(item: _Plan) -> None:
        if isinstance(item, _Ref):
            found.setdefault(item.name, None)
        elif isinstance(item, _DictPlan):
            for _, sub, _ in item.entries:
                walk(sub)
            if item.additional is not None:
                walk(item.additional)
        elif isinstance(item, _ListPlan):
            walk(item.items)
        elif isinstance(item, _UnionPlan):
            for variant in item.variants:
                walk(variant)

    walk(plan)
    return tuple(found)


def cut_targets(plans: Iterable[_Plan]) -> set[str]:
    """Имена определений, на которых в планах стоят отсечённые узлы."""
    names: set[str] = set()

    def walk(item: _Plan) -> None:
        if isinstance(item, _Cut):
            names.add(item.name)
        elif isinstance(item, _DictPlan):
            for _, sub, _ in item.entries:
                walk(sub)
            if item.additional is not None:
                walk(item.additional)
        elif isinstance(item, _ListPlan):
            walk(item.items)
        elif isinstance(item, _UnionPlan):
            for variant in item.variants:
                walk(variant)

    for plan in plans:
        walk(plan)
    return names


def recursion_contract(
    plans: Mapping[str, _Plan], definitions: Mapping[str, SchemaNode]
) -> RecursionContract | None:
    """Контракт рекурсии для отсечённых узлов планов; ``None``, если циклов нет."""
    names = cut_targets(plans.values())
    if not names:
        return None
    return RecursionContract(definitions_json_schema(names, definitions))


def _replace_refs(plan: _Plan, targets: set[str]) -> _Plan:
    """Заменить ссылки на ``targets`` отсечёнными узлами."""
    if not targets:
        return plan
    if isinstance(plan, _Ref):
        return _Cut(name=plan.name) if plan.name in targets else plan
    if isinstance(plan, _DictPlan):
        return _DictPlan(
            entries=tuple(
                (name, _replace_refs(sub, targets), is_optional)
                for name, sub, is_optional in plan.entries
            ),
            open=plan.open,
            additional=None if plan.additional is None else _replace_refs(plan.additional, targets),
        )
    if isinstance(plan, _ListPlan):
        return _ListPlan(items=_replace_refs(plan.items, targets), calls=plan.calls)
    if isinstance(plan, _UnionPlan):
        return _UnionPlan(variants=tuple(_replace_refs(item, targets) for item in plan.variants))
    return plan


def referenced_names(plan: _Plan) -> set[str]:
    """Имена определений, на которые ссылается план (без транзитивности).

    Отсечённые узлы сюда не входят: они не зависимость порядка сборки, а поздняя
    ссылка через :class:`~geas.integrations.d42.recursion.RecursionContract`.
    """
    if isinstance(plan, _Ref):
        return {plan.name}
    if isinstance(plan, _DictPlan):
        names: set[str] = set()
        for _, sub, _ in plan.entries:
            names |= referenced_names(sub)
        if plan.additional is not None:
            names |= referenced_names(plan.additional)
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
    Планы должны быть уже пропущены через :func:`cut_cycles`; оставшийся цикл —
    ошибка вызывающего, она отклоняется :class:`~geas.errors.RecursiveSchemaError`
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
                f"в планах d42 остался цикл {cycle}: планы не пропущены через cut_cycles"
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
    dropped: _Dropped,
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
        return _plan_array(node, definitions, path, definition, dropped)
    if isinstance(node, ObjectNode):
        return _plan_object(node, definitions, path, definition, dropped)
    if isinstance(node, UnionNode):
        return _plan_union(node, definitions, path, definition, dropped)
    if isinstance(node, AllOfNode):
        return _plan_all_of(node, definitions, path, definition, dropped)
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
    if node.pattern is not None:
        try:
            re.compile(node.pattern)
        except re.error as error:
            # Нормализация такой паттерн отклоняет; это защита для IR, собранного
            # вручную: иначе re.error при enum и DeclarationError d42 без него.
            raise _error(
                UnsupportedConstructError,
                f"pattern {node.pattern!r} не разбирается регулярными выражениями Python "
                f"({error}): d42 его не проверит и не сгенерирует",
                node,
                path,
                definition,
            ) from None
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
    if node.format in INTEGER_FORMAT_BOUNDS:
        format_minimum, format_maximum = INTEGER_FORMAT_BOUNDS[node.format]
        minimum = _tighter(minimum, format_minimum, max)
        maximum = _tighter(maximum, format_maximum, min)

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
        # 1 и 1.0 — одно значение enum. Сначала все float, потом целые: проекция
        # фикстуры списка с uniqueItems берёт первые N вариантов, а 1 и 1.0 d42
        # считает дубликатами.
        floats = tuple(dict.fromkeys(float(value) for value in node.enum))
        integers = tuple(int(value) for value in floats if value.is_integer())
        leaves = (
            *(_Leaf(base="float", literal=value) for value in floats),
            *(_Leaf(base=_JSON_INT, literal=value) for value in integers),
        )
        return leaves[0] if len(leaves) == 1 else _UnionPlan(variants=leaves)

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
    fractional = _Leaf(base="float", calls=_bound_calls(minimum, maximum))
    int_minimum = None if minimum is None else math.ceil(minimum)
    int_maximum = None if maximum is None else math.floor(maximum)
    if int_minimum is not None and int_maximum is not None and int_minimum > int_maximum:
        return fractional
    # float — первым: проекция фикстуры берёт первый вариант.
    return _UnionPlan(
        variants=(fractional, _Leaf(base=_JSON_INT, calls=_bound_calls(int_minimum, int_maximum)))
    )


def _plan_array(
    node: ArrayNode,
    definitions: Mapping[str, SchemaNode],
    path: ContractPath,
    definition: str | None,
    dropped: _Dropped,
) -> _Plan:
    if node.min_items is not None and node.max_items is not None:
        _require_range(node, node.min_items, node.max_items, "minItems", path, definition)
    items = plan_node(
        node.items, definitions, path=(*path, ARRAY_ITEMS), definition=definition, dropped=dropped
    )
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
    dropped: _Dropped,
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
                plan_node(
                    prop.schema,
                    definitions,
                    path=(*path, prop.name),
                    definition=definition,
                    dropped=dropped,
                ),
                not prop.required,
            )
        )
    additional = (
        plan_node(
            node.additional_properties,
            definitions,
            path=(*path, ADDITIONAL_PROPERTIES),
            definition=definition,
            dropped=dropped,
        )
        if isinstance(node.additional_properties, SchemaNode)
        else None
    )
    for name in node.required_undeclared:
        if name in seen:
            raise _error(
                UnsupportedConstructError,
                f"ключ {name!r} одновременно объявлен свойством и обязательным без схемы",
                node,
                path,
                definition,
            )
        if node.additional_properties is AdditionalProperties.FORBIDDEN:
            raise _error(
                UnsupportedConstructError,
                f"обязательный ключ {name!r} запрещён additionalProperties: false: "
                f"такому объекту не соответствует ни одно значение",
                node,
                path,
                definition,
            )
        # Ключ обязан быть, а значение ограничивает только additionalProperties:
        # разрешены — любое значение (это и есть контракт), типизированы — их схема.
        entries.append((name, additional if additional is not None else _ANY_PLAN, False))
    return _DictPlan(
        entries=tuple(sorted(entries, key=lambda entry: entry[0])),
        open=node.additional_properties is AdditionalProperties.ALLOWED,
        additional=additional,
    )


def _plan_union(
    node: UnionNode,
    definitions: Mapping[str, SchemaNode],
    path: ContractPath,
    definition: str | None,
    dropped: _Dropped,
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
        plan_node(
            variant,
            definitions,
            path=(*path, variant_segment(index)),
            definition=definition,
            dropped=dropped,
        )
        for index, variant in enumerate(node.variants)
    )
    return _UnionPlan(variants=variants)


def _plan_all_of(
    node: AllOfNode,
    definitions: Mapping[str, SchemaNode],
    path: ContractPath,
    definition: str | None,
    dropped: _Dropped,
) -> _Plan:
    """Распределить ``allOf`` по объединению и спланировать результат как обычный узел.

    Распределение идёт до :func:`cut_cycles`: ссылки внутри веток — обычные
    ``RefNode``, и цикл через них отсекается тем же механизмом, что и любой другой.
    """
    try:
        distribution = distribute(node, definitions, path=path)
    except MissingDefinitionError as error:
        raise _error(RefResolutionError, error.reason, error.node, error.path, definition) from None
    except IntersectionError as error:
        branch = f" ветки {error.branch}" if error.branch is not None else ""
        raise _error(
            UnsupportedConstructError,
            f"allOf не выражается в d42: пересечение{branch} не доказуемо — {error.reason}. "
            f"Слейте части в один объект в спецификации либо оформите waiver",
            error.node,
            error.path,
            definition,
        ) from None
    if isinstance(distribution.node, Empty):
        raise _error(
            UnsupportedConstructError,
            f"allOf не допускает ни одного значения: {distribution.node.reason}. "
            f"Контракт противоречив — исправьте спецификацию либо оформите waiver",
            node,
            path,
            definition,
        )
    if dropped is not None:
        where = _where(path, definition)
        dropped.extend(
            DroppedBranchNote(where=where, branch=item.branch, reason=item.reason)
            for item in distribution.dropped
        )
    return plan_node(
        distribution.node, definitions, path=path, definition=definition, dropped=dropped
    )


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


def _where(path: ContractPath, definition: str | None) -> str:
    """Contract path с именем определения: ``Имя:/путь``."""
    where = format_contract_path(path)
    return where if definition is None else f"{definition}:{where}"


def _error(
    error_type: type[Exception],
    message: str,
    node: SchemaNode,
    path: ContractPath,
    definition: str | None,
) -> Exception:
    """Собрать ошибку с contract path и координатами узла."""
    return error_type(  # type: ignore[call-arg]
        f"{message} [contract path {_where(path, definition)}]",
        source=node.origin.source or None,
        json_pointer=node.origin.pointer or None,
    )


# --------------------------------------------------------------------------------------
# Сборка объектов d42
# --------------------------------------------------------------------------------------


def build(
    plan: _Plan,
    built: Mapping[str, GenericSchema],
    contract: RecursionContract | None = None,
) -> GenericSchema:
    """Собрать объект d42 по плану.

    ``built`` — уже собранные определения, ``contract`` — контракт рекурсии, из
    которого берутся отсечённые узлы.
    """
    if isinstance(plan, _Ref):
        return built[plan.name]
    if isinstance(plan, _Cut):
        if contract is None:
            raise RecursiveSchemaError(
                f"в плане есть отсечённый узел {plan.name!r}, но контракт рекурсии не передан"
            )
        return contract.ref(plan.name)
    if isinstance(plan, _Leaf):
        result: Any = json_int if plan.base == _JSON_INT else getattr(schema, plan.base)
        if not isinstance(plan.literal, _NoLiteral):
            result = result(plan.literal)
        for name, args in plan.calls:
            result = getattr(result, name)(*args)
        return result  # type: ignore[no-any-return]
    if isinstance(plan, _DictPlan):
        keys: dict[Any, Any] = {}
        for name, sub, is_optional in plan.entries:
            keys[optional(name) if is_optional else name] = build(sub, built, contract)
        if plan.additional is not None:
            return typed_dict(schema.dict(keys), additional=build(plan.additional, built, contract))
        if plan.open:
            keys[...] = ...
        return schema.dict(keys)
    if isinstance(plan, _ListPlan):
        listed: Any = schema.list(build(plan.items, built, contract))
        for name, args in plan.calls:
            listed = getattr(listed, name)(*args)
        return listed  # type: ignore[no-any-return]
    return schema.any(*(build(variant, built, contract) for variant in plan.variants))


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

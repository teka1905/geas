"""Пересечение узлов IR: d42-проекция ``allOf``.

Зачем
-----

У d42 нет пересечения типов: ``schema.dict`` нельзя «домножить» на другой
``schema.dict``. Но самый частый ``allOf`` в реальных спецификациях — полиморфный
DTO в стиле springdoc: у схемы одновременно ``properties``, ``oneOf`` и
``discriminator``. Нормализатор сохраняет его точно —
``AllOfNode(parts=(база, UnionNode))``, — и JSON Schema проверяет его как ``allOf``.

Для d42 пересечение распределяется по объединению::

    allOf(base, oneOf(V1, ..., Vn))  →  any(base ∧ V1, ..., base ∧ Vn)

Равенство точное: значение проходит ``base`` и хотя бы один вариант тогда и
только тогда, когда оно проходит хотя бы одно ``base ∧ Vk``. Эксклюзивность
``oneOf`` и ``discriminator`` остаются на JSON-Schema-пути — как у любого
``oneOf`` в d42 (шапка :mod:`~geas.integrations.d42.converter`).

Модуль работает только с IR и нужен только d42-пути: JSON Schema контракта
по-прежнему содержит ``allOf`` как есть.

Результат ``∧``
---------------

Пересечение двух узлов даёт одно из трёх:

* **узел IR**, допускающий ровно те значения, что допускают обе стороны;
* :class:`Empty` — пересечение не допускает ни одного значения. Это тоже точный
  ответ: ветка, которая не допускает значений, выбрасывается из объединения без
  потери смысла. Если пусты все ветки, ``allOf`` отклоняется;
* :class:`UnprovableIntersectionError` — пересечение не выражается одним узлом IR
  доказуемо точно (два разных ``pattern``, ``multipleOf`` у ``number``, ключ,
  который в открытом объекте обязан отсутствовать). Это fail closed.

Правила
-------

* ``RefNode`` разворачивается через определения бандла; пары ссылок, которые
  уже пересекаются выше по стеку, — «не доказуемо» (иначе обход не кончится);
* семантически равные узлы (равная JSON Schema) → исходный узел, по возможности
  ``RefNode``: так ветка ``base ∧ Vk``, совпавшая с ``Vk``, остаётся ссылкой
  ``GeneratedVkSchema``, а не инлайн-копией;
* ``AnyNode`` (явный waiver) ∧ X → X;
* объект ∧ объект: свойства объединяются, общие пересекаются рекурсивно,
  ``required`` — объединение. Ключ, объявленный только одной стороной, у другой
  стороны подчиняется её ``additionalProperties``: разрешены — свойство как есть,
  типизированы — пересечение со схемой значений, запрещены — ключ недопустим.
  Недопустимый обязательный ключ опустошает пересечение; недопустимый
  необязательный выбрасывается, только если результат закрыт (иначе
  «не доказуемо»: d42 не выражает «этого ключа быть не должно» в открытом
  словаре). ``additionalProperties`` результата — пересечение политик сторон;
* строка ∧ строка: ``enum ∩ enum`` в порядке более узкой стороны; ``enum`` ∧
  строка без ``enum`` → литералы, которые проходят ``pattern`` и длины обеих
  сторон; без ``enum`` — строгие границы длины, ``pattern`` только равный или с
  одной стороны;
* ``integer``/``number``: границы строже, ``enum ∩``; ``multipleOf`` у целых —
  наименьшее общее кратное, у ``number`` — только равный;
* ``boolean``: ``enum ∩``;
* массив ∧ массив: ``items`` пересекаются, длины строже, ``uniqueItems`` — ИЛИ;
  несовместимые элементы оставляют только пустой массив;
* ``nullable`` сохраняется, только если он есть у обеих сторон; несовместимые
  типы, обе стороны которых nullable, дают ``null``;
* ``oneOf``/``anyOf`` ∧ X → объединение ``Vi ∧ X`` без пустых вариантов;
  ``AllOfNode`` внутри (наследование ``allOf: [$ref база]``, ссылка на
  полиморфный DTO) сначала распределяется, потом пересекается;
* ``discriminator`` части ``allOf`` накладывается на ветки её вариантов после
  свёртки: свойство обязательно, а из его ``enum`` исключаются значения, которые
  ``mapping`` отдаёт другим вариантам (``if``/``then`` вместе с ``oneOf``). У строки
  без ``enum`` исключение не выражается, и ветка остаётся шире, как любой ``oneOf``;
* ``pattern``, который регулярные выражения Python не разбирают (ECMA ``\\p{Lu}``),
  при фильтрации литералов ``enum`` — «не доказуемо». Из спецификации такой паттерн
  сюда не доходит (его отклоняет нормализация) — это защита для IR, собранного вручную;
* ``format`` в d42 не переносится (шапка конвертера), поэтому в пересечении он
  сохраняется только при равенстве или с одной стороны и на точность d42 не влияет.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from geas.jsonschema_gen import node_to_json_schema
from geas.models import (
    INTEGER_FORMAT_BOUNDS,
    AdditionalProperties,
    AllOfNode,
    AnyNode,
    ArrayNode,
    BooleanNode,
    IntegerNode,
    NullNode,
    NumberNode,
    ObjectNode,
    PropertySpec,
    RefNode,
    SchemaNode,
    StringNode,
    UnionKind,
    UnionNode,
)
from geas.paths import ADDITIONAL_PROPERTIES, ARRAY_ITEMS, ContractPath

__all__ = [
    "MAX_BRANCHES",
    "Distribution",
    "DroppedBranch",
    "Empty",
    "IntersectionError",
    "MissingDefinitionError",
    "UnprovableIntersectionError",
    "distribute",
]

#: Предел числа веток после распределения. ``allOf`` из нескольких объединений
#: раскладывается в их декартово произведение; больше — почти наверняка ошибка
#: спецификации, и генерировать такой модуль бессмысленно.
MAX_BRANCHES = 1024

#: Сколько литералов ``enum`` и причин показывать в сообщениях.
_SHOWN = 5


@dataclass(frozen=True, slots=True)
class Empty:
    """Пересечение не допускает ни одного значения; ``reason`` объясняет почему."""

    reason: str


class IntersectionError(Exception):
    """Пересечение не удалось построить; несёт узел и путь для диагностики."""

    def __init__(self, reason: str, *, node: SchemaNode, path: ContractPath) -> None:
        super().__init__(reason)
        self.reason = reason
        self.node = node
        self.path = path
        #: Ветка распределения ``allOf``, в которой случилась ошибка.
        self.branch: str | None = None


class UnprovableIntersectionError(IntersectionError):
    """Пересечение не выражается одним узлом IR доказуемо точно."""


class MissingDefinitionError(IntersectionError):
    """``RefNode`` указывает на определение, которого нет в бандле."""


@dataclass(frozen=True, slots=True)
class DroppedBranch:
    """Ветка объединения, которую пересечение с остальными частями ``allOf`` опустошило."""

    branch: str
    reason: str


@dataclass(frozen=True, slots=True)
class Distribution:
    """Результат распределения ``allOf``: узел (или пустота) и выброшенные ветки."""

    node: SchemaNode | Empty
    dropped: tuple[DroppedBranch, ...] = ()


def distribute(
    node: AllOfNode, definitions: Mapping[str, SchemaNode], *, path: ContractPath = ()
) -> Distribution:
    """Распределить ``allOf`` по объединениям его частей.

    Результат — узел IR без ``AllOfNode`` на верхнем уровне (объединение веток,
    одна ветка или ``RefNode`` на часть, которой весь ``allOf`` равен) либо
    :class:`Empty`. ``nullable`` самого ``node`` не учитывается: его добавляет
    вызывающий, как для любого узла. ``dropped`` — ветки объединений, которые
    пересечение опустошило: они не допускали ни одного значения ещё в контракте.

    :raises UnprovableIntersectionError: пересечение не выражается доказуемо точно.
    :raises MissingDefinitionError: ссылка на отсутствующее определение.
    """
    return _Intersector(definitions).distribute(node, path)


@dataclass(frozen=True, slots=True)
class _Branch:
    """Ветка распределения: подпись (``None`` у безымянных) и узел или пустота.

    ``discriminators`` — ограничения ``discriminator`` объединений, из вариантов
    которых собрана ветка: ``(свойство, значения, которые mapping отдаёт другим
    вариантам)``. Они накладываются, когда свёрнуты все части ``allOf``: только
    тогда известен ``enum`` свойства, из которого значения можно исключить.
    """

    label: str | None
    value: SchemaNode | Empty
    discriminators: tuple[tuple[str, frozenset[str]], ...] = ()


#: Слот значения ключа объекта: схема свойства или политика дополнительных ключей.
_Slot = SchemaNode | AdditionalProperties


class _Intersector:
    """Одно распределение: кэш развёрнутых определений и защита от циклов."""

    def __init__(self, definitions: Mapping[str, SchemaNode]) -> None:
        self._definitions = definitions
        self._distributed: dict[str, SchemaNode | Empty] = {}
        self._distributing: list[str] = []
        self._meeting: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ распределение

    def distribute(self, node: AllOfNode, path: ContractPath) -> Distribution:
        branches = self._branches(node, path)
        dropped = tuple(
            DroppedBranch(branch=branch.label, reason=branch.value.reason)
            for branch in branches
            if branch.label is not None and isinstance(branch.value, Empty)
        )
        return Distribution(node=self._join(node, branches, path), dropped=dropped)

    def _join(
        self, node: AllOfNode, branches: Sequence[_Branch], path: ContractPath
    ) -> SchemaNode | Empty:
        """Собрать ветки в один узел; если он равен целой части ``allOf`` — вернуть её."""
        kept = [branch.value for branch in branches if not isinstance(branch.value, Empty)]
        if len(branches) == 1 and isinstance(branches[0].value, Empty):
            return branches[0].value
        if not kept:
            reasons = [
                f"{branch.label}: {branch.value.reason}" if branch.label else branch.value.reason
                for branch in branches
                if isinstance(branch.value, Empty)
            ]
            return Empty(_shorten_list("ни одна ветка не допускает значений", reasons))
        if len(kept) == 1:
            result: SchemaNode = kept[0]
        else:
            result = UnionNode(origin=node.origin, kind=self._kind_of(node), variants=tuple(kept))
        for candidate in _single_refs(node):
            semantic = self._semantic(candidate, path)
            if not isinstance(semantic, Empty) and self._same(result, semantic):
                return candidate
        return result

    def _branches(self, node: AllOfNode, path: ContractPath) -> list[_Branch]:
        """Декартово произведение альтернатив частей, свёрнутое пересечением."""
        branches: list[_Branch] = []
        for index, part in enumerate(node.parts):
            alternatives = self._alternatives(part, path)
            if index == 0:
                branches = list(alternatives)
                continue
            folded: list[_Branch] = []
            for mine in branches:
                for other in alternatives:
                    label = _join_labels(mine.label, other.label)
                    left, right = mine.value, other.value
                    if isinstance(left, Empty):
                        value: SchemaNode | Empty = left
                    elif isinstance(right, Empty):
                        value = right
                    else:
                        value = self._meet_branch(left, right, path, label)
                    folded.append(
                        _Branch(label, value, (*mine.discriminators, *other.discriminators))
                    )
            branches = folded
            if len(branches) > MAX_BRANCHES:
                raise UnprovableIntersectionError(
                    f"allOf раскладывается больше чем в {MAX_BRANCHES} веток",
                    node=node,
                    path=path,
                )
        return [self._discriminated(branch, path) for branch in branches]

    def _discriminated(self, branch: _Branch, path: ContractPath) -> _Branch:
        """Наложить на свёрнутую ветку ограничения ``discriminator`` её вариантов.

        JSON Schema объединения с ``discriminator`` требует его свойство, а каждое
        значение из ``mapping`` — ``if``/``then`` на свой вариант. Вместе с ``oneOf``
        это значит: в ветке варианта ``Vk`` значение, которое ``mapping`` отдаёт
        другому варианту, недопустимо (оно обязано пройти и тот вариант, и тогда
        подходят два). Исключение выражается, когда у свойства ветки есть ``enum``;
        у строки без ``enum`` ветка остаётся шире, как любой ``oneOf`` в d42.
        """
        value = branch.value
        for name, excluded in branch.discriminators:
            if isinstance(value, Empty):
                break
            value = self._constrain_discriminator(value, name, excluded, path)
        return _Branch(branch.label, value)

    def _constrain_discriminator(
        self, node: SchemaNode, name: str, excluded: frozenset[str], path: ContractPath
    ) -> SchemaNode | Empty:
        semantic = self._semantic(node, path)
        if isinstance(semantic, Empty):
            return semantic
        if not isinstance(semantic, ObjectNode):
            # null у nullable-части и вложенные объединения не сужаются: ``required``
            # к ним не применяется, а внутрь объединения ограничение не спускается.
            return node
        declared = semantic.property(name)
        if declared is None:
            if name in semantic.required_undeclared:
                return node
            if semantic.additional_properties is AdditionalProperties.FORBIDDEN:
                return Empty(
                    f"discriminator требует свойство {name!r}, а additionalProperties: false "
                    f"его запрещает"
                )
            return replace(
                semantic,
                required_undeclared=tuple(sorted({*semantic.required_undeclared, name})),
            )
        narrowed = self._without_values(declared.schema, excluded, path)
        if isinstance(narrowed, Empty):
            return Empty(f"discriminator {name!r}: {narrowed.reason}")
        if narrowed is declared.schema and declared.required:
            return node
        prop = PropertySpec(name=name, schema=narrowed, required=True)
        return replace(
            semantic,
            properties=tuple(prop if item.name == name else item for item in semantic.properties),
        )

    def _without_values(
        self, node: SchemaNode, excluded: frozenset[str], path: ContractPath
    ) -> SchemaNode | Empty:
        """Схема свойства без значений ``excluded``; исходный узел, если исключать нечего."""
        if not excluded:
            return node
        semantic = self._semantic(node, path)
        if isinstance(semantic, Empty):
            return semantic
        if not isinstance(semantic, StringNode) or semantic.enum is None:
            return node
        kept = tuple(value for value in semantic.enum if value not in excluded)
        if len(kept) == len(semantic.enum):
            return node
        if not kept:
            return Empty(
                f"все значения enum {_show(semantic.enum)} discriminator.mapping отдаёт "
                f"другим вариантам"
            )
        return replace(semantic, enum=kept)

    def _meet_branch(
        self, left: SchemaNode, right: SchemaNode, path: ContractPath, label: str | None
    ) -> SchemaNode | Empty:
        try:
            return self._meet(left, right, path)
        except IntersectionError as error:
            # Побеждает самая внешняя ветка: её пользователь видит в спецификации.
            if label is not None:
                error.branch = label
            raise

    def _alternatives(self, part: SchemaNode, path: ContractPath) -> list[_Branch]:
        """Альтернативы одной части ``allOf``: варианты объединения или она сама."""
        target: SchemaNode = part
        nullable = part.nullable
        if isinstance(part, RefNode):
            resolved = self._definition(part, path)
            if not isinstance(resolved, (AllOfNode, UnionNode)):
                return [_Branch(None, part)]
            target = resolved
            nullable = nullable or resolved.nullable
        if isinstance(target, UnionNode):
            alternatives: list[_Branch] = [
                _Branch(_variant_label(target, index), variant, _discriminator_of(target, variant))
                for index, variant in enumerate(target.variants)
            ]
        elif isinstance(target, AllOfNode):
            alternatives = self._nested_branches(part, target, path)
        else:
            return [_Branch(None, part)]
        if nullable:
            alternatives.append(_Branch(None, NullNode(origin=target.origin)))
        return alternatives

    def _nested_branches(
        self, part: SchemaNode, target: AllOfNode, path: ContractPath
    ) -> list[_Branch]:
        """Ветки вложенного ``allOf`` — части текущего или цели ссылки."""
        if not isinstance(part, RefNode):
            return self._branches(target, path)
        with self._guard(part, target, path):
            return self._branches(target, path)

    # -------------------------------------------------------------- пересечение

    def _meet(self, left: SchemaNode, right: SchemaNode, path: ContractPath) -> SchemaNode | Empty:
        """``left ∧ right``; при равенстве одной из сторон возвращается она сама."""
        if left is right:
            return left
        if isinstance(left, AnyNode):
            return right
        if isinstance(right, AnyNode):
            return left
        if isinstance(left, RefNode) and isinstance(right, RefNode):
            if left.name == right.name:
                return right if left.nullable and not right.nullable else left
            key = (left.name, right.name)
            if key in self._meeting:
                raise UnprovableIntersectionError(
                    f"пересечение {left.name} и {right.name} ссылается само на себя: "
                    f"оно не сводится к конечному узлу",
                    node=right,
                    path=path,
                )
            self._meeting.add(key)
            try:
                return self._meet_semantic(left, right, path)
            finally:
                self._meeting.discard(key)
        return self._meet_semantic(left, right, path)

    def _meet_semantic(
        self, left: SchemaNode, right: SchemaNode, path: ContractPath
    ) -> SchemaNode | Empty:
        """Пересечь развёрнутые стороны и вернуть исходную, если результат ей равен."""
        left_semantic = self._semantic(left, path)
        right_semantic = self._semantic(right, path)
        if isinstance(left_semantic, Empty):
            return left_semantic
        if isinstance(right_semantic, Empty):
            return right_semantic
        if self._same(left_semantic, right_semantic):
            return _original(right, right_semantic)
        result = self._meet_nodes(left_semantic, right_semantic, path)
        if isinstance(result, Empty):
            return result
        for original, semantic in ((right, right_semantic), (left, left_semantic)):
            if result is semantic or self._same(result, semantic):
                return _original(original, semantic)
        return result

    def _meet_nodes(
        self, left: SchemaNode, right: SchemaNode, path: ContractPath
    ) -> SchemaNode | Empty:
        if isinstance(left, AnyNode):
            return right
        if isinstance(right, AnyNode):
            return left
        if isinstance(left, UnionNode):
            return self._meet_union(left, right, path, union_first=True)
        if isinstance(right, UnionNode):
            return self._meet_union(right, left, path, union_first=False)
        if isinstance(left, NullNode) or isinstance(right, NullNode):
            null, other = (left, right) if isinstance(left, NullNode) else (right, left)
            if _admits_null(other):
                return null
            return Empty(f"null несовместим с {_type_name(other)}")
        if type(left) is not type(right):
            if {type(left), type(right)} == {IntegerNode, NumberNode}:
                raise UnprovableIntersectionError(
                    "integer и number: пересечение — целые числа с границами number, "
                    "перевод границ не реализован",
                    node=right,
                    path=path,
                )
            if left.nullable and right.nullable:
                return NullNode(origin=right.origin)
            return Empty(f"типы {_type_name(left)} и {_type_name(right)} несовместимы")
        if isinstance(left, ObjectNode) and isinstance(right, ObjectNode):
            return self._meet_objects(left, right, path)
        if isinstance(left, ArrayNode) and isinstance(right, ArrayNode):
            return self._meet_arrays(left, right, path)
        if isinstance(left, StringNode) and isinstance(right, StringNode):
            return _meet_strings(left, right, path)
        if isinstance(left, IntegerNode) and isinstance(right, IntegerNode):
            return _meet_integers(left, right, path)
        if isinstance(left, NumberNode) and isinstance(right, NumberNode):
            return _meet_numbers(left, right, path)
        if isinstance(left, BooleanNode) and isinstance(right, BooleanNode):
            return _meet_booleans(left, right)
        raise UnprovableIntersectionError(
            f"пересечение узлов {type(left).__name__} не поддержано", node=right, path=path
        )

    def _meet_union(
        self, union: UnionNode, other: SchemaNode, path: ContractPath, *, union_first: bool
    ) -> SchemaNode | Empty:
        variants: list[SchemaNode] = []
        reasons: list[str] = []
        for index, variant in enumerate(union.variants):
            met = (
                self._meet(variant, other, path)
                if union_first
                else self._meet(other, variant, path)
            )
            if isinstance(met, Empty):
                reasons.append(f"{_variant_label(union, index)}: {met.reason}")
            else:
                variants.append(met)
        nullable = union.nullable and _admits_null(other)
        if not variants:
            if nullable:
                return NullNode(origin=union.origin)
            return Empty(_shorten_list(f"ни один вариант {union.kind.value} не совместим", reasons))
        if (
            nullable == union.nullable
            and len(variants) == len(union.variants)
            and all(met is variant for met, variant in zip(variants, union.variants))
        ):
            return union
        if len(variants) == 1:
            return _with_nullable(variants[0]) if nullable else variants[0]
        return UnionNode(
            origin=union.origin, kind=union.kind, variants=tuple(variants), nullable=nullable
        )

    def _meet_objects(
        self, left: ObjectNode, right: ObjectNode, path: ContractPath
    ) -> ObjectNode | Empty:
        extra = self._meet_slot(
            left.additional_properties,
            right.additional_properties,
            (*path, ADDITIONAL_PROPERTIES),
        )
        additional: AdditionalProperties | SchemaNode = (
            AdditionalProperties.FORBIDDEN if isinstance(extra, Empty) else extra
        )
        names = sorted(
            {prop.name for prop in (*left.properties, *right.properties)}
            | {*left.required_undeclared, *right.required_undeclared}
        )
        properties: list[PropertySpec] = []
        undeclared: list[str] = []
        for name in names:
            mine, theirs = left.property(name), right.property(name)
            required = (
                bool(mine and mine.required)
                or bool(theirs and theirs.required)
                or name in left.required_undeclared
                or name in right.required_undeclared
            )
            met = self._meet_slot(
                mine.schema if mine is not None else left.additional_properties,
                theirs.schema if theirs is not None else right.additional_properties,
                (*path, name),
            )
            if met is AdditionalProperties.FORBIDDEN or isinstance(met, Empty):
                reason = (
                    met.reason
                    if isinstance(met, Empty)
                    else "одна из частей запрещает этот ключ (additionalProperties: false)"
                )
                if required:
                    return Empty(f"обязательное свойство {name!r}: {reason}")
                if additional is not AdditionalProperties.FORBIDDEN:
                    raise UnprovableIntersectionError(
                        f"свойство {name!r} допустимо только отсутствующим ({reason}), "
                        f"а d42 не выражает запрет ключа в открытом словаре",
                        node=theirs.schema if theirs is not None else _declared(mine, right),
                        path=(*path, name),
                    )
                continue
            if met is AdditionalProperties.ALLOWED:
                # Схемы нет ни у одной стороны: ключ пришёл из required без properties
                # и остаётся обязательным ключом без схемы.
                undeclared.append(name)
                continue
            if not isinstance(met, SchemaNode):  # pragma: no cover — политики исчерпаны выше
                raise AssertionError(f"свойство {name!r} без схемы")
            properties.append(PropertySpec(name=name, schema=met, required=required))
        minimum = _stricter(left.min_properties, right.min_properties, max)
        maximum = _stricter(left.max_properties, right.max_properties, min)
        if minimum is not None and maximum is not None and minimum > maximum:
            return Empty(f"minProperties={minimum} больше maxProperties={maximum}")
        return ObjectNode(
            origin=right.origin,
            nullable=left.nullable and right.nullable,
            properties=tuple(properties),
            additional_properties=additional,
            min_properties=minimum,
            max_properties=maximum,
            required_undeclared=tuple(undeclared),
        )

    def _meet_slot(
        self, left: _Slot, right: _Slot, path: ContractPath
    ) -> SchemaNode | AdditionalProperties | Empty:
        """Пересечение слотов: разрешено — нейтральный элемент, запрещено — поглощающий."""
        if left is AdditionalProperties.FORBIDDEN or right is AdditionalProperties.FORBIDDEN:
            return AdditionalProperties.FORBIDDEN
        if left is AdditionalProperties.ALLOWED:
            return right
        if right is AdditionalProperties.ALLOWED:
            return left
        if not isinstance(left, SchemaNode) or not isinstance(right, SchemaNode):
            raise AssertionError("неизвестная политика additionalProperties")  # pragma: no cover
        return self._meet(left, right, path)

    def _meet_arrays(
        self, left: ArrayNode, right: ArrayNode, path: ContractPath
    ) -> ArrayNode | Empty:
        minimum = _stricter(left.min_items, right.min_items, max)
        maximum = _stricter(left.max_items, right.max_items, min)
        if minimum is not None and maximum is not None and minimum > maximum:
            return Empty(f"minItems={minimum} больше maxItems={maximum}")
        items = self._meet(left.items, right.items, (*path, ARRAY_ITEMS))
        nullable = left.nullable and right.nullable
        unique = left.unique_items or right.unique_items
        if isinstance(items, Empty):
            if minimum:
                return Empty(f"элементы массива: {items.reason}")
            # Элементы несовместимы — годится только пустой массив. Схема элементов
            # у пустого массива ни на что не влияет, поэтому берётся любая сторона.
            return ArrayNode(origin=right.origin, nullable=nullable, items=right.items, max_items=0)
        return ArrayNode(
            origin=right.origin,
            nullable=nullable,
            items=items,
            min_items=minimum,
            max_items=maximum,
            unique_items=unique,
        )

    # ------------------------------------------------------------ развёртка

    def _semantic(self, node: SchemaNode, path: ContractPath) -> SchemaNode | Empty:
        """Узел без ``RefNode`` и ``AllOfNode`` сверху, с учётом ``nullable`` ссылки."""
        if isinstance(node, RefNode):
            target = self._definition(node, path)
            if isinstance(target, RefNode):
                with self._guard(node, target, path):
                    semantic = self._semantic(target, path)
            elif isinstance(target, AllOfNode):
                semantic = self._distributed_definition(node, target, path)
            else:
                semantic = target
            return _nullable_semantic(semantic, node.nullable, node)
        if isinstance(node, AllOfNode):
            joined = self._join(node, self._branches(node, path), path)
            semantic = joined if isinstance(joined, Empty) else self._semantic(joined, path)
            return _nullable_semantic(semantic, node.nullable, node)
        return node

    def _distributed_definition(
        self, ref: RefNode, target: AllOfNode, path: ContractPath
    ) -> SchemaNode | Empty:
        cached = self._distributed.get(ref.name)
        if cached is not None:
            return cached
        with self._guard(ref, target, path):
            joined = self._join(target, self._branches(target, path), path)
            semantic = self._semantic(joined, path) if isinstance(joined, RefNode) else joined
        result = _nullable_semantic(semantic, target.nullable, target)
        self._distributed[ref.name] = result
        return result

    def _definition(self, ref: RefNode, path: ContractPath) -> SchemaNode:
        target = self._definitions.get(ref.name)
        if target is None:
            raise MissingDefinitionError(
                f"определение {ref.name!r} отсутствует в бандле операции", node=ref, path=path
            )
        return target

    def _guard(self, ref: RefNode, target: SchemaNode, path: ContractPath) -> _Guard:
        return _Guard(self._distributing, ref, target, path)

    def _kind_of(self, node: AllOfNode) -> UnionKind:
        """``oneOf``, если все объединения частей — ``oneOf``; иначе ``anyOf``.

        Произведение ``oneOf`` точно остаётся ``oneOf``: значение подходит ровно под
        одну пару вариантов тогда и только тогда, когда оно подходит ровно под один
        вариант каждого объединения. d42 вида объединения не различает.
        """
        kinds: set[UnionKind] = set()
        pending: list[SchemaNode] = list(node.parts)
        seen: set[str] = set()
        while pending:
            part = pending.pop()
            if isinstance(part, RefNode):
                if part.name in seen:
                    continue
                seen.add(part.name)
                target = self._definitions.get(part.name)
                if isinstance(target, (UnionNode, AllOfNode)):
                    pending.append(target)
            elif isinstance(part, UnionNode):
                kinds.add(part.kind)
            elif isinstance(part, AllOfNode):
                pending.extend(part.parts)
        return UnionKind.ANY_OF if UnionKind.ANY_OF in kinds else UnionKind.ONE_OF

    # ------------------------------------------------------------ равенство

    @staticmethod
    def _same(left: SchemaNode, right: SchemaNode) -> bool:
        """Семантическое равенство: равная JSON Schema (``origin`` не участвует)."""
        return node_to_json_schema(left) == node_to_json_schema(right)


class _Guard:
    """Контекст «разворачиваю определение»: повторный вход — цикл через ``allOf``."""

    __slots__ = ("_path", "_ref", "_stack", "_target")

    def __init__(
        self, stack: list[str], ref: RefNode, target: SchemaNode, path: ContractPath
    ) -> None:
        self._stack = stack
        self._ref = ref
        self._target = target
        self._path = path

    def __enter__(self) -> None:
        if self._ref.name in self._stack:
            cycle = " -> ".join([*self._stack[self._stack.index(self._ref.name) :], self._ref.name])
            raise UnprovableIntersectionError(
                f"allOf ссылается сам на себя через {cycle}: такое пересечение не сводится "
                f"к конечному объединению",
                node=self._ref,
                path=self._path,
            )
        self._stack.append(self._ref.name)

    def __exit__(self, *_: object) -> None:
        self._stack.pop()


# --------------------------------------------------------------------------------------
# Скаляры
# --------------------------------------------------------------------------------------


def _meet_strings(left: StringNode, right: StringNode, path: ContractPath) -> StringNode | Empty:
    nullable = left.nullable and right.nullable
    string_format = _common_format(left.format, right.format)
    if left.enum is not None or right.enum is not None:
        for side in (left, right):
            if side.pattern is None:
                continue
            try:
                re.compile(side.pattern)
            except re.error as error:
                raise UnprovableIntersectionError(
                    f"pattern {side.pattern!r} не разбирается регулярными выражениями Python "
                    f"({error}): литералы enum по нему не проверить",
                    node=right,
                    path=path,
                ) from None
        values = _enum_meet(left.enum, right.enum)
        kept = tuple(
            value for value in values if _fits_string(left, value) and _fits_string(right, value)
        )
        if not kept:
            return Empty(_enum_conflict(left.enum, right.enum, values))
        # Остальные ограничения уже проверены на литералах и в enum-узле избыточны.
        return StringNode(origin=right.origin, nullable=nullable, format=string_format, enum=kept)

    if left.pattern is not None and right.pattern is not None and left.pattern != right.pattern:
        raise UnprovableIntersectionError(
            f"два разных pattern ({left.pattern!r} и {right.pattern!r}): d42 держит одну "
            f"регулярку на строку",
            node=right,
            path=path,
        )
    minimum = _stricter(left.min_length, right.min_length, max)
    maximum = _stricter(left.max_length, right.max_length, min)
    if minimum is not None and maximum is not None and minimum > maximum:
        return Empty(f"minLength={minimum} больше maxLength={maximum}")
    return StringNode(
        origin=right.origin,
        nullable=nullable,
        format=string_format,
        pattern=left.pattern if left.pattern is not None else right.pattern,
        min_length=minimum,
        max_length=maximum,
    )


def _meet_integers(
    left: IntegerNode, right: IntegerNode, path: ContractPath
) -> IntegerNode | Empty:
    nullable = left.nullable and right.nullable
    integer_format = left.format if left.format == right.format else None
    if integer_format is None and (left.format is None or right.format is None):
        integer_format = left.format or right.format
    # Границы форматов сворачиваются в явные: формат результата может потеряться.
    minimum = _stricter(_integer_bound(left, 0), _integer_bound(right, 0), max)
    maximum = _stricter(_integer_bound(left, 1), _integer_bound(right, 1), min)
    exclusive_minimum = _stricter(left.exclusive_minimum, right.exclusive_minimum, max)
    exclusive_maximum = _stricter(left.exclusive_maximum, right.exclusive_maximum, min)
    multiple_of = _stricter(left.multiple_of, right.multiple_of, math.lcm)

    if left.enum is not None or right.enum is not None:
        values = _enum_meet(left.enum, right.enum)
        kept = tuple(
            value
            for value in values
            if _fits_number(value, minimum, maximum, exclusive_minimum, exclusive_maximum)
            and (multiple_of is None or value % multiple_of == 0)
        )
        if not kept:
            return Empty(_enum_conflict(left.enum, right.enum, values))
        return IntegerNode(origin=right.origin, nullable=nullable, format=integer_format, enum=kept)

    lower = _stricter(minimum, None if exclusive_minimum is None else exclusive_minimum + 1, max)
    upper = _stricter(maximum, None if exclusive_maximum is None else exclusive_maximum - 1, min)
    if lower is not None and upper is not None and lower > upper:
        return Empty(f"нижняя граница {lower} больше верхней {upper}")
    return IntegerNode(
        origin=right.origin,
        nullable=nullable,
        format=integer_format,
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
        exclusive_maximum=exclusive_maximum,
        multiple_of=multiple_of,
    )


def _meet_numbers(left: NumberNode, right: NumberNode, path: ContractPath) -> NumberNode | Empty:
    if (
        left.multiple_of is not None
        and right.multiple_of is not None
        and left.multiple_of != right.multiple_of
    ):
        raise UnprovableIntersectionError(
            f"multipleOf {left.multiple_of!r} и {right.multiple_of!r} у number: общий шаг "
            f"вещественных чисел точно не считается",
            node=right,
            path=path,
        )
    multiple_of = left.multiple_of if left.multiple_of is not None else right.multiple_of
    nullable = left.nullable and right.nullable
    number_format = _common_format(left.format, right.format)
    minimum = _stricter(left.minimum, right.minimum, max)
    maximum = _stricter(left.maximum, right.maximum, min)
    exclusive_minimum = _stricter(left.exclusive_minimum, right.exclusive_minimum, max)
    exclusive_maximum = _stricter(left.exclusive_maximum, right.exclusive_maximum, min)

    if left.enum is not None or right.enum is not None:
        if multiple_of is not None:
            raise UnprovableIntersectionError(
                "enum у number рядом с multipleOf: делимость вещественных литералов точно "
                "не проверяется",
                node=right,
                path=path,
            )
        values = _enum_meet(left.enum, right.enum)
        kept = tuple(
            value
            for value in values
            if _fits_number(value, minimum, maximum, exclusive_minimum, exclusive_maximum)
        )
        if not kept:
            return Empty(_enum_conflict(left.enum, right.enum, values))
        return NumberNode(origin=right.origin, nullable=nullable, format=number_format, enum=kept)

    if minimum is not None and maximum is not None and minimum > maximum:
        return Empty(f"minimum={minimum!r} больше maximum={maximum!r}")
    return NumberNode(
        origin=right.origin,
        nullable=nullable,
        format=number_format,
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
        exclusive_maximum=exclusive_maximum,
        multiple_of=multiple_of,
    )


def _meet_booleans(left: BooleanNode, right: BooleanNode) -> BooleanNode | Empty:
    nullable = left.nullable and right.nullable
    if left.enum is None and right.enum is None:
        return BooleanNode(origin=right.origin, nullable=nullable)
    values = _enum_meet(left.enum, right.enum)
    if not values:
        return Empty(_enum_conflict(left.enum, right.enum, values))
    return BooleanNode(origin=right.origin, nullable=nullable, enum=values)


# --------------------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------------------


def _enum_meet(left: tuple[Any, ...] | None, right: tuple[Any, ...] | None) -> tuple[Any, ...]:
    """``enum ∩ enum`` в порядке более узкой стороны (при равенстве — левой)."""
    if left is None:
        return right or ()
    if right is None:
        return left
    narrow, wide = (left, right) if len(left) <= len(right) else (right, left)
    return tuple(value for value in narrow if any(_same_literal(value, item) for item in wide))


def _same_literal(left: Any, right: Any) -> bool:
    """Равенство литералов JSON: ``True`` не равен ``1``."""
    return isinstance(left, bool) == isinstance(right, bool) and left == right


def _enum_conflict(
    left: tuple[Any, ...] | None, right: tuple[Any, ...] | None, common: tuple[Any, ...]
) -> str:
    if left is not None and right is not None and not common:
        return f"enum {_show(left)} и {_show(right)} не пересекаются"
    values = left if left is not None else right
    return f"ни одно значение enum {_show(values or ())} не проходит ограничения другой части"


def _show(values: tuple[Any, ...]) -> str:
    shown = ", ".join(repr(value) for value in values[:_SHOWN])
    return f"[{shown}, …]" if len(values) > _SHOWN else f"[{shown}]"


def _shorten_list(head: str, reasons: list[str]) -> str:
    if not reasons:
        return head
    shown = "; ".join(reasons[:_SHOWN])
    tail = f"; … ещё {len(reasons) - _SHOWN}" if len(reasons) > _SHOWN else ""
    return f"{head} ({shown}{tail})"


def _fits_string(node: StringNode, value: str) -> bool:
    if node.pattern is not None and re.search(node.pattern, value) is None:
        return False
    if node.min_length is not None and len(value) < node.min_length:
        return False
    return node.max_length is None or len(value) <= node.max_length


def _fits_number(
    value: Any, minimum: Any, maximum: Any, exclusive_minimum: Any, exclusive_maximum: Any
) -> bool:
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    if exclusive_minimum is not None and value <= exclusive_minimum:
        return False
    return exclusive_maximum is None or value < exclusive_maximum


def _integer_bound(node: IntegerNode, side: int) -> int | None:
    """Включающая граница целого вместе с границей формата (``0`` — нижняя, ``1`` — верхняя)."""
    own = node.minimum if side == 0 else node.maximum
    bounds = INTEGER_FORMAT_BOUNDS.get(node.format or "")
    if bounds is None:
        return own
    bound: int = _stricter(own, bounds[side], max if side == 0 else min)
    return bound


def _stricter(first: Any, second: Any, chooser: Any) -> Any:
    """Более строгое из двух ограничений; отсутствующее не ограничивает."""
    if first is None:
        return second
    if second is None:
        return first
    return chooser(first, second)


def _common_format(left: str | None, right: str | None) -> str | None:
    """``format`` результата: общий или единственный. Разные форматы d42 не различает."""
    if left == right or right is None:
        return left
    if left is None:
        return right
    return None


def _admits_null(node: SchemaNode) -> bool:
    return isinstance(node, (NullNode, AnyNode)) or node.nullable


def _with_nullable(node: SchemaNode) -> SchemaNode:
    if isinstance(node, (NullNode, AnyNode)) or node.nullable:
        return node
    return replace(node, nullable=True)


def _nullable_semantic(
    semantic: SchemaNode | Empty, nullable: bool, owner: SchemaNode
) -> SchemaNode | Empty:
    """Добавить ``null`` к развёрнутому узлу, если его допускает ссылка или ``allOf``."""
    if not nullable:
        return semantic
    if isinstance(semantic, Empty):
        return NullNode(origin=owner.origin)
    return _with_nullable(semantic)


def _original(original: SchemaNode, semantic: SchemaNode) -> SchemaNode:
    """Исходный узел вместо равного ему результата; ``AllOfNode`` наружу не отдаётся."""
    return semantic if isinstance(original, AllOfNode) else original


def _single_refs(node: AllOfNode) -> Iterator[RefNode]:
    """Ссылки-части ``allOf``, которым может оказаться равен весь ``allOf``."""
    for part in node.parts:
        if isinstance(part, RefNode):
            yield part
        elif isinstance(part, AllOfNode) and len(part.parts) == 1 and not part.nullable:
            yield from _single_refs(part)


def _declared(prop: PropertySpec | None, fallback: SchemaNode) -> SchemaNode:
    return prop.schema if prop is not None else fallback


def _discriminator_of(
    union: UnionNode, variant: SchemaNode
) -> tuple[tuple[str, frozenset[str]], ...]:
    """Ограничение ``discriminator`` для ветки варианта: свойство и чужие значения.

    ``mapping`` ведёт в определения, поэтому инлайн-варианту не принадлежит ни одно
    значение. Неявное отображение по имени схемы JSON Schema контракта не
    проверяет (``jsonschema_gen``), и здесь его тоже нет.
    """
    discriminator = union.discriminator
    if discriminator is None:
        return ()
    own = variant.name if isinstance(variant, RefNode) else None
    foreign = frozenset(value for value, target in discriminator.mapping if target != own)
    return ((discriminator.property_name, foreign),)


def _variant_label(union: UnionNode, index: int) -> str:
    variant = union.variants[index]
    if isinstance(variant, RefNode):
        return variant.name
    return f"{union.kind.value}[{index}]"


def _join_labels(first: str | None, second: str | None) -> str | None:
    if first is None:
        return second
    if second is None:
        return first
    return f"{first} ∧ {second}"


def _type_name(node: SchemaNode) -> str:
    names: dict[type[SchemaNode], str] = {
        ObjectNode: "object",
        ArrayNode: "array",
        StringNode: "string",
        IntegerNode: "integer",
        NumberNode: "number",
        BooleanNode: "boolean",
        NullNode: "null",
    }
    return names.get(type(node), type(node).__name__)

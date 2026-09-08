"""Overlay'и: подмена генератора отдельного листа при сохранении контракта.

Зачем
-----

Сгенерированная схема описывает контракт, но не всегда описывает *осмысленные*
данные: ``schema.str`` в поле «название» даст ``"9-hK_2 0zQ"``, и такой фикстурой
неудобно смотреть скриншот теста. Хочется подменить генератор одного листа,
ничего не сломав в остальном контракте.

.. code-block:: python

    QueueDetailsSchema = overlay_generators(
        GeneratedTicketQueueDtoSchema,
        {
            ("name",): ValidQueueNameSchema,
            ("groups", EACH, "title"): ValidGroupTitleSchema,
        },
    )

Инварианты
----------

1. **Подменяется только генератор существующего листа.** Структура (словари,
   списки) и обязательность ключей всегда остаются сгенерированными: контейнеры
   пересобираются через ``props.update(...)``, ни один объект d42 не мутируется на
   месте, флаг ``optional`` копируется как есть.
2. **Путь проверяется в момент сборки overlay'я, а не в момент генерации.** Поле
   переименовали в спецификации — падает импорт модуля с overlay'ями, а не тест
   через неделю. Отсутствующий ключ, элемент не-списка, спуск внутрь листа — всё
   это :class:`~geas.errors.ContractOverlayError` с точным путём.
3. **``EACH`` — публичный маркер «каждый элемент массива»**; вложенные массивы
   поддерживаются (``("a", EACH, "b", EACH, "c")``).
4. **Через nullable спуск прозрачен.** Если по пути стоит ``X | schema.none``,
   overlay применяется к ветке ``X``, а ``schema.none`` остаётся на месте:
   поле как было nullable, так и осталось.
5. **Совместимость проверяется настолько рано, насколько она разрешима.** Тип
   ручной схемы обязан совпасть с типом сгенерированного листа; если лист — это
   union литералов (``enum``), ручной генератор обязан быть его подмножеством.
   Несовпадение типа или значение вне ``enum`` — ошибка сразу, при сборке overlay'я.

Что нельзя проверить заранее
----------------------------

``pattern`` и границы (``minLength``, ``minimum``, ...) разрешимы только для
конкретного значения: «регулярка A ⊆ регулярка B» в общем виде не считается, а
пересечения схем в d42 нет — ``schema.str.regex(...)`` нельзя «домножить» на
сгенерированный ``schema.str.len(1, 32)``. Поэтому здесь применяется второй
вариант из двух допустимых: генерируем ручной схемой, а результат проверяем по
**исходному сгенерированному** контракту.

Технически: :func:`overlay_generators` запоминает исходную схему на объекте
результата (атрибут :data:`ORIGINAL_CONTRACT_ATTR`), а
:func:`geas.integrations.d42.fixtures.validate_overlay_fixture`
достаёт её и валидирует сгенерированное значение. ``build_fixture`` вызывает эту
проверку сам, поэтому значение, которое ручной генератор выдал вне контракта
(строка длиннее ``maxLength``, число вне диапазона), падает
:class:`~geas.errors.ContractOverlayError` в момент генерации, а не
уезжает в мок.

Результат overlay'я — обычная схема d42: ``fake()``, ``%`` (``substitute``) и
``make_required()`` работают на ней как на любой другой.

Оговорка: ``substitute`` и ``make_required`` конструируют **новый** объект схемы и
про наш атрибут ничего не знают, поэтому исходный контракт на результат этих
операций не переносится — проверка по контракту до overlay'я на нём уже не
сработает. Порядок должен быть обратный: сначала ``make_required`` / ``%``, потом
:func:`overlay_generators`; либо генерируйте фикстуру прямо из результата overlay'я.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from d42.declaration import GenericSchema
from d42.declaration.types import AnySchema, DictSchema, ListSchema, NoneSchema, Schema
from niltype import Nil

from geas.errors import ContractOverlayError
from geas.paths import ARRAY_ITEMS, format_contract_path

__all__ = [
    "EACH",
    "ORIGINAL_CONTRACT_ATTR",
    "OverlayPath",
    "original_contract",
    "overlay_generators",
]


class _Each:
    """Маркер «каждый элемент массива» в overlay-пути."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "EACH"


#: Публичный маркер элементов массива: ``("groups", EACH, "title")``.
EACH = _Each()

#: Путь до листа внутри сгенерированной схемы.
OverlayPath = tuple[str | _Each, ...]

#: Атрибут, в котором на результате overlay'я хранится исходный контракт.
ORIGINAL_CONTRACT_ATTR = "__geas_original__"


def overlay_generators(
    generated: GenericSchema,
    overrides: Mapping[OverlayPath, GenericSchema],
) -> GenericSchema:
    """Подменить генераторы листьев сгенерированной схемы.

    Каждый ключ ``overrides`` — путь до листа (имена свойств и :data:`EACH`),
    значение — ручная схема-генератор. Возвращается новая схема; ``generated`` не
    изменяется.

    :raises ContractOverlayError: путь не существует, ведёт не в лист, спускается
        внутрь неоднозначного union'а либо ручная схема несовместима с контрактом.
    """
    result = generated
    # Порядок применения не влияет на результат (пути не пересекаются: путь-префикс
    # упирается в контейнер и отвергается), но сортировка делает сообщения об
    # ошибках воспроизводимыми.
    for path in sorted(overrides, key=describe_path):
        manual = overrides[path]
        if not isinstance(manual, Schema):
            raise ContractOverlayError(
                f"overlay {describe_path(path)}: ожидалась схема d42, "
                f"получено {type(manual).__name__}"
            )
        result = _apply(result, tuple(path), manual, full=tuple(path))
    if result is generated:
        # Пустой overrides: возвращаем копию, чтобы не вешать атрибут на вход.
        result = type(generated)(generated.props)
    setattr(result, ORIGINAL_CONTRACT_ATTR, original_contract(generated) or generated)
    return result


def original_contract(schema: GenericSchema) -> GenericSchema | None:
    """Исходный (до overlay'ев) контракт схемы, если он известен."""
    value = getattr(schema, ORIGINAL_CONTRACT_ATTR, None)
    return value if isinstance(value, Schema) else None


def describe_path(path: OverlayPath) -> str:
    """Строковая форма overlay-пути в грамматике contract path: ``/groups/-/title``."""
    return format_contract_path(
        tuple(ARRAY_ITEMS if isinstance(item, _Each) else str(item) for item in path)
    )


def _apply(
    node: GenericSchema,
    path: OverlayPath,
    manual: GenericSchema,
    *,
    full: OverlayPath,
) -> GenericSchema:
    """Пересобрать ``node``, заменив генератор листа по пути ``path``."""
    where = describe_path(full)

    if isinstance(node, AnySchema) and node.props.types is not Nil:
        types = tuple(node.props.types)
        rest = tuple(item for item in types if not isinstance(item, NoneSchema))
        if len(rest) != len(types) and rest:
            # Nullable: спускаемся в содержательную ветку, null сохраняем.
            if path and len(rest) > 1:
                raise ContractOverlayError(
                    f"overlay {where}: спуск внутрь union из {len(rest)} вариантов "
                    f"неоднозначен — не видно, в какой из них вести путь"
                )
            inner = rest[0] if len(rest) == 1 else AnySchema()(*rest)
            return _rebuild_nullable(types, _apply(inner, path, manual, full=full))
        if path:
            raise ContractOverlayError(
                f"overlay {where}: нельзя спускаться внутрь union — сгенерированный "
                f"узел допускает {len(types)} разных форм"
            )

    if not path:
        _check_compatible(node, manual, where)
        return manual

    head, tail = path[0], path[1:]

    if isinstance(head, _Each):
        if not isinstance(node, ListSchema) or node.props.type is Nil:
            raise ContractOverlayError(
                f"overlay {where}: EACH применим только к массиву с однородными "
                f"элементами, а здесь {_describe(node)}"
            )
        items = _apply(node.props.type, tail, manual, full=full)
        return type(node)(node.props.update(type=items))

    if not isinstance(node, DictSchema) or node.props.keys is Nil:
        raise ContractOverlayError(
            f"overlay {where}: ключ {head!r} искали в объекте, а здесь {_describe(node)}"
        )
    keys = dict(node.props.keys)
    if head not in keys:
        available = ", ".join(repr(key) for key in sorted(k for k in keys if isinstance(k, str)))
        raise ContractOverlayError(
            f"overlay {where}: ключа {head!r} нет в сгенерированной схеме. "
            f"Доступны: {available or '<нет ключей>'}"
        )
    current, is_optional = keys[head]
    # Обязательность берётся из контракта и не переопределяется overlay'ем.
    keys[head] = (_apply(current, tail, manual, full=full), is_optional)
    return type(node)(node.props.update(keys=keys))


def _rebuild_nullable(types: tuple[GenericSchema, ...], inner: GenericSchema) -> GenericSchema:
    """Собрать ``inner | schema.none``, сохранив позиции ``none`` в объединении."""
    rebuilt: list[GenericSchema] = []
    placed = False
    for item in types:
        if isinstance(item, NoneSchema):
            rebuilt.append(item)
        elif not placed:
            rebuilt.append(inner)
            placed = True
    # AnySchema.__call__ сам разворачивает вложенные объединения.
    return AnySchema()(*rebuilt)


def _check_compatible(generated: GenericSchema, manual: GenericSchema, where: str) -> None:
    """Проверить совместимость ручного генератора со сгенерированным листом."""
    if isinstance(generated, (DictSchema, ListSchema)):
        raise ContractOverlayError(
            f"overlay {where}: подменить можно только генератор листа, а здесь "
            f"{_describe(generated)} — структура всегда остаётся сгенерированной"
        )
    if isinstance(generated, AnySchema) and generated.props.types is Nil:
        # schema.any без вариантов появляется только по waiver: проверять нечего.
        return

    generated_values = _literal_values(generated)
    if generated_values is not None:
        manual_values = _literal_values(manual)
        if manual_values is None:
            raise ContractOverlayError(
                f"overlay {where}: сгенерированный лист — enum из "
                f"{len(generated_values)} литералов, поэтому ручная схема обязана "
                f"быть литералом или объединением литералов, а не {_describe(manual)}"
            )
        extra = [
            value
            for value in manual_values
            if not any(type(value) is type(item) and value == item for item in generated_values)
        ]
        if extra:
            raise ContractOverlayError(
                f"overlay {where}: значения {extra!r} отсутствуют в enum контракта "
                f"{list(generated_values)!r}"
            )
        return

    allowed = {type(item) for item in _branches(generated)}
    unexpected = [item for item in _branches(manual) if type(item) not in allowed]
    if unexpected:
        expected = ", ".join(sorted(item.__name__ for item in allowed))
        raise ContractOverlayError(
            f"overlay {where}: тип ручной схемы не совпадает с контрактом. "
            f"Ожидалось: {expected}; получено: "
            f"{', '.join(type(item).__name__ for item in unexpected)}"
        )


def _branches(node: GenericSchema) -> tuple[GenericSchema, ...]:
    """Ветки объединения (или сам узел, если это не объединение)."""
    if isinstance(node, AnySchema) and node.props.types is not Nil:
        return tuple(node.props.types)
    return (node,)


def _literal_values(node: GenericSchema) -> tuple[Any, ...] | None:
    """Значения литералов, если узел — литерал или объединение литералов."""
    values: list[Any] = []
    for branch in _branches(node):
        if isinstance(branch, (AnySchema, DictSchema, ListSchema)):
            return None
        value = branch.props.get("value")
        if value is Nil:
            return None
        values.append(value)
    return tuple(values) if values else None


def _describe(node: GenericSchema) -> str:
    """Короткое имя типа схемы для сообщений об ошибках."""
    return type(node).__name__

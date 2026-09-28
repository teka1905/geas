"""Отсечка цикла: d42-лист на месте ссылки, которая замыкает рекурсию.

Зачем
-----

d42-схема строится «по значению»: ``schema.dict({...})`` содержит сами дочерние
схемы, а не ссылки на них. Рекурсивный контракт так не выразить — развёртка не
кончится. Отказываться от d42 для всей операции из-за одного цикла глубоко
внутри DTO тоже плохо: нерекурсивная часть (а это обычно почти весь ответ)
остаётся без ``%`` и ``fake()``.

Поэтому цикл **отсекается**. Конвертер обходит граф определений от корней модуля
и на ссылке, которая возвращает обход в определение, уже лежащее на текущем пути,
ставит :class:`RecursiveRefSchema` вместо ещё одной развёртки. Всё до отсечки —
обычная generated-схема, отсечённый узел — лист.

Что умеет лист
--------------

* **Проверка** — по JSON Schema определения из контракта, со всеми ``$defs``,
  которые из него достижимы: типы, обязательность, ``enum``, ``pattern``, границы,
  ``oneOf``, ``discriminator`` и сама рекурсия проверяются точно. Лист не
  «пропускает что угодно». Единственное исключение — ``format``: как и везде на
  d42-пути, он не проверяется и не генерируется (см. шапку
  :mod:`~geas.integrations.d42.converter`). Его держит JSON-Schema-путь
  ``validate_response`` и моков, который видит рекурсию целиком.
* **Подстановка** ``%`` — делегируется типизированной d42-схеме того же
  определения. Частичная подстановка работает так же, как у обычного словаря
  d42, и уходит вглубь дерева: ``Tree % {"children": [{"name": "a"}]}``
  закрепит имя дочернего узла, не требуя перечислять остальные поля.
* **Генерация** ``fake()`` — минимальный экземпляр определения: обязательные
  поля, массивы минимальной длины, в объединении — первый вариант, из которого
  есть выход из цикла. Результат сверяется с JSON Schema; если определение вообще
  не имеет конечного значения (цикл только через обязательные поля), поднимается
  :class:`~geas.errors.RecursiveSchemaError` с перечислением таких определений.
* ``make_required()`` работает на верхнем словаре как обычно и внутрь листа не
  заходит — он и у d42 не рекурсивен.

Позднее связывание
------------------

Лист нужен раньше, чем схема его определения: определение ссылается на себя же.
Поэтому модуль сначала создаёт :class:`RecursionContract` с JSON Schema
определений, затем строит схемы, беря листья через :meth:`RecursionContract.ref`,
и в конце связывает имена с готовыми схемами через :meth:`RecursionContract.bind`.
Связь хранится в контракте, а не в ``props`` листа: равенство схем d42 сравнивает
``props``, и цель внутри них сделала бы сравнение бесконечным. Два листа равны,
если совпадают имя определения и JSON Schema контракта.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast

from d42.custom_type import CustomSchema, Props
from d42.declaration import GenericSchema, schema
from d42.declaration.types import AnySchema, DictSchema, ListSchema, Schema
from d42.generation import Generator
from d42.representation import Representor
from d42.substitution import Substitutor, SubstitutorValidator
from d42.utils import is_ellipsis
from d42.validation import Formatter, ValidationResult, Validator
from d42.validation.errors import ValidationError
from jsonschema.exceptions import ValidationError as JsonSchemaError
from niltype import Nil
from th import PathHolder

from geas.errors import RecursiveSchemaError, ValidationFailedError
from geas.jsonschema_gen import JSON_SCHEMA_DIALECT
from geas.runtime.validation import MAX_VALUE_REPR, json_schema_validator

__all__ = [
    "RecursionContract",
    "RecursiveRefSchema",
    "RecursiveRefValidationError",
]


class RecursionContract:
    """JSON Schema определений, на которых отсечены циклы, и связь листьев с d42.

    Один контракт на generated-модуль (или на один вызов ``to_d42``). ``definitions``
    — это ``$defs`` в терминах JSON Schema: определения, на которых стоят листья,
    и всё, что из них достижимо.
    """

    __slots__ = ("_definitions", "_minimal", "_ranks", "_refs", "_targets", "_validators")

    def __init__(self, definitions: Mapping[str, Mapping[str, Any]]) -> None:
        self._definitions: dict[str, Any] = {
            name: definitions[name] for name in sorted(definitions)
        }
        self._refs: dict[str, RecursiveRefSchema] = {}
        self._targets: dict[str, GenericSchema] | None = None
        self._validators: dict[str, Any] = {}
        self._minimal: dict[str, GenericSchema] = {}
        self._ranks: dict[str, int] | None = None

    # ------------------------------------------------------------- построение

    @property
    def definitions(self) -> Mapping[str, Any]:
        """``$defs`` контракта."""
        return self._definitions

    def ref(self, name: str) -> RecursiveRefSchema:
        """Лист, отсекающий цикл на определении ``name`` (один объект на имя)."""
        if name not in self._definitions:
            raise RecursiveSchemaError(
                f"определения {name!r} нет в JSON Schema контракта рекурсии; "
                f"доступны: {sorted(self._definitions)}"
            )
        existing = self._refs.get(name)
        if existing is not None:
            return existing
        if self._targets is not None:
            raise RecursiveSchemaError(
                f"контракт рекурсии уже связан, новый узел {name!r} связать было бы не с чем"
            )
        created = RecursiveRefSchema(RecursiveRefProps({"name": name, "contract": self}))
        self._refs[name] = created
        return created

    def bind(self, targets: Mapping[str, GenericSchema]) -> None:
        """Связать каждый выданный лист с d42-схемой его определения.

        Вызывается один раз, после того как все схемы модуля построены.
        """
        if self._targets is not None:
            raise RecursiveSchemaError("контракт рекурсии уже связан")
        missing = sorted(set(self._refs) - set(targets))
        if missing:
            raise RecursiveSchemaError(f"не переданы d42-схемы для отсечённых узлов {missing}")
        unknown = sorted(set(targets) - set(self._definitions))
        if unknown:
            raise RecursiveSchemaError(
                f"определений {unknown} нет в JSON Schema контракта рекурсии"
            )
        for name, target in targets.items():
            if not isinstance(target, Schema):
                raise RecursiveSchemaError(
                    f"для {name!r} ожидалась схема d42, получено {type(target).__name__}"
                )
        self._targets = dict(targets)

    def target(self, name: str) -> GenericSchema:
        """Типизированная d42-схема определения ``name``."""
        if self._targets is None or name not in self._targets:
            raise RecursiveSchemaError(
                f"отсечённый узел {name!r} не связан со схемой своего определения: "
                f"RecursionContract.bind() не вызывался"
            )
        return self._targets[name]

    # -------------------------------------------------------------- проверка

    def json_schema(self, name: str) -> dict[str, Any]:
        """Самодостаточный JSON Schema-документ одного определения."""
        return {
            "$schema": JSON_SCHEMA_DIALECT,
            "$defs": self._definitions,
            "$ref": f"#/$defs/{name}",
        }

    def errors(self, name: str, value: Any) -> list[JsonSchemaError]:
        """Ошибки JSON Schema для значения, в стабильном порядке."""
        validator = self._validators.get(name)
        if validator is None:
            validator = json_schema_validator(self.json_schema(name), check_formats=False)
            self._validators[name] = validator
        return sorted(
            validator.iter_errors(value),
            key=lambda error: ([str(part) for part in error.absolute_path], error.message),
        )

    # ------------------------------------------------------------- генерация

    def generate(self, name: str, visitor: Generator, **kwargs: Any) -> Any:
        """Минимальное значение определения, проверенное по JSON Schema."""
        value = self.minimal_schema(name).__accept__(visitor, **kwargs)
        errors = self.errors(name, value)
        if errors:
            first = errors[0]
            raise ValidationFailedError(
                f"минимальное значение определения {name!r}, собранное по d42-схеме, "
                f"не проходит JSON Schema контракта: {first.message}. Задайте значение "
                f"этого узла явно через %",
                json_pointer=_pointer(first.absolute_path) or "/",
                schema_pointer=_pointer(first.absolute_schema_path) or "/",
                validator="jsonschema",
                actual=_safe_repr(value),
            )
        return value

    def minimal_schema(self, name: str) -> GenericSchema:
        """d42-схема минимального экземпляра определения — только для генерации.

        Необязательные ключи в ней опущены, массивы имеют минимальную длину, а из
        каждого объединения оставлен первый вариант, путь из которого не
        возвращается в цикл бесконечно.
        """
        cached = self._minimal.get(name)
        if cached is not None:
            return cached
        ranks = self._productive_ranks()
        if name not in ranks:
            stuck = sorted(set(self._targets or {}) - set(ranks))
            raise RecursiveSchemaError(
                f"у определения {name!r} нет конечного значения: каждый путь из "
                f"{stuck} возвращается в цикл через обязательные поля, непустые массивы "
                f"или все варианты объединения. fake() для такого узла невозможен — "
                f"задайте значение через % или разорвите цикл в спецификации"
            )
        built = _minimal(self.target(name), self, ranks, limit=ranks[name])
        self._minimal[name] = built
        return built

    def _productive_ranks(self) -> dict[str, int]:
        """Ранги определений, у которых есть конечное значение.

        Ранг — номер раунда неподвижной точки, на котором определение стало
        строимым из уже строимых. Минимальная генерация спускается только к
        листьям со строго меньшим рангом, поэтому она всегда завершается.
        """
        if self._ranks is not None:
            return self._ranks
        targets = self._targets or {}
        ranks: dict[str, int] = {}
        round_number = 0
        while True:
            round_number += 1
            memo: dict[int, int | None] = {}
            fresh = [
                name
                for name in sorted(targets)
                if name not in ranks and _rank(targets[name], ranks, memo) is not None
            ]
            if not fresh:
                break
            for name in fresh:
                ranks[name] = round_number
        self._ranks = ranks
        return ranks

    # ---------------------------------------------------------- сравнение

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RecursionContract) and self._definitions == other._definitions

    def __hash__(self) -> int:
        return hash(tuple(self._definitions))

    def __repr__(self) -> str:
        return f"RecursionContract({sorted(self._definitions)!r})"


class RecursiveRefProps(Props):
    """``props`` листа: имя определения и контракт рекурсии."""

    @property
    def name(self) -> str:
        return cast(str, self.get("name"))

    @property
    def contract(self) -> RecursionContract:
        return cast(RecursionContract, self.get("contract"))


class RecursiveRefSchema(CustomSchema[RecursiveRefProps]):
    """Узел, на котором отсечён цикл: проверка по JSON Schema, генерация и ``%`` — через d42.

    Создаётся только через :meth:`RecursionContract.ref`.
    """

    @property
    def name(self) -> str:
        """Имя определения, на которое ссылается узел."""
        return self.props.name

    @property
    def contract(self) -> RecursionContract:
        """Контракт рекурсии модуля."""
        return self.props.contract

    @property
    def target(self) -> GenericSchema:
        """Типизированная d42-схема определения."""
        return self.contract.target(self.name)

    def __represent__(self, visitor: Representor, *, indent: int = 0, **kwargs: Any) -> str:
        return f"recursive_ref({self.name!r})"

    def __generate__(self, visitor: Generator, **kwargs: Any) -> Any:
        return self.contract.generate(self.name, visitor, **kwargs)

    def __validate__(
        self, visitor: Validator, *, value: Any, path: PathHolder, **kwargs: Any
    ) -> ValidationResult:
        if isinstance(visitor, SubstitutorValidator):
            # Подстановка частичная: неуказанные ключи допустимы, как у словаря d42.
            return self.target.__accept__(visitor, value=value, path=path, **kwargs)
        result = visitor.make_validation_result()
        for error in self.contract.errors(self.name, value):
            nested = deepcopy(path)
            for part in error.absolute_path:
                nested = nested[part]
            result.add_error(
                RecursiveRefValidationError(nested, error.instance, self.name, error.message)
            )
        return result

    def __substitute__(self, visitor: Substitutor, *, value: Any, **kwargs: Any) -> GenericSchema:
        return self.target.__accept__(visitor, value=value, **kwargs)


class RecursiveRefValidationError(ValidationError):
    """Значение отсечённого узла не соответствует JSON Schema своего определения."""

    def __init__(self, path: PathHolder, actual_value: Any, definition: str, message: str) -> None:
        self.path = path
        self.actual_value = actual_value
        self.definition = definition
        self.message = message

    def format(self, formatter: Formatter) -> str:
        format_path = getattr(formatter, "_format_path", None)
        where = format_path(self.path) if callable(format_path) else str(self.path)
        return (
            f"Value at {where} does not match JSON Schema of {self.definition!r}: "
            f"{_shorten(self.message)}"
        )

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}({self.path!r}, {_safe_repr(self.actual_value)}, "
            f"{self.definition!r}, {_shorten(self.message)!r})"
        )


# --------------------------------------------------------------------------------------
# Минимальная генерация
# --------------------------------------------------------------------------------------


def _is_optional(flag: Any) -> bool:
    """Флаг обязательности ключа d42: ``False``, ``True`` или ``optional.absent``."""
    return bool(flag)


def _required_length(props: Any) -> int:
    """Минимальная длина массива, которую допускает схема."""
    if props.len is not Nil:
        return int(props.len)
    if props.min_len is not Nil:
        return int(props.min_len)
    return 0


def _rank(
    schema_: GenericSchema, ranks: Mapping[str, int], memo: dict[int, int | None]
) -> int | None:
    """Наибольший ранг листьев, без которых не собрать значение; ``None`` — не собрать."""
    key = id(schema_)
    if key not in memo:
        memo[key] = _rank_inner(schema_, ranks, memo)
    return memo[key]


def _rank_inner(
    schema_: GenericSchema, ranks: Mapping[str, int], memo: dict[int, int | None]
) -> int | None:
    if isinstance(schema_, RecursiveRefSchema):
        return ranks.get(schema_.name)
    if isinstance(schema_, DictSchema):
        keys = schema_.props.keys
        if keys is Nil:
            return 0
        worst = 0
        for key, (value, is_optional) in keys.items():
            if is_ellipsis(key) or _is_optional(is_optional) or is_ellipsis(value):
                continue
            rank = _rank(value, ranks, memo)
            if rank is None:
                return None
            worst = max(worst, rank)
        return worst
    if isinstance(schema_, ListSchema):
        elements = schema_.props.elements
        if elements is not Nil:
            worst = 0
            for element in elements:
                if is_ellipsis(element):
                    continue
                rank = _rank(element, ranks, memo)
                if rank is None:
                    return None
                worst = max(worst, rank)
            return worst
        items = schema_.props.type
        if items is Nil or _required_length(schema_.props) == 0:
            return 0
        return _rank(items, ranks, memo)
    if isinstance(schema_, AnySchema):
        types = schema_.props.types
        if types is Nil or not types:
            return 0
        candidates = [
            rank for rank in (_rank(item, ranks, memo) for item in types) if rank is not None
        ]
        return min(candidates) if candidates else None
    return 0


def _minimal(
    schema_: GenericSchema, contract: RecursionContract, ranks: Mapping[str, int], *, limit: int
) -> GenericSchema:
    """Схема минимального экземпляра, спускающаяся только к листьям ранга меньше ``limit``."""
    if isinstance(schema_, RecursiveRefSchema):
        return _minimal(schema_.target, contract, ranks, limit=ranks[schema_.name])
    if isinstance(schema_, DictSchema):
        keys = schema_.props.keys
        if keys is Nil:
            return schema.dict
        required: dict[Any, Any] = {
            key: _minimal(value, contract, ranks, limit=limit)
            for key, (value, is_optional) in keys.items()
            if not (is_ellipsis(key) or _is_optional(is_optional) or is_ellipsis(value))
        }
        return schema.dict(required)
    if isinstance(schema_, ListSchema):
        props = schema_.props
        elements = props.elements
        if elements is not Nil:
            minimal_elements = [
                element if is_ellipsis(element) else _minimal(element, contract, ranks, limit=limit)
                for element in elements
            ]
            return type(schema_)(props.update(elements=minimal_elements))
        length = _required_length(props)
        items = props.type
        if items is Nil or length == 0:
            return type(schema_)(props.update(len=length))
        return type(schema_)(
            props.update(type=_minimal(items, contract, ranks, limit=limit), len=length)
        )
    if isinstance(schema_, AnySchema):
        types = schema_.props.types
        if types is Nil or not types:
            return schema_
        memo: dict[int, int | None] = {}
        for variant in types:
            variant_rank = _rank(variant, ranks, memo)
            if variant_rank is not None and variant_rank < limit:
                return _minimal(variant, contract, ranks, limit=limit)
        # Недостижимо: определение получило ранг именно потому, что такой вариант есть.
        raise RecursiveSchemaError("в объединении нет варианта с выходом из цикла")
    return schema_


def _pointer(parts: Any) -> str:
    tokens = [str(item).replace("~", "~0").replace("/", "~1") for item in parts]
    return "/" + "/".join(tokens) if tokens else ""


def _safe_repr(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= MAX_VALUE_REPR else text[: MAX_VALUE_REPR - 1] + "…"


def _shorten(message: str) -> str:
    return message if len(message) <= MAX_VALUE_REPR else message[: MAX_VALUE_REPR - 1] + "…"

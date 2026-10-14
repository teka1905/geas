"""d42-схема значения параметра в том виде, в каком оно уходит в запрос.

Зачем
-----

Значение HTTP-параметра — всегда строка: ``?folderId=42&flag=true``. Типизированная
``schema.int`` такую строку не пропустит, а голая ``schema.str.regex(r"^-?\\d+$")``
неточна в обе стороны: границ ``int64`` она не знает, а ``fake()`` по ней выдаёт
строки из трёх десятков цифр и отрицательные числа там, где контракт их не
допускает.

:func:`serialized` оборачивает типизированную схему значения. Проверяется строка,
а ограничения — границы, ``enum``, длина массива — берутся из вложенной схемы
целиком, без перевода в регулярное выражение.

Семантика
---------

* **Проверка.** Значение — строка, которая разбирается по типу вложенной схемы
  тем же правилом, что и перехваченный запрос
  (:func:`~geas.runtime.validation.decode_scalar`), и результат проходит
  вложенную схему. ``"042"`` для ``integer`` допустимо — так же, как при проверке
  запроса по контракту.
* **Генерация** ``fake()`` — значение вложенной схемы в строковом виде
  (:func:`~geas.runtime.validation.encode_scalar`): ``42`` → ``"42"``,
  ``True`` → ``"true"``.
* **Подстановка** ``%`` строкой закрепляет точный вид на проводе и возвращает
  ``schema.str(...)``. Подстановка типизированным значением закрепляет само
  значение: ``serialized(schema.int) % 42`` примет и ``"42"``, и ``"042"``, а
  ``fake()`` выдаст ``"42"``. Так ``% 1.0`` для ``number`` не разойдётся с
  клиентом, который пишет единицу как ``"1"``.
* **Массив одной строкой** (``delimiter``) разбирается по разделителю, каждый
  элемент — по типу элементов вложенного ``schema.list``. Пустой строке
  соответствует один пустой элемент, а не пустой массив — как и при разборе
  запроса, поэтому ``% []`` отклоняется.
* **Место запроса** (``location``) добавляет ограничение самого текста, которое
  накладывает транспорт: сегмент path не бывает пустым и не содержит ``/``, у значения заголовка и
  cookie нет пробелов по краям (HTTP их срезает), в cookie нет ``;`` (им
  разделяются пары). Разобранный запрос таких значений не содержит, поэтому
  проверка не становится уже контракта, а ``fake()`` не выдаёт значение, которое
  транспорт изменил бы по дороге.

``format`` (``date-time``, ``uuid``) здесь, как и везде на d42-пути, не
проверяется и не генерируется: его держит JSON Schema контракта (см. шапку
:mod:`~geas.integrations.d42.converter`).
"""

from __future__ import annotations

import math
from typing import Any, cast

from d42.custom_type import CustomSchema, Props
from d42.declaration import GenericSchema, schema
from d42.declaration.types import (
    AnySchema,
    BoolSchema,
    FloatSchema,
    IntSchema,
    ListSchema,
    NoneSchema,
    Schema,
    StrSchema,
)
from d42.generation import Generator
from d42.representation import Representor
from d42.substitution import Substitutor, SubstitutorValidator
from d42.substitution.errors import make_substitution_error
from d42.utils import is_ellipsis
from d42.validation import Formatter, ValidationResult, Validator
from d42.validation.errors import TypeValidationError, ValidationError
from niltype import Nil
from th import PathHolder

from geas.errors import ValidationFailedError
from geas.runtime.validation import decode_scalar, encode_scalar, safe_repr

__all__ = [
    "SerializedSchema",
    "SerializedValueValidationError",
    "serialized",
]

#: Сколько раз ``fake()`` перебирает варианты, прежде чем сдаться. Элемент-строка
#: может содержать разделитель, а строка — пробел по краю (он есть в алфавите d42):
#: такой вариант транспорт изменил бы, и он отбрасывается.
_GENERATION_ATTEMPTS = 100

#: Места запроса, которые ограничивают сам текст значения, и описание ограничения.
_LOCATION_TEXT = {
    "path": "a non-empty path segment without '/'",
    "header": "a header value without leading or trailing whitespace",
    "cookie": "a cookie value without leading or trailing whitespace and without ';'",
}

#: Ожидаемая запись значения — для сообщений об ошибке в стиле d42.
_EXPECTED_TEXT = {
    "integer": "an integer in decimal notation",
    "number": "a number",
    "boolean": "'true' or 'false'",
    "string": "a string",
}


class SerializedProps(Props):
    """``props`` схемы: вложенная типизированная схема и разделитель массива."""

    @property
    def type(self) -> GenericSchema:
        return cast(GenericSchema, self.get("type"))

    @property
    def delimiter(self) -> str | None:
        value = self.get("delimiter")
        return None if value is Nil else cast(str, value)

    @property
    def location(self) -> str | None:
        value = self.get("location")
        return None if value is Nil else cast(str, value)


class SerializedSchema(CustomSchema[SerializedProps]):
    """Строка параметра, которая разбирается в значение вложенной схемы.

    Создаётся через :func:`serialized`.
    """

    @property
    def inner(self) -> GenericSchema:
        """Типизированная схема разобранного значения."""
        return self.props.type

    @property
    def delimiter(self) -> str | None:
        """Разделитель элементов массива, сериализованного одной строкой."""
        return self.props.delimiter

    @property
    def location(self) -> str | None:
        """Место запроса, если оно ограничивает сам текст значения."""
        return self.props.location

    # ------------------------------------------------------------- протокол d42

    def __represent__(self, visitor: Representor, *, indent: int = 0, **kwargs: Any) -> str:
        arguments = [self.inner.__accept__(visitor, indent=indent, **kwargs)]
        if self.delimiter is not None:
            arguments.append(f"delimiter={self.delimiter!r}")
        if self.location is not None:
            arguments.append(f"location={self.location!r}")
        return f"serialized({', '.join(arguments)})"

    def __generate__(self, visitor: Generator, **kwargs: Any) -> str:
        if self.delimiter is None and self.location is None:
            return self._encode(self.inner.__accept__(visitor, **kwargs))
        validator = Validator()
        for _ in range(_GENERATION_ATTEMPTS):
            text = self._encode(self.inner.__accept__(visitor, **kwargs))
            if not self.__accept__(validator, value=text).has_errors():
                return text
        raise ValidationFailedError(
            f"fake() не нашёл значение {self!r} за {_GENERATION_ATTEMPTS} попыток: "
            f"сгенерированная строка либо разбирается в другой массив, либо нарушает "
            f"ограничение места запроса. Задайте значение через % или overlay'ем",
            validator="d42",
        )

    def __validate__(
        self, visitor: Validator, *, value: Any, path: PathHolder, **kwargs: Any
    ) -> ValidationResult:
        result = visitor.make_validation_result()
        if isinstance(visitor, SubstitutorValidator) and not isinstance(value, str):
            # ``%`` принимает и значение, и его строковый вид: см. шапку модуля.
            try:
                typed = self._typed(value)
            except _TypeMismatchError as error:
                return result.add_error(TypeValidationError(path, error.value, error.expected))
            except _UnwritableValueError as error:
                return result.add_error(SerializedValueValidationError(path, value, error.expected))
            return self.inner.__accept__(visitor, value=typed, path=path, **kwargs)
        if not isinstance(value, str):
            return result.add_error(TypeValidationError(path, value, str))
        if self.location is not None and not _fits_location(value, self.location):
            return result.add_error(
                SerializedValueValidationError(path, value, _LOCATION_TEXT[self.location])
            )
        try:
            decoded = self._decode(value)
        except ValueError:
            return result.add_error(
                SerializedValueValidationError(path, value, self._expected_text())
            )
        return self.inner.__accept__(visitor, value=decoded, path=path, **kwargs)

    def __substitute__(self, visitor: Substitutor, *, value: Any, **kwargs: Any) -> GenericSchema:
        result = self.__accept__(visitor.validator, value=value)
        if result.has_errors():
            raise make_substitution_error(result, visitor.formatter)
        if isinstance(value, str):
            return schema.str(value)
        substituted = self.inner.__accept__(visitor, value=self._typed(value), **kwargs)
        return type(self)(self.props.update(type=substituted))

    # --------------------------------------------------------------- кодек

    def _item_kind(self) -> str:
        if self.delimiter is None:
            return value_kind(self.inner)
        return _list_item_kind(cast(ListSchema, self.inner))

    def _expected_text(self) -> str:
        expected = _EXPECTED_TEXT[self._item_kind()]
        if self.delimiter is None:
            return expected
        return f"items of {expected} separated by {self.delimiter!r}"

    def _decode(self, text: str) -> Any:
        kind = self._item_kind()
        if self.delimiter is None:
            return decode_scalar(text, kind)
        return [decode_scalar(item, kind) for item in text.split(self.delimiter)]

    def _encode(self, value: Any) -> str:
        if self.delimiter is None:
            return encode_scalar(value)
        return self.delimiter.join(encode_scalar(item) for item in value)

    def _typed(self, value: Any) -> Any:
        """Проверить тип подставленного значения и привести ``int`` к ``float`` у ``number``."""
        kind = self._item_kind()
        if self.delimiter is None:
            typed = _typed_scalar(value, kind)
        else:
            if not isinstance(value, list):
                raise _TypeMismatchError(value, list)
            if not value:
                raise _UnwritableValueError(
                    "a non-empty list: an empty one has no single-string form"
                )
            typed = [_typed_scalar(item, kind) for item in value]
        if self.location is not None and not _fits_location(self._encode(typed), self.location):
            raise _UnwritableValueError(_LOCATION_TEXT[self.location])
        return typed


class SerializedValueValidationError(ValidationError):
    """Значение не записывается строкой параметра.

    Строка не разбирается по типу вложенной схемы либо её изменил бы транспорт.
    """

    def __init__(self, path: PathHolder, actual_value: Any, expected: str) -> None:
        self.path = path
        self.actual_value = actual_value
        self.expected = expected

    def format(self, formatter: Formatter) -> str:
        format_path = getattr(formatter, "_at_path", None)
        where = format_path(self.path) if callable(format_path) else f" at {self.path}"
        return (
            f"Value {type(self.actual_value)}{where} must be a request parameter written as "
            f"{self.expected}, but {safe_repr(self.actual_value)} given"
        )

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}({self.path!r}, {safe_repr(self.actual_value)}, "
            f"{self.expected!r})"
        )


def serialized(
    inner: GenericSchema, *, delimiter: str | None = None, location: str | None = None
) -> SerializedSchema:
    """Строковый вид значения параметра поверх его типизированной схемы.

    ``inner`` — схема разобранного значения: ``schema.int``, ``schema.float``,
    ``schema.bool``, ``schema.str`` или объединение литералов одного из этих типов.
    С ``delimiter`` — ``schema.list`` таких элементов: массив, сериализованный одной
    строкой (``1,2,3``). ``location`` (``"path"``, ``"header"``, ``"cookie"``) —
    ограничение самого текста, которое накладывает транспорт (см. шапку модуля).

    :raises TypeError: у вложенной схемы нет однозначного строкового вида.
    """
    if not isinstance(inner, Schema):
        raise TypeError(f"inner должен быть схемой d42, получено {type(inner).__name__}")
    props: dict[str, Any] = {"type": inner}
    if location is not None:
        if location not in _LOCATION_TEXT:
            raise TypeError(
                f"location {location!r} не ограничивает текст значения; "
                f"допустимы {sorted(_LOCATION_TEXT)}"
            )
        props["location"] = location
    if delimiter is None:
        value_kind(inner)
        return SerializedSchema(SerializedProps(props))
    if not isinstance(delimiter, str) or not delimiter:
        raise TypeError(f"delimiter должен быть непустой строкой, получено {delimiter!r}")
    if not isinstance(inner, ListSchema):
        raise TypeError(
            f"с delimiter вложенная схема должна быть schema.list(<элемент>), "
            f"получено {type(inner).__name__}"
        )
    _list_item_kind(inner)
    return SerializedSchema(SerializedProps({**props, "delimiter": delimiter}))


def value_kind(inner: GenericSchema) -> str:
    """Тип JSON Schema, по которому разбирается строковый вид значения ``inner``.

    :raises TypeError: схема не скалярная или объединяет разные типы.
    """
    if isinstance(inner, AnySchema) and inner.props.types is not Nil:
        if any(isinstance(item, NoneSchema) for item in inner.props.types):
            raise TypeError(
                f"у null в {inner!r} нет строкового вида параметра: пустая строка "
                f'разбирается как null только у строк — добавьте schema.str("")'
            )
        kinds = {value_kind(item) for item in inner.props.types}
        if kinds == {"number", "integer"}:
            # Так d42 описывает JSON Schema ``number``: дробная ветка и целая.
            return "number"
        if len(kinds) == 1:
            return kinds.pop()
        raise TypeError(
            f"объединение {inner!r} не имеет однозначного строкового вида: "
            f"варианты разных типов ({sorted(kinds) or 'нет вариантов'})"
        )
    for schema_type, kind in (
        (BoolSchema, "boolean"),
        (IntSchema, "integer"),
        (FloatSchema, "number"),
        (StrSchema, "string"),
    ):
        if isinstance(inner, schema_type):
            return kind
    raise TypeError(
        f"у {type(inner).__name__} нет строкового вида параметра: поддержаны schema.int, "
        f"schema.float, schema.bool, schema.str и объединения их литералов"
    )


def _list_item_kind(inner: ListSchema) -> str:
    """Тип элементов списка: из ``schema.list(<элемент>)`` или из закреплённых элементов.

    После ``%`` d42 хранит список не типом элемента, а перечнем элементов
    (``schema.list([schema.int(1), schema.int(2)])``).
    """
    if inner.props.type is not Nil:
        return value_kind(inner.props.type)
    elements = inner.props.elements
    items = [] if elements is Nil else [item for item in elements if not is_ellipsis(item)]
    if not items:
        raise TypeError(
            "с delimiter вложенная схема должна быть schema.list(<элемент>): без типа "
            "элементов строку не разобрать"
        )
    kinds = {value_kind(item) for item in items}
    if len(kinds) != 1:
        raise TypeError(f"элементы списка разных типов ({sorted(kinds)}): строку не разобрать")
    return kinds.pop()


def _fits_location(text: str, location: str) -> bool:
    """Переживёт ли текст транспорт места запроса без изменений."""
    if location == "path":
        # Записанный путь уже раскодирован: %2F стал разделителем, и значения со
        # слэшем внутри одного сегмента в перехваченном запросе не бывает.
        return text != "" and "/" not in text
    if text != text.strip():
        return False
    return location != "cookie" or ";" not in text


class _UnwritableValueError(Exception):
    """У подставленного значения нет строкового вида, который донёс бы транспорт."""

    def __init__(self, expected: str) -> None:
        super().__init__(expected)
        self.expected = expected


class _TypeMismatchError(Exception):
    """Подставленное значение не того типа, что вложенная схема."""

    def __init__(self, value: Any, expected: type[Any]) -> None:
        super().__init__(value, expected)
        self.value = value
        self.expected = expected


def _typed_scalar(value: Any, kind: str) -> Any:
    # ``bool`` — подкласс ``int``, а d42 проверяет тип через isinstance: без явной
    # проверки ``True`` прошёл бы как целое и ушёл бы в запрос строкой "true".
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _TypeMismatchError(value, int)
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _TypeMismatchError(value, float)
        if not math.isfinite(value):
            raise _UnwritableValueError("a finite number")
        return float(value)
    if kind == "boolean":
        if not isinstance(value, bool):
            raise _TypeMismatchError(value, bool)
        return value
    if not isinstance(value, str):
        raise _TypeMismatchError(value, str)
    return value

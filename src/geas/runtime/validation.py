"""Валидация по JSON Schema и разбор сериализованных параметров.

Это **независимый от d42** путь проверки. Он работает всегда — и когда extra
``[d42]`` не установлен, и рядом с d42-проверкой. Именно он держит точную
семантику ``oneOf``, ``discriminator``, ``format`` и границ.

Сеть не используется: валидатор получает пустой :class:`referencing.Registry`,
чей ``retrieve`` всегда бросает исключение. Любая попытка внешнего разрешения
``$ref`` превращается в ошибку контракта, а не в HTTP-запрос.
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from ..errors import RefResolutionError, RequestContractError, ValidationFailedError
from ..models import Direction, ParameterLocation

__all__ = [
    "ARRAY_DELIMITERS",
    "MAX_VALUE_REPR",
    "decode_parameter",
    "validate_instance",
]

#: Разделители для массивов, сериализованных одной строкой.
ARRAY_DELIMITERS: dict[str, str] = {
    "form": ",",
    "simple": ",",
    "spaceDelimited": " ",
    "pipeDelimited": "|",
}

#: Длина безопасного представления фактического значения в тексте ошибки.
MAX_VALUE_REPR = 200

_INTEGER = re.compile(r"^-?\d+$")
_NUMBER = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _no_network(uri: str) -> Any:
    raise RefResolutionError(
        f"схема пытается разрешить внешний ресурс {uri!r}. Библиотека никогда не ходит в сеть; "
        f"все определения обязаны лежать в $defs самого документа"
    )


# ``retrieve`` задаётся явно, хотя у ``referencing`` и дефолт бросает исключение:
# так гарантия «никогда не ходим в сеть» видна прямо здесь и не зависит от того,
# что решит библиотека в следующей версии. mypy не видит kwarg, потому что attrs
# генерирует ``__init__`` с приватным именем поля ``_retrieve``.
_EMPTY_REGISTRY: Registry[Any] = Registry(retrieve=_no_network)  # type: ignore[call-arg]


def _safe_repr(value: Any) -> str:
    text = repr(value)
    if len(text) > MAX_VALUE_REPR:
        return text[: MAX_VALUE_REPR - 1] + "…"
    return text


def _pointer(parts: Any) -> str:
    tokens = [str(item).replace("~", "~0").replace("/", "~1") for item in parts]
    return "/" + "/".join(tokens) if tokens else ""


def validate_instance(
    schema: dict[str, Any],
    instance: Any,
    *,
    operation_key: str,
    direction: Direction,
    part: str,
    request_index: int | None = None,
) -> None:
    """Проверить значение по JSON Schema и превратить первую ошибку в ошибку контракта.

    Ошибка несёт operation key, направление, часть запроса, JSON Pointer внутри
    значения, указатель в схеме, нарушенное правило и безопасное представление
    фактического значения.
    """
    validator = Draft202012Validator(
        schema,
        registry=_EMPTY_REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    try:
        errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    except Exception as exc:
        # ``jsonschema`` заворачивает ошибки разрешения ссылок в собственный тип,
        # и наружу вылезало бы чужое исключение вместо понятного объяснения.
        unresolvable = _find_unresolvable(exc)
        if unresolvable is None:
            raise
        raise RefResolutionError(
            f"схема ссылается на {unresolvable!r}, чего нет в её $defs. "
            f"Библиотека никогда не ходит в сеть: все определения обязаны лежать "
            f"внутри самого документа",
            operation_key=operation_key,
            direction=direction.value,
        ) from exc
    if not errors:
        return
    error = errors[0]
    _raise(
        error,
        operation_key=operation_key,
        direction=direction,
        part=part,
        request_index=request_index,
    )


def _find_unresolvable(error: BaseException) -> str | None:
    """Найти в цепочке причин ошибку неразрешимой ссылки и вернуть её URI."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, Unresolvable):
            return str(getattr(current, "ref", current))
        if isinstance(current, RefResolutionError):
            return current.base_message
        current = current.__cause__ or current.__context__
    return None


def _raise(
    error: ValidationError,
    *,
    operation_key: str,
    direction: Direction,
    part: str,
    request_index: int | None,
) -> None:
    message = f"{part}: {error.message}"
    kwargs: dict[str, Any] = {
        "operation_key": operation_key,
        "json_pointer": _pointer(error.absolute_path) or "/",
        "schema_pointer": _pointer(error.absolute_schema_path) or "/",
        "validator": str(error.validator),
        "expected": _safe_repr(error.validator_value),
        "actual": _safe_repr(error.instance),
    }
    if direction is Direction.REQUEST:
        raise RequestContractError(message, request_index=request_index, **kwargs)
    from ..errors import ResponseContractError

    raise ResponseContractError(message, **kwargs)


def decode_parameter(
    *,
    name: str,
    location: ParameterLocation,
    style: str,
    explode: bool,
    schema: dict[str, Any],
    values: list[str],
    operation_key: str,
    request_index: int | None = None,
) -> Any:
    """Восстановить типизированное значение параметра из его строкового вида.

    Разбор идёт строго по объявленным ``style``/``explode``. Значение, которое
    не приводится к объявленному типу, — это ошибка контракта, а не «ну ладно,
    оставим строкой».
    """
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), None)

    if kind == "array":
        items = schema.get("items") or {}
        if explode:
            raw_items = list(values)
        else:
            delimiter = ARRAY_DELIMITERS.get(style, ",")
            raw_items = [] if not values else values[0].split(delimiter)
            if len(values) > 1:
                raise RequestContractError(
                    f"параметр {name!r} объявлен с explode=false, но пришёл несколько раз "
                    f"({len(values)} значений)",
                    request_index=request_index,
                    operation_key=operation_key,
                    json_pointer=f"/{name}",
                    expected=f"одно значение, элементы через {ARRAY_DELIMITERS.get(style, ',')!r}",
                    actual=_safe_repr(values),
                )
        return [
            _coerce(
                item,
                items,
                name=name,
                location=location,
                operation_key=operation_key,
                request_index=request_index,
                index=position,
            )
            for position, item in enumerate(raw_items)
        ]

    if len(values) > 1:
        raise RequestContractError(
            f"скалярный параметр {name!r} пришёл {len(values)} раз",
            request_index=request_index,
            operation_key=operation_key,
            json_pointer=f"/{name}",
            expected="одно значение",
            actual=_safe_repr(values),
        )
    return _coerce(
        values[0],
        schema,
        name=name,
        location=location,
        operation_key=operation_key,
        request_index=request_index,
        index=None,
    )


def _coerce(
    text: str,
    schema: dict[str, Any],
    *,
    name: str,
    location: ParameterLocation,
    operation_key: str,
    request_index: int | None,
    index: int | None,
) -> Any:
    kind = schema.get("type")
    nullable = False
    if isinstance(kind, list):
        nullable = "null" in kind
        kind = next((item for item in kind if item != "null"), None)

    pointer = f"/{name}" if index is None else f"/{name}/{index}"

    def fail(expected: str) -> None:
        raise RequestContractError(
            f"{location.value}-параметр {name!r}: значение не приводится к типу {kind!r}",
            request_index=request_index,
            operation_key=operation_key,
            json_pointer=pointer,
            expected=expected,
            actual=_safe_repr(text),
        )

    if kind == "integer":
        if not _INTEGER.match(text):
            fail("целое число в десятичной записи")
        return int(text)
    if kind == "number":
        if not _NUMBER.match(text):
            fail("число")
        return float(text)
    if kind == "boolean":
        if text not in ("true", "false"):
            fail("'true' или 'false'")
        return text == "true"
    if kind == "string" or kind is None:
        if nullable and text == "":
            return None
        return text
    fail(f"скалярный тип, а не {kind!r}")
    raise AssertionError("unreachable")


def wrap_validation_error(
    error: ValidationFailedError, *, operation_key: str
) -> ValidationFailedError:
    """Дополнить ошибку ключом операции, если он ещё не проставлен."""
    if error.operation_key is None:
        error.operation_key = operation_key
    return error

"""Публичный ``OperationHandle`` — то, чем пользуется тест.

Handle иммутабелен, типизирован и не тянет за собой ни d42, ни JJ: импорт
generated-реестра работает на голом ядре. Опциональные интеграции подключаются
лениво, и если extra не установлен, ошибка называет точную команду установки.

Кроме самого контракта, выбранное тело запроса и выбранный вариант ответа знают
свои координаты в generated-артефактах: файл нормализованного контракта,
JSON Pointer внутри него, d42-модуль, имя d42-схемы и путь к её исходнику.
Благодаря этому от alias-файла в проекте до generated-схемы один шаг —
``view.describe()`` или ``geas show``, а не поиск по каталогу.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from ..errors import (
    MissingExtraError,
    OperationLookupError,
    RequestContractError,
    ResponseVariantError,
)
from ..media import (
    DEFAULT_TEXT_CHARSET,
    MediaKind,
    declared_charset,
    media_kind,
    media_priority,
    media_range_specificity,
)
from ..models import Direction, ParameterLocation, UncheckedVariant
from .description import DEFAULT_MAX_DEPTH, describe_contract, describe_parameters
from .validation import decode_parameter, validate_instance, wire_text

__all__ = [
    "OperationHandle",
    "ParameterSetView",
    "ParameterView",
    "RequestBodyView",
    "RequestView",
    "ResponseView",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class ParameterView:
    """Контракт одного параметра или заголовка."""

    name: str
    location: ParameterLocation
    required: bool
    style: str
    explode: bool
    json_schema: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True, slots=True)
class ParameterSetView:
    """Параметры одного места запроса (path, query, header, cookie) и их d42-схема.

    d42-схема места — словарь «имя параметра → значение в том виде, в каком оно
    уходит в запрос»: строки, числа и флаги строками, повторяющийся ключ — списком.
    Про необязательные координаты артефактов — см. :class:`RequestBodyView`.
    """

    location: ParameterLocation
    parameters: tuple[ParameterView, ...]
    #: Имя переменной generated d42-схемы места (``None``, если схемы нет).
    d42_export: str | None = None
    #: Почему d42-схемы места нет; ``None``, если она есть.
    d42_reason: str | None = None
    #: Стабильный ключ операции — нужен описанию, чтобы назвать себя.
    operation_key: str | None = None
    #: Файл нормализованного контракта (``contracts/<slug>.json``).
    contract_file: Path | None = None
    #: Полное имя generated d42-модуля параметров запроса.
    d42_module: str | None = None
    #: Файл этого модуля — определяется без импорта d42.
    d42_source_path: Path | None = None

    @property
    def contract_path(self) -> str | None:
        """Файл контракта вместе с JSON Pointer списка параметров запроса."""
        return _contract_path(self.contract_file, "/request/parameters")

    @property
    def d42_reference(self) -> str | None:
        """Ссылка ``модуль:переменная`` на generated d42-схему места."""
        return _d42_reference(self.d42_module, self.d42_export)

    def describe(self, *, max_depth: int = DEFAULT_MAX_DEPTH) -> str:
        """Человекочитаемое описание параметров места. Ничего не печатает — возвращает строку."""
        return describe_parameters(
            operation_key=self.operation_key,
            location=self.location,
            parameters=[
                (item.name, item.required, item.style, item.explode, item.json_schema)
                for item in self.parameters
            ],
            contract_path=self.contract_path,
            d42_reference=self.d42_reference,
            d42_reason=self.d42_reason,
            d42_source_path=self.d42_source_path,
            max_depth=max_depth,
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class RequestBodyView:
    """Контракт тела запроса для одного content type.

    Координаты артефактов (``contract_file`` и далее) необязательны: view можно
    собрать и напрямую — так делают инструменты и тесты, — тогда координат
    просто нет. Отсутствие значения — всегда ``None``, а не пустая строка:
    пустая строка выглядела бы как настоящий, но пустой путь.
    """

    content_type: str
    required: bool
    json_schema: Mapping[str, Any]
    #: Имя переменной generated d42-схемы (``None``, если d42 для операции выключен).
    d42_export: str | None
    #: Стабильный ключ операции — нужен описанию, чтобы назвать себя.
    operation_key: str | None = None
    #: Файл нормализованного контракта (``contracts/<slug>.json``).
    contract_file: Path | None = None
    #: JSON Pointer этого тела внутри файла контракта (RFC 6901).
    json_pointer: str | None = None
    #: Полное имя generated d42-модуля направления.
    d42_module: str | None = None
    #: Файл этого модуля. Определяется по раскладке артефактов, **без** импорта:
    #: описание обязано работать и без установленного extra ``[d42]``.
    d42_source_path: Path | None = None

    @property
    def contract_path(self) -> str | None:
        """Файл контракта вместе с JSON Pointer: ``/путь/файл.json#/request/bodies/0/schema``."""
        return _contract_path(self.contract_file, self.json_pointer)

    @property
    def d42_reference(self) -> str | None:
        """Ссылка ``модуль:переменная`` на generated d42-схему."""
        return _d42_reference(self.d42_module, self.d42_export)

    def describe(self, *, max_depth: int = DEFAULT_MAX_DEPTH) -> str:
        """Человекочитаемое описание тела запроса. Ничего не печатает — возвращает строку."""
        return describe_contract(
            operation_key=self.operation_key,
            direction=Direction.REQUEST,
            content_type=self.content_type,
            required=self.required,
            json_schema=self.json_schema,
            contract_path=self.contract_path,
            d42_reference=self.d42_reference,
            d42_source_path=self.d42_source_path,
            max_depth=max_depth,
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class ResponseView:
    """Контракт одного варианта ответа.

    Про необязательные координаты артефактов — см. :class:`RequestBodyView`.
    """

    status: int | str
    content_type: str | None
    #: ``None`` означает ответ без тела.
    json_schema: Mapping[str, Any] | None
    headers: tuple[ParameterView, ...]
    d42_export: str | None
    #: Стабильный ключ операции — нужен описанию, чтобы назвать себя.
    operation_key: str | None = None
    #: Файл нормализованного контракта (``contracts/<slug>.json``).
    contract_file: Path | None = None
    #: JSON Pointer этого варианта внутри файла контракта (RFC 6901).
    json_pointer: str | None = None
    #: Полное имя generated d42-модуля направления.
    d42_module: str | None = None
    #: Файл этого модуля — определяется без импорта d42.
    d42_source_path: Path | None = None

    def label(self) -> str:
        """Ярлык варианта для сообщений."""
        return f"{self.status}:{self.content_type or '-'}"

    @property
    def contract_path(self) -> str | None:
        """Файл контракта вместе с JSON Pointer: ``/путь/файл.json#/responses/0/schema``."""
        return _contract_path(self.contract_file, self.json_pointer)

    @property
    def d42_reference(self) -> str | None:
        """Ссылка ``модуль:переменная`` на generated d42-схему."""
        return _d42_reference(self.d42_module, self.d42_export)

    def describe(self, *, max_depth: int = DEFAULT_MAX_DEPTH) -> str:
        """Человекочитаемое описание варианта ответа. Ничего не печатает — возвращает строку."""
        return describe_contract(
            operation_key=self.operation_key,
            direction=Direction.RESPONSE,
            status=self.status,
            content_type=self.content_type,
            json_schema=self.json_schema,
            contract_path=self.contract_path,
            d42_reference=self.d42_reference,
            d42_source_path=self.d42_source_path,
            max_depth=max_depth,
        )


_Variant = TypeVar("_Variant", "RequestBodyView", "ResponseView")


def _one_schema_variant(variants: Sequence[_Variant]) -> _Variant | None:
    """Главный из JSON-вариантов, описанных одной и той же схемой; иначе ``None``.

    ``application/json`` и ``text/json`` (умолчание Swashbuckle) или ``application/json``
    и ``*/*`` с одной схемой — это один вариант в двух написаниях: тело проверяется и
    разбирается клиентом одинаково, поэтому выбор главного
    (:func:`~geas.media.media_priority`) не «молча первый». Варианты с разными схемами
    так не выбираются никогда, как и текстовые: ``text/plain`` и ``text/html`` или
    JSON-строка и текст уходят на провод по-разному.
    """
    if len(variants) < 2:
        return None
    first = variants[0].json_schema
    if first is None:
        return None
    for item in variants:
        if item.content_type is None or item.json_schema != first:
            return None
        if media_kind(item.content_type) is not MediaKind.JSON:
            return None
    return min(variants, key=lambda item: media_priority(item.content_type or ""))


def _contract_path(contract_file: Path | None, json_pointer: str | None) -> str | None:
    """Адрес схемы: путь к файлу контракта и JSON Pointer внутри него."""
    if contract_file is None:
        return None
    if not json_pointer:
        return str(contract_file)
    return f"{contract_file}#{json_pointer}"


def _d42_reference(module: str | None, export: str | None) -> str | None:
    """Ссылка на generated d42-схему или ``None``, если её нет.

    Половины ссылки недостаточно: без имени переменной по модулю всё равно
    ничего не импортировать, поэтому неполная пара — это отсутствие ссылки.
    """
    if module is None or export is None:
        return None
    return f"{module}:{export}"


@dataclass(frozen=True, kw_only=True, slots=True)
class RequestView:
    """Контракт запроса целиком."""

    path: tuple[ParameterView, ...] = ()
    query: tuple[ParameterView, ...] = ()
    header: tuple[ParameterView, ...] = ()
    cookie: tuple[ParameterView, ...] = ()
    bodies: tuple[RequestBodyView, ...] = ()
    #: Места, у которых есть параметры, в порядке path → query → header → cookie.
    #: Если не переданы, собираются из параметров — без координат d42.
    parameter_sets: tuple[ParameterSetView, ...] = ()
    #: Тела запроса, которые спецификация объявила, а контракт не проверяет.
    unchecked_bodies: tuple[UncheckedVariant, ...] = ()

    def __post_init__(self) -> None:
        by_location = {
            ParameterLocation.PATH: self.path,
            ParameterLocation.QUERY: self.query,
            ParameterLocation.HEADER: self.header,
            ParameterLocation.COOKIE: self.cookie,
        }
        for location, items in by_location.items():
            misplaced = sorted(item.name for item in items if item.location is not location)
            if misplaced:
                raise ValueError(f"в {location.value} лежат параметры другого места: {misplaced}")
        expected = {location: items for location, items in by_location.items() if items}
        if not self.parameter_sets:
            derived = tuple(
                ParameterSetView(location=location, parameters=items)
                for location, items in expected.items()
            )
            object.__setattr__(self, "parameter_sets", derived)
            return
        actual: dict[ParameterLocation, tuple[ParameterView, ...]] = {}
        for item in self.parameter_sets:
            if item.location in actual:
                raise ValueError(f"место {item.location.value} в parameter_sets встречается дважды")
            actual[item.location] = item.parameters
        if actual != expected:
            raise ValueError(
                "parameter_sets расходятся с path/query/header/cookie: места "
                f"{sorted(location.value for location in actual)} против "
                f"{sorted(location.value for location in expected)} либо другой состав параметров"
            )

    def parameters(self) -> tuple[ParameterView, ...]:
        """Все параметры в стабильном порядке."""
        return self.path + self.query + self.header + self.cookie

    def parameter_set(self, location: ParameterLocation | str) -> ParameterSetView:
        """Параметры одного места запроса вместе с координатами их d42-схемы."""
        parsed = _parse_location(location)
        for item in self.parameter_sets:
            if item.location is parsed:
                return item
        declared = [item.location.value for item in self.parameter_sets]
        raise OperationLookupError(
            f"у операции нет {parsed.value}-параметров; параметры описаны в {declared or 'нигде'}"
        )

    def body(self, content_type: str | None = None) -> RequestBodyView:
        """Контракт тела запроса.

        Если тело одно, ``content_type`` можно не передавать. Если их несколько,
        выбор обязателен: молча брать первый нельзя.
        """
        if content_type is None:
            if len(self.bodies) == 1:
                return self.bodies[0]
            same = _one_schema_variant(self.bodies)
            if same is not None:
                return same
            if not self.bodies:
                if self.unchecked_bodies:
                    raise ResponseVariantError(
                        "у операции нет проверяемого тела запроса" + self._unchecked_note()
                    )
                raise ResponseVariantError("у операции нет тела запроса")
            available = sorted(item.content_type for item in self.bodies)
            raise ResponseVariantError(
                f"у операции несколько тел запроса ({available}); укажите content_type явно"
                + self._unchecked_note()
            )
        available = sorted(item.content_type for item in self.bodies)
        match = self.match(content_type)
        if isinstance(match, RequestBodyView):
            return match
        if match is not None:
            raise ResponseVariantError(
                f"тело запроса {content_type!r} объявлено спецификацией "
                f"({match.content_type}), но не проверяется: {match.reason}; проверяемые "
                f"тела: {available or 'нет'}"
            )
        raise ResponseVariantError(
            f"тела запроса с content type {content_type!r} нет; доступны {available}"
            + self._unchecked_note()
        )

    def match(self, content_type: str) -> RequestBodyView | UncheckedVariant | None:
        """Объявленное тело, которое покрывает content type, или ``None``.

        Спецификация может объявить диапазон (``*/*``, ``image/*``), а клиент шлёт
        конкретный тип. Побеждает самое точное совпадение: тип целиком, затем
        ``type/*``, затем ``*/*``. Непроверяемое тело возвращается как
        :class:`~geas.models.UncheckedVariant`.
        """
        best: RequestBodyView | UncheckedVariant | None = None
        best_score = -1
        candidates: list[RequestBodyView | UncheckedVariant] = [
            *self.bodies,
            *self.unchecked_bodies,
        ]
        for item in candidates:
            score = media_range_specificity(item.content_type, content_type)
            if score is not None and score > best_score:
                best, best_score = item, score
        return best

    def _unchecked_note(self) -> str:
        """Хвост сообщения о телах, которые объявлены, но не проверяются."""
        if not self.unchecked_bodies:
            return ""
        declared = ", ".join(
            f"{item.content_type} ({item.reason})" for item in self.unchecked_bodies
        )
        return f"; объявлено без проверки: {declared}"


class OperationHandle:
    """Иммутабельная ручка одной операции контракта."""

    __slots__ = (
        "_d42_modules",
        "_d42_reason",
        "_d42_reasons",
        "_key",
        "_method",
        "_operation_id",
        "_path",
        "_python_path",
        "_request",
        "_responses",
        "_source",
        "_unchecked",
        "_unsupported",
    )

    def __init__(
        self,
        *,
        key: str,
        operation_id: str,
        method: str,
        path: str,
        source: str,
        python_path: tuple[str, ...],
        request: RequestView,
        responses: tuple[ResponseView, ...],
        unsupported: tuple[str, ...] = (),
        unchecked: tuple[UncheckedVariant, ...] = (),
        d42_modules: Mapping[str, str | None] | None = None,
        d42_reason: str | None = None,
        d42_reasons: Mapping[str, str | None] | None = None,
    ) -> None:
        self._key = key
        self._operation_id = operation_id
        self._method = method
        self._path = path
        self._source = source
        self._python_path = python_path
        self._request = request
        self._responses = responses
        self._unsupported = unsupported
        self._unchecked = unchecked
        self._d42_modules = dict(d42_modules or {})
        self._d42_reason = d42_reason
        self._d42_reasons = dict(d42_reasons or {})

    # ------------------------------------------------------------ свойства

    @property
    def key(self) -> str:
        """Стабильный ключ операции из manifest."""
        return self._key

    @property
    def operation_id(self) -> str:
        """``operationId`` из спецификации."""
        return self._operation_id

    @property
    def method(self) -> str:
        """HTTP-метод в верхнем регистре."""
        return self._method

    @property
    def path(self) -> str:
        """Полный шаблон маршрута, включая префикс источника."""
        return self._path

    @property
    def source(self) -> str:
        """Имя источника из manifest."""
        return self._source

    @property
    def python_path(self) -> tuple[str, ...]:
        """Путь операции в generated namespace."""
        return self._python_path

    @property
    def request(self) -> RequestView:
        """Контракт запроса."""
        return self._request

    @property
    def responses(self) -> tuple[ResponseView, ...]:
        """Все варианты ответа."""
        return self._responses

    @property
    def unsupported(self) -> tuple[str, ...]:
        """Причины строками: тела, которые объявлены, но не проверяются, и отказы d42.

        Структурно непроверяемые тела — в :attr:`unchecked`.
        """
        return self._unsupported

    @property
    def unchecked(self) -> tuple[UncheckedVariant, ...]:
        """Тела, которые спецификация объявила, а контракт не проверяет, структурно."""
        return self._unchecked

    def __repr__(self) -> str:
        return f"OperationHandle({self._key!r}, {self._method} {self._path})"

    # ------------------------------------------------------------- варианты

    def response(
        self, *, status: int | str | None = None, content_type: str | None = None
    ) -> ResponseView:
        """Выбрать вариант ответа.

        Если вариант ровно один, аргументы можно опустить. Если их несколько,
        выбор обязателен — молча брать первый нельзя.
        """
        candidates = [
            item
            for item in self._responses
            if (status is None or item.status == status)
            and (content_type is None or item.content_type == content_type)
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len({item.status for item in candidates}) == 1:
            same = _one_schema_variant(candidates)
            if same is not None:
                return same
        available = sorted(item.label() for item in self._responses)
        if not candidates:
            declared = [
                item
                for item in self._unchecked
                if item.direction is Direction.RESPONSE
                and (status is None or item.status == status)
                and (content_type is None or item.content_type == content_type)
            ]
            if declared:
                raise ResponseVariantError(
                    f"вариант ответа {status if status is not None else '*'}:"
                    f"{content_type or '*'} объявлен спецификацией, но не проверяется: "
                    + "; ".join(f"{item.label()} — {item.reason}" for item in declared)
                    + f". Проверяемые варианты: {available or 'нет'}",
                    operation_key=self._key,
                )
            raise ResponseVariantError(
                f"у операции нет варианта ответа "
                f"{status if status is not None else '*'}:{content_type or '*'}; "
                f"доступны {available}" + self._unchecked_responses_note(),
                operation_key=self._key,
            )
        raise ResponseVariantError(
            f"вариант ответа не выбран однозначно: подходят "
            f"{sorted(i.label() for i in candidates)}. Укажите status и content_type явно",
            operation_key=self._key,
        )

    def _unchecked_responses_note(self) -> str:
        """Хвост сообщения об ответах, которые объявлены, но не проверяются."""
        declared = [item for item in self._unchecked if item.direction is Direction.RESPONSE]
        if not declared:
            return ""
        return "; объявлено без проверки: " + ", ".join(
            f"{item.status}:{item.content_type} ({item.reason})" for item in declared
        )

    # ------------------------------------------------------------ описание

    def describe_response(
        self,
        *,
        status: int | str | None = None,
        content_type: str | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> str:
        """Описание варианта ответа: поля, обязательность, ограничения, адреса артефактов.

        Ничего не перегенерирует и не печатает: читается уже сгенерированный
        контракт, наружу отдаётся строка.
        """
        return self.response(status=status, content_type=content_type).describe(max_depth=max_depth)

    def describe_request_body(
        self, *, content_type: str | None = None, max_depth: int = DEFAULT_MAX_DEPTH
    ) -> str:
        """То же самое для тела запроса."""
        return self._request.body(content_type).describe(max_depth=max_depth)

    # ----------------------------------------------------------- валидация

    def validate_response(
        self,
        body: Any,
        *,
        status: int | str | None = None,
        content_type: str | None = None,
    ) -> ResponseView:
        """Проверить тело ответа по контракту и вернуть выбранный вариант.

        Проверка идёт по JSON Schema всегда и дополнительно по generated d42,
        если extra установлен. Два независимых пути — это не дублирование:
        d42-проекция ``oneOf`` шире исходной семантики, и точность держит именно
        JSON Schema.
        """
        variant = self.response(status=status, content_type=content_type)
        if variant.json_schema is None:
            if body is not None:
                raise ResponseVariantError(
                    f"вариант ответа {variant.label()} описан без тела, "
                    f"но передано тело {type(body).__name__}",
                    operation_key=self._key,
                )
            return variant
        if body is None:
            raise ResponseVariantError(
                f"вариант ответа {variant.label()} требует тело, но передан None",
                operation_key=self._key,
            )
        part = f"тело ответа {variant.label()}"
        self._require_text_value(variant.content_type, body, Direction.RESPONSE, part)
        validate_instance(
            dict(variant.json_schema),
            body,
            operation_key=self._key,
            direction=Direction.RESPONSE,
            part=part,
        )
        self._validate_with_d42(body, export=variant.d42_export, direction=Direction.RESPONSE)
        return variant

    def validate_request_body(self, body: Any, *, content_type: str | None = None) -> None:
        """Проверить тело запроса по контракту."""
        view = self._request.body(content_type)
        part = f"тело запроса {view.content_type}"
        self._require_text_value(view.content_type, body, Direction.REQUEST, part)
        validate_instance(
            dict(view.json_schema),
            body,
            operation_key=self._key,
            direction=Direction.REQUEST,
            part=part,
        )
        self._validate_with_d42(body, export=view.d42_export, direction=Direction.REQUEST)

    def _require_text_value(
        self,
        content_type: str | None,
        value: Any,
        direction: Direction,
        part: str,
        request_index: int | None = None,
    ) -> None:
        """Тело ``text/*`` — всегда строка, даже если схема его не сужает (``allow_any``)."""
        if content_type is None or media_kind(content_type) is not MediaKind.TEXT:
            return
        if isinstance(value, str):
            return
        validate_instance(
            {"type": "string"},
            value,
            operation_key=self._key,
            direction=direction,
            part=part,
            request_index=request_index,
        )

    def validate_recorded_request(
        self,
        *,
        method: str,
        path: str,
        segments: Mapping[str, str],
        params: Sequence[tuple[str, str]],
        headers: Sequence[tuple[str, str]],
        body: Any,
        raw_body: bytes | None = None,
        index: int,
        pinned_path_params: Mapping[str, Any] | None = None,
    ) -> None:
        """Проверить перехваченный запрос целиком.

        Проверяются метод, маршрут, path/query/header/cookie параметры, их
        сериализация, обязательность и тело вместе с content type.
        """
        if method.upper() != self._method:
            raise RequestContractError(
                "метод не совпадает с контрактом",
                request_index=index,
                operation_key=self._key,
                json_pointer="/method",
                expected=self._method,
                actual=method.upper(),
            )
        self._validate_route(path, segments, index, pinned_path_params or {})
        self._validate_params(params, index)
        self._validate_headers(headers, index)
        self._validate_cookies(headers, index)
        self._validate_body(headers, body, raw_body, index)

    # --------------------------------------------------------- части запроса

    def _validate_route(
        self,
        path: str,
        segments: Mapping[str, str],
        index: int,
        pinned: Mapping[str, Any],
    ) -> None:
        extracted = extract_path_params(self._path, path)
        if extracted is None and not segments:
            raise RequestContractError(
                "маршрут запроса не соответствует шаблону операции",
                request_index=index,
                operation_key=self._key,
                json_pointer="/path",
                expected=self._path,
                actual=path,
            )
        resolved: dict[str, str] = dict(extracted or {})
        # ``segments`` заполняется JJ только для незакреплённых шаблонных сегментов,
        # поэтому оно дополняет разбор по шаблону, а не заменяет его.
        resolved.update({key: value for key, value in segments.items() if value is not None})

        for view in self._request.path:
            raw = resolved.get(view.name)
            if raw is None:
                raise RequestContractError(
                    f"в маршруте не найден path-параметр {view.name!r}",
                    request_index=index,
                    operation_key=self._key,
                    json_pointer=f"/path/{view.name}",
                    expected=self._path,
                    actual=path,
                )
            if view.name in pinned and wire_text(pinned[view.name]) != raw:
                raise RequestContractError(
                    f"path-параметр {view.name!r} отличается от закреплённого в моке",
                    request_index=index,
                    operation_key=self._key,
                    json_pointer=f"/path/{view.name}",
                    expected=wire_text(pinned[view.name]),
                    actual=raw,
                )
            value = decode_parameter(
                name=view.name,
                location=ParameterLocation.PATH,
                style=view.style,
                explode=view.explode,
                schema=dict(view.json_schema),
                values=[raw],
                operation_key=self._key,
                request_index=index,
            )
            validate_instance(
                dict(view.json_schema),
                value,
                operation_key=self._key,
                direction=Direction.REQUEST,
                part=f"path-параметр {view.name!r}",
                request_index=index,
            )

    def _validate_params(self, params: Sequence[tuple[str, str]], index: int) -> None:
        grouped: dict[str, list[str]] = {}
        for name, value in params:
            grouped.setdefault(name, []).append(value)
        for view in self._request.query:
            values = grouped.get(view.name)
            if not values:
                if view.required:
                    raise RequestContractError(
                        f"обязательный query-параметр {view.name!r} отсутствует",
                        request_index=index,
                        operation_key=self._key,
                        json_pointer=f"/query/{view.name}",
                        expected="параметр присутствует",
                        actual="отсутствует",
                    )
                continue
            value = decode_parameter(
                name=view.name,
                location=ParameterLocation.QUERY,
                style=view.style,
                explode=view.explode,
                schema=dict(view.json_schema),
                values=values,
                operation_key=self._key,
                request_index=index,
            )
            validate_instance(
                dict(view.json_schema),
                value,
                operation_key=self._key,
                direction=Direction.REQUEST,
                part=f"query-параметр {view.name!r}",
                request_index=index,
            )

    def _validate_headers(self, headers: Sequence[tuple[str, str]], index: int) -> None:
        lowered: dict[str, list[str]] = {}
        for name, value in headers:
            lowered.setdefault(name.lower(), []).append(value)
        for view in self._request.header:
            values = lowered.get(view.name.lower())
            if not values:
                if view.required:
                    raise RequestContractError(
                        f"обязательный заголовок {view.name!r} отсутствует",
                        request_index=index,
                        operation_key=self._key,
                        json_pointer=f"/header/{view.name}",
                        expected="заголовок присутствует",
                        actual="отсутствует",
                    )
                continue
            value = decode_parameter(
                name=view.name,
                location=ParameterLocation.HEADER,
                style=view.style,
                explode=view.explode,
                schema=dict(view.json_schema),
                values=values,
                operation_key=self._key,
                request_index=index,
            )
            validate_instance(
                dict(view.json_schema),
                value,
                operation_key=self._key,
                direction=Direction.REQUEST,
                part=f"заголовок {view.name!r}",
                request_index=index,
            )

    def _validate_cookies(self, headers: Sequence[tuple[str, str]], index: int) -> None:
        if not self._request.cookie:
            return
        jar: dict[str, str] = {}
        for name, value in headers:
            if name.lower() != "cookie":
                continue
            for chunk in value.split(";"):
                if "=" not in chunk:
                    continue
                cookie_name, _, cookie_value = chunk.partition("=")
                jar[cookie_name.strip()] = cookie_value.strip()
        for view in self._request.cookie:
            raw = jar.get(view.name)
            if raw is None:
                if view.required:
                    raise RequestContractError(
                        f"обязательная cookie {view.name!r} отсутствует",
                        request_index=index,
                        operation_key=self._key,
                        json_pointer=f"/cookie/{view.name}",
                        expected="cookie присутствует",
                        actual="отсутствует",
                    )
                continue
            value = decode_parameter(
                name=view.name,
                location=ParameterLocation.COOKIE,
                style=view.style,
                explode=view.explode,
                schema=dict(view.json_schema),
                values=[raw],
                operation_key=self._key,
                request_index=index,
            )
            validate_instance(
                dict(view.json_schema),
                value,
                operation_key=self._key,
                direction=Direction.REQUEST,
                part=f"cookie {view.name!r}",
                request_index=index,
            )

    def _validate_body(
        self,
        headers: Sequence[tuple[str, str]],
        body: Any,
        raw_body: bytes | None,
        index: int,
    ) -> None:
        content_type_header = None
        for name, value in headers:
            if name.lower() == "content-type":
                content_type_header = value
                break
        content_type = (
            None
            if content_type_header is None
            else content_type_header.split(";", 1)[0].strip().lower()
        )

        unchecked = self._request.unchecked_bodies
        if not self._request.bodies and not unchecked:
            return

        required = any(item.required for item in self._request.bodies) or any(
            item.required for item in unchecked
        )
        payload = body if body not in (None, b"", "") else None
        if payload is None and raw_body:
            payload = raw_body
        if payload is None:
            if required:
                declared = sorted(
                    [i.content_type for i in self._request.bodies]
                    + [i.content_type for i in unchecked]
                )
                raise RequestContractError(
                    "тело запроса обязательно, но запрос пришёл без тела",
                    request_index=index,
                    operation_key=self._key,
                    json_pointer="/body",
                    expected=f"тело {declared}",
                    actual="пусто",
                )
            return

        if content_type is None:
            available = sorted(
                [i.content_type for i in self._request.bodies] + [i.content_type for i in unchecked]
            )
            raise RequestContractError(
                "у запроса с телом нет заголовка Content-Type",
                request_index=index,
                operation_key=self._key,
                json_pointer="/header/Content-Type",
                expected=f"один из {available}",
                actual="отсутствует",
            )
        match = self._request.match(content_type)
        if match is None:
            declared = sorted(
                [i.content_type for i in self._request.bodies] + [i.content_type for i in unchecked]
            )
            raise RequestContractError(
                "content type запроса не описан контрактом",
                request_index=index,
                operation_key=self._key,
                json_pointer="/header/Content-Type",
                expected=f"один из {declared}"
                + (
                    f"; из них не проверяются: {sorted(i.content_type for i in unchecked)}"
                    if unchecked
                    else ""
                ),
                actual=content_type,
            )
        if isinstance(match, UncheckedVariant):
            # Тело объявлено спецификацией, но контракта у него нет: признать его
            # дефектом нельзя, а проверить нечем (см. ``operation.unchecked``).
            return
        view = match
        # Объявлен диапазон (``*/*``) — тело разбирается по фактическому типу запроса.
        kind = media_kind(content_type)
        if kind is MediaKind.OTHER:
            # ``*/*`` покрывает и бинарное тело, но разобрать его для проверки нечем.
            return

        if kind is MediaKind.TEXT:
            payload = self._decode_text_body(
                payload, raw_body, content_type_header or content_type, index
            )
        elif isinstance(payload, (bytes, bytearray)):
            try:
                payload = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RequestContractError(
                    f"тело запроса не разбирается как JSON: {exc}",
                    request_index=index,
                    operation_key=self._key,
                    json_pointer="/body",
                    expected="валидный JSON",
                    actual=repr(payload[:200]),
                ) from exc
        elif isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise RequestContractError(
                    f"тело запроса не разбирается как JSON: {exc}",
                    request_index=index,
                    operation_key=self._key,
                    json_pointer="/body",
                    expected="валидный JSON",
                    actual=payload[:200],
                ) from exc

        validate_instance(
            dict(view.json_schema),
            payload,
            operation_key=self._key,
            direction=Direction.REQUEST,
            part=f"тело запроса {view.content_type}",
            request_index=index,
        )
        self._validate_with_d42(payload, export=view.d42_export, direction=Direction.REQUEST)

    def _decode_text_body(
        self, payload: Any, raw_body: bytes | None, content_type_header: str, index: int
    ) -> str:
        """Текст тела запроса: байты декодируются по ``charset`` из ``Content-Type``.

        Предпочитаются исходные байты: JJ сам декодирует только ``text/plain`` и всегда
        как UTF-8, а ``charset=windows-1251`` в заголовке — обычное дело. Нет ``charset``
        — UTF-8.
        """
        charset = declared_charset(content_type_header) or DEFAULT_TEXT_CHARSET
        data = raw_body if isinstance(raw_body, (bytes, bytearray)) and raw_body else payload
        if isinstance(data, str):
            return data
        if not isinstance(data, (bytes, bytearray)):
            raise RequestContractError(
                "тело запроса с текстовым content type не является текстом",
                request_index=index,
                operation_key=self._key,
                json_pointer="/body",
                expected="строка",
                actual=type(data).__name__,
            )
        try:
            return bytes(data).decode(charset)
        except LookupError as exc:
            raise RequestContractError(
                f"неизвестный charset {charset!r} в Content-Type",
                request_index=index,
                operation_key=self._key,
                json_pointer="/header/Content-Type",
                expected="известная Python кодировка",
                actual=content_type_header,
            ) from exc
        except UnicodeDecodeError as exc:
            raise RequestContractError(
                f"тело запроса не декодируется как {charset}: {exc}",
                request_index=index,
                operation_key=self._key,
                json_pointer="/body",
                expected=f"текст в кодировке {charset}",
                actual=repr(bytes(data[:200])),
            ) from exc

    # --------------------------------------------------- опциональные extras

    def _d42_module_name(self, direction: Direction) -> str | None:
        return self._d42_modules.get(direction.value)

    def d42_schema(
        self,
        direction: Direction,
        *,
        export: str | None = None,
        part: ParameterLocation | str | None = None,
    ) -> Any:
        """Вернуть generated d42-схему направления или параметров одного места запроса.

        ``export`` — имя generated-переменной (``view.d42_export``). Если в
        направлении ровно одна d42-схема тела, его можно не передавать; если их
        несколько, выбор обязателен — молча брать первую нельзя.

        ``part`` — место запроса (``"path"``, ``"query"``, ``"header"``, ``"cookie"``):
        вернуть схему его параметров в том виде, в каком значения уходят в запрос
        (см. :class:`ParameterSetView`). Только для ``Direction.REQUEST`` и без
        ``export``.

        Требует extra ``[d42]``. Если d42 для направления или места не генерировался
        (``d42: false`` в manifest или конструкция, невыразимая в d42), поднимается
        понятная ошибка с причиной.
        """
        if part is not None:
            if export is not None:
                raise OperationLookupError(
                    "передайте либо export, либо part: part сам выбирает схему места",
                    operation_key=self._key,
                )
            if direction is not Direction.REQUEST:
                raise OperationLookupError(
                    f"d42-схемы параметров генерируются только для запроса, а передано "
                    f"направление {direction.value}",
                    operation_key=self._key,
                )
            try:
                view = self._request.parameter_set(part)
            except OperationLookupError as error:
                raise OperationLookupError(error.base_message, operation_key=self._key) from None
            return self._parameters_d42_schema(view)
        if direction is Direction.REQUEST and export is not None:
            for item in self._request.parameter_sets:
                if item.d42_export == export:
                    return self._parameters_d42_schema(item)
        if export is None:
            export = self._only_d42_export(direction)
        module_name = self._d42_module_name(direction)
        if module_name is None or export is None:
            missing_body = self._missing_body(direction)
            if missing_body is not None:
                raise OperationLookupError(missing_body, operation_key=self._key)
            reason = (
                self._d42_reasons.get(direction.value)
                or self._d42_reason
                or "в manifest у операции стоит d42: false"
            )
            raise OperationLookupError(
                f"для операции {self._key!r} не сгенерированы d42-схемы направления "
                f"{direction.value}: {reason}",
                operation_key=self._key,
            )
        module = _import_d42_module(module_name)
        try:
            return getattr(module, export)
        except AttributeError as exc:
            raise OperationLookupError(
                f"в модуле {module_name} нет схемы {export!r}; перегенерируйте артефакты "
                f"командой 'geas update'",
                operation_key=self._key,
            ) from exc

    def _missing_body(self, direction: Direction) -> str | None:
        """Сообщение, если d42-схемы тела нет потому, что тела нет в контракте."""
        declared = [
            f"{item.label()} — {item.reason}"
            for item in self._unchecked
            if item.direction is direction
        ]
        note = f" (объявлено без проверки: {'; '.join(declared)})" if declared else ""
        if direction is Direction.RESPONSE:
            if any(item.json_schema is not None for item in self._responses):
                return None
            what = "проверяемого ответа" if declared else "ответа"
            return f"у операции {self._key!r} нет {what} с телом{note}, поэтому нет и его d42-схемы"
        if self._request.bodies:
            return None
        what = "проверяемого тела" if declared else "тела"
        message = f"у операции {self._key!r} нет {what} запроса{note}, поэтому нет и его d42-схемы"
        places = [item.location.value for item in self._request.parameter_sets]
        if places:
            message += (
                f". d42-схемы параметров: d42_schema(Direction.REQUEST, part=...), места {places}"
            )
        return message

    def _parameters_d42_schema(self, view: ParameterSetView) -> Any:
        """d42-схема параметров места либо ошибка с причиной, почему её нет."""
        if view.d42_module is None or view.d42_export is None:
            reason = view.d42_reason or self._d42_reason or "в manifest у операции стоит d42: false"
            raise OperationLookupError(
                f"для операции {self._key!r} не сгенерирована d42-схема "
                f"{view.location.value}-параметров: {reason}",
                operation_key=self._key,
            )
        module = _import_d42_module(view.d42_module)
        try:
            return getattr(module, view.d42_export)
        except AttributeError as exc:
            raise OperationLookupError(
                f"в модуле {view.d42_module} нет схемы {view.d42_export!r}; перегенерируйте "
                f"артефакты командой 'geas update'",
                operation_key=self._key,
            ) from exc

    def _only_d42_export(self, direction: Direction) -> str | None:
        """Имя единственной d42-схемы направления или ``None``, если схем нет."""
        variants: Sequence[RequestBodyView | ResponseView] = (
            self._request.bodies if direction is Direction.REQUEST else self._responses
        )
        exports = sorted({item.d42_export for item in variants if item.d42_export is not None})
        if len(exports) > 1:
            raise OperationLookupError(
                f"у операции {self._key!r} несколько d42-схем направления "
                f"{direction.value} ({exports}); передайте export явно, например "
                f"export=operation.response(status=200).d42_export",
                operation_key=self._key,
            )
        return exports[0] if exports else None

    def _validate_with_d42(self, value: Any, *, export: str | None, direction: Direction) -> None:
        """Дополнительная проверка по generated d42, если extra установлен."""
        if export is None or self._d42_module_name(direction) is None:
            return
        try:
            import d42  # noqa: F401
        except ImportError:
            return
        from ..integrations.d42.converter import validate_with_d42

        validate_with_d42(
            self.d42_schema(direction, export=export),
            value,
            operation_key=self._key,
            direction=direction,
        )

    def mock(self, **kwargs: Any) -> Any:
        """Создать operation-aware JJ-мок.

        Требует extra ``[jj]``. Подробности — в
        :class:`geas.integrations.jj.ContractMock`.
        """
        try:
            from ..integrations.jj.contract_mock import ContractMock
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise MissingExtraError("jj", "OperationHandle.mock()") from exc
        return ContractMock(operation=self, **kwargs)


def _parse_location(location: ParameterLocation | str) -> ParameterLocation:
    """Место запроса из enum или его строкового значения."""
    if isinstance(location, ParameterLocation):
        return location
    try:
        return ParameterLocation(location)
    except ValueError:
        allowed = [item.value for item in ParameterLocation]
        raise OperationLookupError(
            f"неизвестное место параметров {location!r}; допустимы {allowed}"
        ) from None


def _import_d42_module(module_name: str) -> Any:
    from importlib import import_module

    try:
        import d42  # noqa: F401
    except ImportError as exc:
        raise MissingExtraError("d42", "generated d42-схемы") from exc
    # Интеграция сама проверяет версию d42 и объясняет, если она слишком старая.
    import_module("geas.integrations.d42")
    return import_module(module_name)


_TEMPLATE_VARIABLE = re.compile(r"\{([^}]*)\}")


def extract_path_params(template: str, actual: str) -> dict[str, str] | None:
    """Разобрать фактический путь по шаблону маршрута.

    Возвращает ``None``, если путь шаблону не соответствует. Имена переменных
    берутся из шаблона как есть: в реальных спецификациях они не обязаны быть
    валидными Python-идентификаторами, поэтому именованные группы не годятся.
    """
    from urllib.parse import unquote

    names: list[str] = []
    pattern = ["^"]
    position = 0
    for match in _TEMPLATE_VARIABLE.finditer(template):
        pattern.append(re.escape(template[position : match.start()]))
        pattern.append("([^/]+)")
        names.append(match.group(1))
        position = match.end()
    pattern.append(re.escape(template[position:]))
    pattern.append("$")

    found = re.match("".join(pattern), actual)
    if found is None:
        return None
    return {name: unquote(value) for name, value in zip(names, found.groups())}

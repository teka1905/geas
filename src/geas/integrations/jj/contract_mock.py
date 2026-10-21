"""``ContractMock`` — единственный публичный вход в JJ-интеграцию v0.1.

.. code-block:: python

    async with operations.ws2.add_ticket.mock(
        response=response_body,
        path_params={"queueId": queue_id},
        wait_for_requests=1,
    ) as mock:
        await page.submit()

Жизненный цикл:

1. разрешается операция и вариант ответа;
2. проверяются заданные path- и query-параметры;
3. тело ответа проверяется по generated d42;
4. тело ответа независимо проверяется по JSON Schema;
5. проверяются статус и content type;
6. **только после этого** строится и регистрируется JJ-ответ;
7. при успешном выходе забирается история;
8. выполняется cleanup/deregister;
9. проверяется каждый перехваченный запрос;
10. история остаётся доступной пользователю.

``wait_for_requests`` — это только синхронизация жизненного цикла и получение
истории. Он **не заменяет** ассерты теста на точное количество вызовов: их
по-прежнему пишет потребитель, опираясь на :attr:`ContractMock.history`.

Если тело сценария бросило исключение, оно остаётся первичным: cleanup всё равно
выполняется, а вторичные диагностики прикладываются к нему заметками
(``exception notes``) и доступны через :attr:`ContractMock.diagnostics`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any

from ...errors import ContractMockError, RequestContractError
from ...media import DEFAULT_TEXT_CHARSET, MediaKind, declared_charset, media_kind
from ...models import Direction, ParameterLocation
from ...runtime.operation import route_variants
from ...runtime.validation import (
    ARRAY_DELIMITERS,
    decode_parameter,
    json_type,
    validate_instance,
    wire_text,
)
from . import backend

if TYPE_CHECKING:  # pragma: no cover
    from ...runtime.operation import OperationHandle, ResponseView

__all__ = ["ContractMock"]

_TEMPLATE = re.compile(r"\{([^}]+)\}")


class ContractMock:
    """Асинхронный контекстный менеджер operation-aware мока."""

    __slots__ = (
        "_body",
        "_diagnostics",
        "_entered",
        "_headers",
        "_history",
        "_history_callback",
        "_mocked",
        "_operation",
        "_path_params",
        "_query_params",
        "_response_headers",
        "_status",
        "_timeout",
        "_variant",
        "_wait_for_requests",
    )

    def __init__(
        self,
        *,
        operation: OperationHandle,
        response: Any = None,
        status: int | str | None = None,
        content_type: str | None = None,
        path_params: Mapping[str, Any] | None = None,
        query_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        response_headers: Mapping[str, str] | None = None,
        history_callback: Callable[[Sequence[Any]], None] | None = None,
        wait_for_requests: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        backend.check_version()
        self._operation = operation
        self._path_params = dict(path_params or {})
        self._query_params = dict(query_params or {})
        self._headers = dict(headers or {})
        self._response_headers = dict(response_headers or {})
        self._history_callback = history_callback
        self._wait_for_requests = wait_for_requests
        self._timeout = timeout
        self._history: list[Any] = []
        self._diagnostics: tuple[BaseException, ...] = ()
        self._entered = False
        self._mocked: Any = None

        self._check_path_params()
        self._query_params = self._checked_query_params()
        self._headers = self._checked_headers()
        self._variant, self._status = self._select_variant(status, content_type)
        self._body = response
        # Шаги 3–5: сначала d42, потом независимо JSON Schema, затем статус и
        # content type. Всё это происходит до создания и регистрации мока.
        self._operation.validate_response(
            response,
            status=self._variant.status,
            content_type=self._variant.content_type,
        )
        self._check_status_and_content_type()
        self._check_text_encoding()

    # ------------------------------------------------------------ подготовка

    def _check_path_params(self) -> None:
        """Проверить закреплённые path-параметры по контракту и перевести их в строки."""
        known = {view.name: view for view in self._operation.request.path}
        unknown = sorted(set(self._path_params) - set(known))
        if unknown:
            raise ContractMockError(
                f"path_params содержит параметры {unknown}, которых нет в маршруте "
                f"{self._operation.path!r}. Описанные: {sorted(known)}",
                operation_key=self._operation.key,
            )
        self._path_params = {
            name: self._pinned_text(known[name], value, part=f"path_params[{name!r}]")
            for name, value in sorted(self._path_params.items())
        }

    def _checked_query_params(self) -> dict[str, str]:
        """Перевести закреплённые query-параметры в строки матчера и проверить их по контракту.

        Без этой проверки значение вне ``enum`` или не того типа уходило бы в матчер
        JJ как есть: мок молча не совпадал бы ни с одним запросом, и тест падал бы
        с «перехвачено 0», а не с указанием на параметр. Правило перевода — в
        :meth:`_pinned_text`.

        Параметр, которого нет в контракте, закрепить нельзя: расхождение спецификации
        с API чинится в спецификации, а до тех пор такой параметр проверяют по
        истории мока.
        """
        known = {view.name: view for view in self._operation.request.query}
        unknown = sorted(set(self._query_params) - set(known))
        if unknown:
            raise ContractMockError(
                f"query_params содержит параметры {unknown}, которых нет в контракте "
                f"операции. Описанные: {sorted(known)}. Такой параметр проверяйте по "
                f"истории мока: mock.requests[i].params",
                operation_key=self._operation.key,
            )
        return {
            name: self._pinned_text(known[name], value, part=f"query_params[{name!r}]")
            for name, value in sorted(self._query_params.items())
        }

    def _checked_headers(self) -> dict[str, str]:
        """Перевести закреплённые заголовки в строки матчера.

        Заголовок, описанный контрактом (имя сравнивается без учёта регистра), проверяется
        так же, как query-параметр. Остальные (``Authorization``, ``X-Request-Id``)
        контракт обычно не описывает, и они закрепляются как есть — строкой.
        """
        known = {view.name.lower(): view for view in self._operation.request.header}
        pinned: dict[str, str] = {}
        for name, value in sorted(self._headers.items()):
            part = f"headers[{name!r}]"
            view = known.get(name.lower())
            pinned[name] = (
                self._pinned_text(view, value, part=part)
                if view is not None
                else self._matcher_text(part, value, delimiter=None)
            )
        return pinned

    def _pinned_text(self, view: Any, value: Any, *, part: str) -> str:
        """Строка, которую сравнит матчер JJ, проверенная по контракту параметра.

        Значение сначала становится строкой — ``True`` → ``"true"``, ``None`` → ``""``
        (так ``null`` записывается в запросе), ``UUID`` и ``date`` — через ``str()``, — и
        уже она разбирается по контракту тем же правилом, что перехваченный запрос.
        У массива повторами ключа строка закрепляет один повтор и проверяется по схеме
        элемента: матчер совпадает, если совпал хотя бы один повтор. Массив одной
        строкой можно передать списком — он склеится через разделитель стиля.
        """
        json_schema = dict(view.json_schema)
        array = json_type(json_schema)[0] == "array"
        repeated = (
            array
            and view.location is ParameterLocation.QUERY
            and view.style == "form"
            and view.explode
        )
        delimiter = None if repeated or not array else ARRAY_DELIMITERS.get(view.style, ",")
        text = self._matcher_text(part, value, delimiter=delimiter)
        checked = dict(json_schema.get("items") or {}) if repeated else json_schema
        try:
            decoded = decode_parameter(
                name=view.name,
                location=view.location,
                style=view.style,
                explode=view.explode,
                schema=checked,
                values=[text],
                operation_key=self._operation.key,
            )
        except RequestContractError:
            if value is not None:
                raise
            decoded = text
        if value is None and decoded is not None:
            # Пустая строка разбирается в null только у nullable-строки; у числа, флага
            # и не-nullable строки null в запросе не записать.
            raise ContractMockError(
                f"{part}: null в запросе записывается пустой строкой, а у параметра "
                f"{view.name!r} пустая строка не разбирается в null",
                operation_key=self._operation.key,
            )
        validate_instance(
            checked,
            decoded,
            operation_key=self._operation.key,
            direction=Direction.REQUEST,
            part=part,
        )
        return text

    def _matcher_text(self, part: str, value: Any, *, delimiter: str | None) -> str:
        """Строка матчера по :func:`~geas.runtime.validation.wire_text`.

        Список склеивается через ``delimiter`` — у массива, который уходит одной строкой.
        """
        if isinstance(value, (list, tuple)) and delimiter is not None:
            return delimiter.join(self._matcher_text(part, item, delimiter=None) for item in value)
        try:
            return wire_text(value)
        except ValueError as error:
            raise ContractMockError(
                f"{part}: {error}. Матчер JJ сравнивает одно значение параметра: закрепите "
                f"один повтор ключа либо проверьте значение по истории мока",
                operation_key=self._operation.key,
            ) from None

    def _select_variant(
        self, status: int | str | None, content_type: str | None
    ) -> tuple[ResponseView, int]:
        """Выбрать вариант ответа и HTTP-статус, которым его отдавать."""
        from ...errors import ResponseVariantError

        try:
            variant = self._operation.response(status=status, content_type=content_type)
        except ResponseVariantError:
            # ``default`` подменяет только статус, которого спецификация не объявляет
            # вовсе. Объявленный статус (в том числе непроверяемый вариант вроде
            # ``200: application/octet-stream``) чужой схемой ``default`` не мокается.
            if not isinstance(status, int) or self._status_is_declared(status):
                raise
            if not any(item.status == "default" for item in self._operation.responses):
                raise
            # Выбор среди вариантов ``default`` идёт по тем же правилам, что и любой
            # другой: content_type учитывается, неоднозначность — ошибка.
            variant = self._operation.response(status="default", content_type=content_type)
            return variant, status

        if isinstance(variant.status, int):
            return variant, variant.status
        if isinstance(status, int):
            return variant, status
        raise ContractMockError(
            "выбран вариант ответа 'default'; передайте конкретный HTTP-статус аргументом status",
            operation_key=self._operation.key,
        )

    def _status_is_declared(self, status: int) -> bool:
        """Объявлен ли статус спецификацией — проверяемым или непроверяемым вариантом."""
        return any(item.status == status for item in self._operation.responses) or any(
            item.status == status
            for item in self._operation.unchecked
            if item.direction is Direction.RESPONSE
        )

    def _served_content_type(self) -> str | None:
        """Media type, по которому решается, как отдать тело: заголовок из
        ``response_headers``, иначе тип варианта (``*/*`` — JSON).
        """
        override = {key.lower(): value for key, value in self._response_headers.items()}
        supplied = override.get("content-type")
        if supplied is not None:
            return supplied.split(";", 1)[0].strip().lower()
        return self._variant.content_type

    def _serves_text(self) -> bool:
        """Уходит ли тело строкой как есть (``text/*``), а не JSON-литералом."""
        served = self._served_content_type()
        return (
            self._variant.json_schema is not None
            and served is not None
            and media_kind(served) is MediaKind.TEXT
        )

    def _check_text_encoding(self) -> None:
        """Строковое тело ``text/*`` обязано кодироваться в ``charset`` ответа.

        Иначе ошибка (неизвестный ``charset``, символ вне кодировки) вылетела бы из JJ
        на входе в ``async with``, а не здесь — до регистрации мока.
        """
        if not self._serves_text():
            return
        if not isinstance(self._body, str):
            raise ContractMockError(
                f"ответ с Content-Type {self._served_content_type()!r} отдаётся строкой как "
                f"есть, а передано {type(self._body).__name__}",
                operation_key=self._operation.key,
            )
        override = {key.lower(): value for key, value in self._response_headers.items()}
        header = override.get("content-type")
        charset = (declared_charset(header) if header else None) or DEFAULT_TEXT_CHARSET
        try:
            self._body.encode(charset)
        except LookupError:
            raise ContractMockError(
                f"неизвестный charset {charset!r} в Content-Type ответа {header!r}",
                operation_key=self._operation.key,
            ) from None
        except UnicodeEncodeError as error:
            raise ContractMockError(
                f"тело ответа не кодируется в {charset}: {error}. Укажите подходящий charset "
                f"в response_headers['Content-Type'] или уберите его — по умолчанию utf-8",
                operation_key=self._operation.key,
            ) from None

    def _check_status_and_content_type(self) -> None:
        if not 100 <= self._status <= 599:
            raise ContractMockError(
                f"HTTP-статус {self._status} вне диапазона 100..599",
                operation_key=self._operation.key,
            )
        declared = self._variant.content_type
        override = {key.lower(): value for key, value in self._response_headers.items()}
        supplied = override.get("content-type")
        if supplied is None or declared is None:
            return
        canonical = supplied.split(";", 1)[0].strip().lower()
        if declared == "*/*":
            # ``*/*`` описан JSON-схемой: отдать его можно JSON или текстом, но не
            # бинарным телом, которое мок не умеет собрать из значения.
            if media_kind(canonical) is MediaKind.OTHER:
                raise ContractMockError(
                    f"вариант */* описан JSON-схемой: отдать его можно как JSON или text/*, "
                    f"а в заголовке {supplied!r}",
                    operation_key=self._operation.key,
                )
            return
        if canonical != declared:
            raise ContractMockError(
                f"content type ответа не совпадает с контрактом: заголовок "
                f"{supplied!r}, контракт {declared!r}",
                operation_key=self._operation.key,
            )

    def _resolved_path(self) -> str:
        """Подставить закреплённые path-параметры, остальные оставить шаблонами."""

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in self._path_params:
                return match.group(0)
            value = str(self._path_params[name])
            if "/" in value:
                raise ContractMockError(
                    f"path-параметр {name!r} содержит '/' и разбил бы маршрут на лишние "
                    f"сегменты: {value!r}",
                    operation_key=self._operation.key,
                )
            return value

        return _TEMPLATE.sub(substitute, self._operation.path)

    def _matched_paths(self) -> list[str]:
        """Формы маршрута, которые перехватывает мок."""
        resolved = self._resolved_path()
        if not self._operation.accept_trailing_slash:
            return [resolved]
        return list(route_variants(resolved))

    # ------------------------------------------------------- контекст-менеджер

    async def __aenter__(self) -> ContractMock:
        if self._entered:
            raise ContractMockError(
                "ContractMock нельзя войти повторно: создайте новый мок",
                operation_key=self._operation.key,
            )
        self._entered = True
        headers = dict(self._response_headers)
        declared = self._variant.content_type
        has_content_type = any(key.lower() == "content-type" for key in headers)
        # ``*/*`` — это «любой тип», а не заголовок: ответ с JSON-телом JJ подпишет сам.
        if declared and declared != "*/*" and not has_content_type:
            headers["Content-Type"] = declared
        matcher = backend.build_matcher(
            method=self._operation.method,
            path=self._matched_paths(),
            params=self._query_params or None,
            headers=self._headers or None,
        )
        response = backend.build_response(
            status=self._status,
            body=self._body,
            has_body=self._variant.json_schema is not None,
            as_text=self._serves_text(),
            headers=headers,
        )
        self._mocked = backend.create_mocked(matcher, response)
        await self._mocked.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        secondary: list[BaseException] = []

        if exc is None and self._wait_for_requests:
            try:
                await self._mocked.wait_for_requests(self._wait_for_requests, timeout=self._timeout)
            except BaseException as error:
                secondary.append(error)

        try:
            await self._mocked.__aexit__(exc_type, exc, tb)
        except BaseException as error:
            secondary.append(error)

        self._history = list(self._mocked.history or ())

        if self._history_callback is not None:
            try:
                self._history_callback(self._history)
            except BaseException as error:
                secondary.append(error)

        if (
            exc is None
            and self._wait_for_requests is not None
            and len(self._history) < self._wait_for_requests
        ):
            secondary.append(
                ContractMockError(
                    f"ожидалось минимум {self._wait_for_requests} запрос(ов), "
                    f"перехвачено {len(self._history)}",
                    operation_key=self._operation.key,
                )
            )

        try:
            self.validate_requests()
        except BaseException as error:
            secondary.append(error)

        self._diagnostics = tuple(secondary)

        if exc is not None:
            for item in secondary:
                _attach_note(exc, item)
            return False

        if secondary:
            primary = secondary[0]
            for item in secondary[1:]:
                _attach_note(primary, item)
            raise primary
        return False

    # ---------------------------------------------------------------- данные

    @property
    def history(self) -> list[Any]:
        """История перехваченных обменов в совместимом с JJ формате list."""
        return self._history

    @property
    def requests(self) -> tuple[Any, ...]:
        """Перехваченные запросы — удобный срез истории."""
        return tuple(
            item["request"] if isinstance(item, dict) else item.request for item in self._history
        )

    @property
    def operation(self) -> OperationHandle:
        """Операция, для которой создан мок."""
        return self._operation

    @property
    def status(self) -> int:
        """HTTP-статус, которым отвечает мок."""
        return self._status

    @property
    def variant(self) -> ResponseView:
        """Вариант ответа контракта, которым отвечает мок."""
        return self._variant

    @property
    def diagnostics(self) -> tuple[BaseException, ...]:
        """Вторичные ошибки, если основное исключение пришло из тела сценария."""
        return self._diagnostics

    def validate_requests(self) -> None:
        """Проверить каждый перехваченный запрос по контракту операции."""
        for index, item in enumerate(self._history):
            parts = backend.history_request_parts(item)
            self._operation.validate_recorded_request(
                method=parts["method"],
                path=parts["path"],
                segments=parts["segments"],
                params=parts["params"],
                headers=parts["headers"],
                body=parts["body"],
                raw_body=parts["raw"],
                index=index,
                pinned_path_params=self._path_params,
            )

    def __repr__(self) -> str:
        # ``_resolved_path`` имеет право отказаться подставлять значение (например
        # со слэшем внутри), но ``repr`` вызывают отладчик и сам pytest при разборе
        # падения. Исключение отсюда подменило бы настоящую причину ошибки, поэтому
        # непригодное значение показывается как есть, шаблоном.
        try:
            path = self._resolved_path()
        except ContractMockError:
            path = self._operation.path
        return (
            f"ContractMock({self._operation.key!r}, {self._operation.method} "
            f"{path}, status={self._status})"
        )


def _attach_note(target: BaseException, secondary: BaseException) -> None:
    """Приложить вторичную диагностику к первичному исключению.

    ``BaseException.add_note`` появился в Python 3.11 — там заметка попадает ещё и
    в печатаемый traceback. На 3.10 ``__notes__`` заполняется вручную: интерпретатор
    его не отрисует, но программный доступ к диагностике одинаков на всех
    поддержанных версиях, и тестам не нужно ветвиться по версии Python.

    Дублирующий канал — :attr:`ContractMock.diagnostics`: там лежат сами объекты
    исключений, а не их текст.
    """
    note = f"[geas] {type(secondary).__name__}: {secondary}"
    add_note = getattr(target, "add_note", None)
    if add_note is not None:
        add_note(note)
        return
    notes = getattr(target, "__notes__", None)
    if isinstance(notes, list):
        notes.append(note)
    else:
        target.__notes__ = [note]  # type: ignore[attr-defined]

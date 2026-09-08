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
2. проверяются заданные path-параметры;
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
from collections.abc import Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any

from ...errors import ContractMockError
from ...models import Direction
from ...runtime.validation import validate_instance
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
        wait_for_requests: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        backend.check_version()
        self._operation = operation
        self._path_params = dict(path_params or {})
        self._query_params = dict(query_params or {})
        self._headers = dict(headers or {})
        self._response_headers = dict(response_headers or {})
        self._wait_for_requests = wait_for_requests
        self._timeout = timeout
        self._history: tuple[Any, ...] = ()
        self._diagnostics: tuple[BaseException, ...] = ()
        self._entered = False
        self._mocked: Any = None

        self._check_path_params()
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

    # ------------------------------------------------------------ подготовка

    def _check_path_params(self) -> None:
        """Проверить, что закреплённые path-параметры описаны контрактом."""
        known = {view.name: view for view in self._operation.request.path}
        unknown = sorted(set(self._path_params) - set(known))
        if unknown:
            raise ContractMockError(
                f"path_params содержит параметры {unknown}, которых нет в маршруте "
                f"{self._operation.path!r}. Описанные: {sorted(known)}",
                operation_key=self._operation.key,
            )
        for name, value in sorted(self._path_params.items()):
            view = known[name]
            validate_instance(
                dict(view.json_schema),
                value,
                operation_key=self._operation.key,
                direction=Direction.REQUEST,
                part=f"path_params[{name!r}]",
            )

    def _select_variant(
        self, status: int | str | None, content_type: str | None
    ) -> tuple[ResponseView, int]:
        """Выбрать вариант ответа и HTTP-статус, которым его отдавать."""
        from ...errors import ResponseVariantError

        try:
            variant = self._operation.response(status=status, content_type=content_type)
        except ResponseVariantError:
            fallback = [item for item in self._operation.responses if item.status == "default"]
            if not (fallback and isinstance(status, int)):
                raise
            variant = fallback[0]
            return variant, status

        if isinstance(variant.status, int):
            return variant, variant.status
        if isinstance(status, int):
            return variant, status
        raise ContractMockError(
            "выбран вариант ответа 'default'; передайте конкретный HTTP-статус аргументом status",
            operation_key=self._operation.key,
        )

    def _check_status_and_content_type(self) -> None:
        if not 100 <= self._status <= 599:
            raise ContractMockError(
                f"HTTP-статус {self._status} вне диапазона 100..599",
                operation_key=self._operation.key,
            )
        declared = self._variant.content_type
        override = {key.lower(): value for key, value in self._response_headers.items()}
        supplied = override.get("content-type")
        if (
            supplied is not None
            and declared is not None
            and supplied.split(";", 1)[0].strip().lower() != declared
        ):
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

    # ------------------------------------------------------- контекст-менеджер

    async def __aenter__(self) -> ContractMock:
        if self._entered:
            raise ContractMockError(
                "ContractMock нельзя войти повторно: создайте новый мок",
                operation_key=self._operation.key,
            )
        self._entered = True
        headers = dict(self._response_headers)
        if self._variant.content_type and not any(key.lower() == "content-type" for key in headers):
            headers["Content-Type"] = self._variant.content_type
        matcher = backend.build_matcher(
            method=self._operation.method,
            path=self._resolved_path(),
            params=self._query_params or None,
            headers=self._headers or None,
        )
        response = backend.build_response(
            status=self._status,
            body=self._body,
            has_body=self._variant.json_schema is not None,
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

        self._history = tuple(self._mocked.history or ())

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
    def history(self) -> tuple[Any, ...]:
        """История перехваченных обменов. Доступна после выхода из блока."""
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
    note = f"[openapi-contract-fixtures] {type(secondary).__name__}: {secondary}"
    add_note = getattr(target, "add_note", None)
    if add_note is not None:
        add_note(note)
        return
    notes = getattr(target, "__notes__", None)
    if isinstance(notes, list):
        notes.append(note)
    else:
        target.__notes__ = [note]  # type: ignore[attr-defined]

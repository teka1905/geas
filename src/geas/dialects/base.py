"""Реестр диалектов и общие помощники адаптеров.

Диалект отвечает ровно за одно: привести свой документ к
:class:`~geas.normalization.raw.RawOperation`. Всё, что ниже —
нормализация схем, JSON Schema, d42, runtime, CLI — про версию спецификации уже
не знает. Добавление OpenAPI 3.1 — это новый модуль и одна строка в
:data:`DIALECTS`.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..errors import SpecLoadError, UnsupportedSpecVersionError
from ..models import Origin, ParameterLocation
from ..normalization.raw import RawOperation
from ..normalization.refs import SpecRegistry
from ..normalization.schemas import SchemaDialectConfig

__all__ = [
    "DIALECTS",
    "HTTP_METHODS",
    "Dialect",
    "detect_dialect",
    "merge_parameters",
]

#: Методы, которые считаются операциями. ``trace`` есть только в OpenAPI 3.
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


class Dialect(Protocol):
    """Адаптер одного диалекта спецификации."""

    #: Имя диалекта для сообщений и артефактов.
    name: str
    #: Настройки разбора Schema Object.
    schema_config: SchemaDialectConfig

    def operations(
        self, registry: SpecRegistry, *, base_path: str | None
    ) -> tuple[RawOperation, ...]:
        """Перечислить все операции документа в детерминированном порядке."""
        ...

    def default_base_path(self, document: Any) -> str:
        """Префикс маршрута, диктуемый самим документом."""
        ...


def merge_parameters(
    shared: list[tuple[str, str, Any]], own: list[tuple[str, str, Any]]
) -> list[Any]:
    """Слить параметры уровня пути и уровня операции.

    Ключ перекрытия — пара ``(name, in)``, как требует спецификация: параметр
    операции вытесняет одноимённый параметр пути.
    """
    merged: dict[tuple[str, str], Any] = {}
    for name, location, raw in shared:
        merged[(name, location)] = raw
    for name, location, raw in own:
        merged[(name, location)] = raw
    return [merged[key] for key in sorted(merged)]


def parse_location(raw: Any, *, origin: Origin) -> ParameterLocation:
    """Разобрать ``in`` параметра."""
    try:
        return ParameterLocation(raw)
    except ValueError as exc:
        raise SpecLoadError(
            f"параметр с in={raw!r} не поддерживается",
            source=origin.source,
            json_pointer=origin.pointer,
        ) from exc


def detect_dialect(document: Any, *, source: str) -> Dialect:
    """Определить диалект по документу.

    OpenAPI 3.1 намеренно **не** считается совместимым с 3.0: в 3.1 ``nullable``
    удалён, ``exclusiveMinimum`` стал числовым, ``type`` может быть массивом, а
    Schema Object — это полноценная JSON Schema 2020-12. Разобрать 3.1 правилами
    3.0 значит тихо потерять ``null`` в типах и неверно прочитать границы.
    """
    from .openapi30 import OpenApi30Dialect
    from .swagger2 import Swagger2Dialect

    if not isinstance(document, dict):
        raise SpecLoadError("корень спецификации должен быть объектом", source=source)

    swagger = document.get("swagger")
    openapi = document.get("openapi")

    if isinstance(swagger, str):
        if swagger.startswith("2.0"):
            return Swagger2Dialect(document)
        raise UnsupportedSpecVersionError(
            f"Swagger {swagger!r} не поддерживается; поддержана только версия 2.0", source=source
        )

    if isinstance(openapi, str):
        if openapi.startswith("3.0."):
            return OpenApi30Dialect(document)
        if openapi.startswith("3.1."):
            raise UnsupportedSpecVersionError(
                f"OpenAPI {openapi} не поддерживается в версии 0.1.\n"
                f"OpenAPI 3.1 не является надмножеством 3.0: в нём удалён 'nullable', "
                f"'exclusiveMinimum'/'exclusiveMaximum' стали числовыми, 'type' может быть "
                f"массивом, появились булевы схемы и '$defs'. Разбирать 3.1 правилами 3.0 "
                f"значит молча потерять контракт, поэтому библиотека отказывается это делать.\n"
                f"Адаптер 3.1 добавляется отдельно (dialects/openapi31.py) без изменений в ядре.",
                source=source,
            )
        raise UnsupportedSpecVersionError(
            f"OpenAPI {openapi!r} не поддерживается; поддержаны 3.0.x и Swagger 2.0", source=source
        )

    raise UnsupportedSpecVersionError(
        "не удалось определить версию: в корне нет ни 'swagger', ни 'openapi'", source=source
    )


#: Порядок важен только для документации: детект идёт по явным ключам версии.
DIALECTS: tuple[str, ...] = ("swagger2", "openapi30")

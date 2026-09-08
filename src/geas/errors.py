"""Иерархия исключений библиотеки.

Единственный базовый класс — :class:`ContractError`. Любая ошибка контракта несёт
четыре координаты, по которым её можно найти в исходной спецификации:

* ``source`` — идентификатор источника из manifest;
* ``operation_key`` — стабильный ключ операции;
* ``direction`` — ``request`` или ``response``;
* ``json_pointer`` — точный JSON Pointer внутри документа-источника.

Координаты необязательны по отдельности (например, ошибка manifest не привязана к
направлению), но всё, что известно на момент выброса, обязано попасть в сообщение.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ArtifactDriftError",
    "ArtifactError",
    "ContractError",
    "ContractMockError",
    "ContractOverlayError",
    "ManifestBindingError",
    "ManifestError",
    "MissingExtraError",
    "NamespaceCollisionError",
    "OperationLookupError",
    "RecursiveSchemaError",
    "RefResolutionError",
    "RequestContractError",
    "ResponseContractError",
    "ResponseVariantError",
    "SpecLoadError",
    "UnsupportedConstructError",
    "UnsupportedSpecVersionError",
    "ValidationFailedError",
    "WaiverError",
]


def _format_location(**coordinates: str | None) -> str:
    """Собрать человекочитаемый хвост сообщения из известных координат."""
    parts = [f"{name}={value or '/'}" for name, value in coordinates.items() if value is not None]
    return "" if not parts else " [" + ", ".join(parts) + "]"


class ContractError(Exception):
    """Базовая ошибка библиотеки.

    Все прикладные исключения наследуются отсюда, поэтому потребитель может
    поймать одним ``except ContractError``.
    """

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        operation_key: str | None = None,
        direction: str | None = None,
        json_pointer: str | None = None,
        schema_pointer: str | None = None,
    ) -> None:
        self.base_message = message
        self.source = source
        self.operation_key = operation_key
        self.direction = direction
        self.json_pointer = json_pointer
        self.schema_pointer = schema_pointer
        suffix = _format_location(
            source=source,
            operation=operation_key,
            direction=direction,
            pointer=json_pointer,
            schema_pointer=schema_pointer,
        )
        super().__init__(message + suffix)

    def as_dict(self) -> dict[str, Any]:
        """Машиночитаемое представление ошибки (для CLI ``--json`` и отчётов)."""
        return {
            "type": type(self).__name__,
            "message": self.base_message,
            "source": self.source,
            "operation": self.operation_key,
            "direction": self.direction,
            "json_pointer": self.json_pointer,
            "schema_pointer": self.schema_pointer,
        }


class ManifestError(ContractError):
    """Manifest невалиден: неизвестное поле, битый тип, отсутствующий источник."""


class ManifestBindingError(ContractError):
    """Manifest больше не соответствует спецификации.

    Изменились ``operationId``, метод, маршрут, request content type, статус или
    response content type — то есть привязка операции разорвана.
    """


class WaiverError(ContractError):
    """Проблема с waiver: истёк, неполон, не использован, избыточен или устарел."""


class UnsupportedSpecVersionError(ContractError):
    """Версия спецификации не поддерживается этой версией библиотеки."""


class SpecLoadError(ContractError):
    """Файл спецификации не читается, не парсится или запрещён политикой доступа."""


class RefResolutionError(ContractError):
    """``$ref`` не разрешается: битый указатель, внешний URL, выход за root."""


class UnsupportedConstructError(ContractError):
    """Конструкция OpenAPI распознана, но не может быть представлена точно.

    Выбрасывается вместо любого lossy fallback. Сообщение обязано называть
    конкретный keyword и точный JSON Pointer.
    """


class RecursiveSchemaError(UnsupportedConstructError):
    """Схема рекурсивна. d42 не умеет выражать рекурсию — конструкция отклоняется."""


class NamespaceCollisionError(ContractError):
    """Конфликт имён в generated Python namespace."""


class ArtifactError(ContractError):
    """Ошибка записи, чтения или валидации generated-артефактов."""


class ArtifactDriftError(ArtifactError):
    """Generated-артефакты в рабочем дереве расходятся с тем, что даёт генератор."""

    def __init__(self, message: str, *, report: Any = None) -> None:
        super().__init__(message)
        self.report = report


class OperationLookupError(ContractError):
    """В реестре нет операции с таким ключом или python path."""


class ResponseVariantError(ContractError):
    """Response-вариант не выбран однозначно.

    Либо у операции несколько ``status``/``content_type`` и вызов не указал, какой
    нужен, либо указанного варианта нет в контракте.
    """


class ValidationFailedError(ContractError):
    """Данные не соответствуют контракту."""

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        operation_key: str | None = None,
        direction: str | None = None,
        json_pointer: str | None = None,
        schema_pointer: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
        validator: str | None = None,
    ) -> None:
        self.expected = expected
        self.actual = actual
        self.validator = validator
        detail = ""
        if expected is not None:
            detail += f"\n  ожидалось: {expected}"
        if actual is not None:
            detail += f"\n  фактически: {actual}"
        super().__init__(
            message + detail,
            source=source,
            operation_key=operation_key,
            direction=direction,
            json_pointer=json_pointer,
            schema_pointer=schema_pointer,
        )

    def as_dict(self) -> dict[str, Any]:
        data = super().as_dict()
        data.update({"expected": self.expected, "actual": self.actual, "rule": self.validator})
        return data


class RequestContractError(ValidationFailedError):
    """Перехваченный запрос не соответствует контракту операции."""

    def __init__(self, message: str, *, request_index: int | None = None, **kwargs: Any) -> None:
        self.request_index = request_index
        prefix = "" if request_index is None else f"request #{request_index}: "
        super().__init__(prefix + message, direction=kwargs.pop("direction", "request"), **kwargs)

    def as_dict(self) -> dict[str, Any]:
        data = super().as_dict()
        data["request_index"] = self.request_index
        return data


class ResponseContractError(ValidationFailedError):
    """Тело ответа мока не соответствует контракту операции."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, direction=kwargs.pop("direction", "response"), **kwargs)


class ContractOverlayError(ContractError):
    """Overlay не применяется: путь отсутствует, переименован или несовместим."""


class MissingExtraError(ContractError, ImportError):
    """Требуется опциональная интеграция, которая не установлена."""

    def __init__(self, extra: str, feature: str) -> None:
        self.extra = extra
        self.feature = feature
        super().__init__(
            f"{feature} требует опциональной зависимости. Установите: pip install 'geas[{extra}]'"
        )


class ContractMockError(ContractError):
    """Некорректное использование ``OperationHandle.mock()``."""

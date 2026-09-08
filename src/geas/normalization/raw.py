"""Диалектно-нейтральная форма сырой операции.

Адаптер диалекта обязан привести свой документ к этим типам. Ниже по стеку про
Swagger 2.0 и OpenAPI 3.0 уже никто не знает: различия (``in: body`` против
``requestBody``, параметр с ``type`` против параметра со ``schema``,
``collectionFormat`` против ``style``/``explode``, ``x-nullable`` против
``nullable``) исчезают ровно здесь.

Схемы остаются **сырыми** Schema Object'ами: их нормализует
:class:`~geas.normalization.schemas.SchemaNormalizer`, отдельно для
каждого направления.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Origin, ParameterLocation

__all__ = [
    "RawBody",
    "RawOperation",
    "RawParameter",
    "RawResponse",
    "is_json_media_type",
    "normalize_media_type",
]


def normalize_media_type(value: str) -> str:
    """Привести media type к канонической форме.

    Реальные спецификации массово пишут ``application/json;charset=UTF-8`` в
    разном регистре. Параметры отбрасываются, регистр приводится к нижнему —
    иначе сравнение content type просто никогда не совпадает.
    """
    return value.split(";", 1)[0].strip().lower()


def is_json_media_type(value: str) -> bool:
    """Является ли media type JSON-совместимым."""
    canonical = normalize_media_type(value)
    return canonical == "application/json" or canonical.endswith("+json")


@dataclass(frozen=True, kw_only=True, slots=True)
class RawParameter:
    """Параметр запроса или заголовок ответа до нормализации схемы."""

    name: str
    location: ParameterLocation
    required: bool
    #: Сырой Schema Object параметра.
    schema: Any
    style: str
    explode: bool
    origin: Origin


@dataclass(frozen=True, kw_only=True, slots=True)
class RawBody:
    """Тело запроса до нормализации схемы."""

    content_type: str
    schema: Any
    required: bool
    origin: Origin
    #: Причина, по которой вариант не представим контрактом (например бинарный
    #: media type). ``None`` — вариант поддержан.
    unsupported_reason: str | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class RawResponse:
    """Один вариант ответа до нормализации схемы."""

    status: int | str
    content_type: str | None
    #: ``None`` означает ответ без тела (например 204).
    schema: Any
    headers: tuple[RawParameter, ...]
    origin: Origin
    #: Причина, по которой вариант не может быть представлен контрактом
    #: (например бинарный media type). ``None`` — вариант поддержан.
    unsupported_reason: str | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class RawOperation:
    """Операция целиком, приведённая к нейтральной форме."""

    operation_id: str | None
    method: str
    path: str
    parameters: tuple[RawParameter, ...]
    bodies: tuple[RawBody, ...]
    responses: tuple[RawResponse, ...]
    origin: Origin
    #: Документ, относительно которого разрешаются ``$ref`` этой операции.
    document_path: Path

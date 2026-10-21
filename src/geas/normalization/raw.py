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

from ..media import is_json_media_type, normalize_media_type
from ..models import Origin, ParameterLocation

__all__ = [
    "RawBody",
    "RawOperation",
    "RawParameter",
    "RawResponse",
    "is_json_media_type",
    "normalize_media_type",
]


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
    #: Media type взят из общего ``consumes`` корня документа Swagger 2.0, а не объявлен
    #: операцией для своего тела.
    shared_media_type: bool = False
    #: Вариант, который прежние версии в контракт не брали (``*/*`` в OpenAPI 3.0,
    #: ``text/json``): ошибка его схемы у незакреплённого варианта не валит генерацию.
    lenient: bool = False


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
    #: Media type взят из общего списка (``produces`` Swagger 2.0), а не объявлен для
    #: этого статуса: схема статуса одна на все типы списка.
    shared_media_type: bool = False
    #: См. :attr:`RawBody.lenient`.
    lenient: bool = False


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
    #: Почему маршрут не удалось отобразить правилом ``strip_prefix`` источника.
    #: ``None`` — маршрут итоговый. Операцию с причиной нельзя выбирать.
    route_error: str | None = None

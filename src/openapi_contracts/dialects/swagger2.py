"""Адаптер Swagger / OpenAPI 2.0.

Отличия, которые исчезают здесь и дальше не видны:

* тело запроса приходит как параметр ``in: body``, а не как ``requestBody``;
* content type берётся из ``consumes``/``produces``, а не из ``content``;
* параметры несут ``type``/``format``/``items`` прямо на себе, без ``schema``;
* массивы сериализуются через ``collectionFormat``, а не ``style``/``explode``;
* nullable выражается расширением ``x-nullable``;
* именованные схемы лежат в ``definitions``, а не в ``components/schemas``;
* маршрут операции — это ``basePath`` плюс шаблон пути.
"""

from __future__ import annotations

from typing import Any

from ..errors import SpecLoadError, UnsupportedConstructError
from ..models import Origin, ParameterLocation
from ..normalization.raw import (
    RawBody,
    RawOperation,
    RawParameter,
    RawResponse,
    is_json_media_type,
    normalize_media_type,
)
from ..normalization.refs import SpecRegistry
from ..normalization.schemas import SchemaDialectConfig
from .base import HTTP_METHODS, merge_parameters, parse_location

__all__ = ["Swagger2Dialect"]

_SWAGGER_METHODS = tuple(method for method in HTTP_METHODS if method != "trace")

#: Ключи Items/Parameter Object, которые переносятся в Schema Object как есть.
_INLINE_SCHEMA_KEYS = (
    "type",
    "format",
    "enum",
    "items",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "x-nullable",
)

#: ``collectionFormat`` → пара ``(style, explode)``.
_COLLECTION_FORMATS: dict[str, tuple[str, bool]] = {
    "csv": ("form", False),
    "ssv": ("spaceDelimited", False),
    "pipes": ("pipeDelimited", False),
    "multi": ("form", True),
}


class Swagger2Dialect:
    """Приводит документ Swagger 2.0 к нейтральной форме."""

    name = "swagger2"
    schema_config = SchemaDialectConfig(
        nullable_key="x-nullable",
        schema_ref_prefix="#/definitions/",
        supports_write_only=False,
    )

    def __init__(self, document: Any) -> None:
        self._document = document

    def default_base_path(self, document: Any) -> str:
        """``basePath`` — часть маршрута по определению спецификации 2.0."""
        raw = document.get("basePath") or ""
        if not isinstance(raw, str):
            raise SpecLoadError("basePath должен быть строкой")
        return raw.rstrip("/")

    # ------------------------------------------------------------ операции

    def operations(
        self, registry: SpecRegistry, *, base_path: str | None
    ) -> tuple[RawOperation, ...]:
        document = registry.entry_document()
        prefix = self.default_base_path(document) if base_path is None else base_path.rstrip("/")
        source = registry.document_id(registry.entry_path)
        root = Origin(source=source, pointer="")
        root_consumes = _media_types(document.get("consumes"), ["application/json"])
        root_produces = _media_types(document.get("produces"), ["application/json"])

        paths = document.get("paths") or {}
        if not isinstance(paths, dict):
            raise SpecLoadError("paths должен быть объектом", source=source)

        collected: list[RawOperation] = []
        for template in sorted(paths):
            item = paths[template]
            item_origin = root.child("paths", template)
            if not isinstance(item, dict):
                raise SpecLoadError(
                    "Path Item должен быть объектом",
                    source=source,
                    json_pointer=item_origin.pointer,
                )
            shared = _indexed_parameters(item.get("parameters"), registry, item_origin)
            for method in _SWAGGER_METHODS:
                operation = item.get(method)
                if operation is None:
                    continue
                op_origin = item_origin.child(method)
                if not isinstance(operation, dict):
                    raise SpecLoadError(
                        "Operation должен быть объектом",
                        source=source,
                        json_pointer=op_origin.pointer,
                    )
                own = _indexed_parameters(operation.get("parameters"), registry, op_origin)
                merged = merge_parameters(shared, own)
                consumes = _media_types(operation.get("consumes"), root_consumes)
                produces = _media_types(operation.get("produces"), root_produces)
                parameters, bodies = self._split_parameters(merged, registry, op_origin, consumes)
                collected.append(
                    RawOperation(
                        operation_id=operation.get("operationId"),
                        method=method.upper(),
                        path=f"{prefix}{template}",
                        parameters=parameters,
                        bodies=bodies,
                        responses=self._responses(operation, registry, op_origin, produces),
                        origin=op_origin,
                        document_path=registry.entry_path,
                    )
                )
        return tuple(collected)

    # ---------------------------------------------------------- параметры

    def _split_parameters(
        self,
        raw_parameters: list[tuple[Any, Origin]],
        registry: SpecRegistry,
        op_origin: Origin,
        consumes: list[str],
    ) -> tuple[tuple[RawParameter, ...], tuple[RawBody, ...]]:
        parameters: list[RawParameter] = []
        bodies: list[RawBody] = []
        for raw, origin in raw_parameters:
            location_raw = raw.get("in")
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                raise SpecLoadError(
                    "у параметра нет имени", source=origin.source, json_pointer=origin.pointer
                )
            if location_raw == "body":
                schema = raw.get("schema")
                if schema is None:
                    raise SpecLoadError(
                        f"body-параметр {name!r} без schema",
                        source=origin.source,
                        json_pointer=origin.pointer,
                    )
                required = bool(raw.get("required", False))
                for media_type in consumes:
                    reason = None if _schema_bearing(media_type) else _media_reason(media_type)
                    bodies.append(
                        RawBody(
                            content_type=media_type,
                            schema=schema,
                            required=required,
                            origin=origin.child("schema"),
                            unsupported_reason=reason,
                        )
                    )
                continue
            if location_raw == "formData":
                raise UnsupportedConstructError(
                    f"параметр {name!r} с in=formData не поддерживается: "
                    f"form-кодированные тела не имеют JSON-контракта",
                    source=origin.source,
                    json_pointer=origin.pointer,
                )
            location = parse_location(location_raw, origin=origin)
            parameters.append(_build_parameter(raw, name=name, location=location, origin=origin))
        parameters.sort(key=lambda item: (item.location.value, item.name))
        bodies.sort(key=lambda item: item.content_type)
        return tuple(parameters), tuple(bodies)

    # ------------------------------------------------------------- ответы

    def _responses(
        self,
        operation: dict[str, Any],
        registry: SpecRegistry,
        op_origin: Origin,
        produces: list[str],
    ) -> tuple[RawResponse, ...]:
        raw_responses = operation.get("responses") or {}
        if not isinstance(raw_responses, dict):
            raise SpecLoadError(
                "responses должен быть объектом",
                source=op_origin.source,
                json_pointer=op_origin.pointer,
            )
        collected: list[RawResponse] = []
        for raw_status in sorted(raw_responses, key=str):
            response = raw_responses[raw_status]
            origin = op_origin.child("responses", raw_status)
            if isinstance(response, dict) and "$ref" in response:
                resolved = registry.resolve(
                    response["$ref"], base=registry.entry_path, origin=origin
                )
                response, origin = resolved.value, resolved.origin
            if not isinstance(response, dict):
                raise SpecLoadError(
                    "Response должен быть объектом",
                    source=origin.source,
                    json_pointer=origin.pointer,
                )
            status = _parse_status(raw_status, origin)
            headers = _response_headers(response.get("headers"), origin)
            schema = response.get("schema")
            if schema is None:
                collected.append(
                    RawResponse(
                        status=status,
                        content_type=None,
                        schema=None,
                        headers=headers,
                        origin=origin,
                    )
                )
                continue
            for media_type in produces:
                reason = None if _schema_bearing(media_type) else _media_reason(media_type)
                collected.append(
                    RawResponse(
                        status=status,
                        content_type=media_type,
                        schema=schema,
                        headers=headers,
                        origin=origin.child("schema"),
                        unsupported_reason=reason,
                    )
                )
        collected.sort(key=lambda item: (str(item.status), item.content_type or ""))
        return tuple(collected)


# ----------------------------------------------------------------- помощники


def _schema_bearing(media_type: str) -> bool:
    """Может ли media type нести JSON-контракт.

    ``*/*`` в Swagger 2.0 массово генерируется серверными фреймворками для
    JSON-ответов, поэтому считается JSON-совместимым.
    """
    return is_json_media_type(media_type) or media_type == "*/*"


def _media_reason(media_type: str) -> str:
    return (
        f"media type {media_type!r} не является JSON-совместимым, "
        f"структурный контракт тела для него не строится"
    )


def _media_types(raw: Any, fallback: list[str]) -> list[str]:
    if raw is None:
        return list(fallback)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise SpecLoadError("consumes/produces должны быть списком строк")
    if not raw:
        return list(fallback)
    return sorted({normalize_media_type(item) for item in raw})


def _parse_status(raw: Any, origin: Origin) -> int | str:
    text = str(raw)
    if text == "default":
        return "default"
    if text.isdigit():
        return int(text)
    raise UnsupportedConstructError(
        f"код ответа {text!r} не поддержан: библиотека работает с конкретными статусами "
        f"и с 'default', но не с диапазонами вида '2XX'",
        source=origin.source,
        json_pointer=origin.pointer,
    )


def _indexed_parameters(
    raw: Any, registry: SpecRegistry, origin: Origin
) -> list[tuple[str, str, tuple[Any, Origin]]]:
    """Разрешить ``$ref`` параметров и вернуть их с ключом ``(name, in)``."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecLoadError(
            "parameters должен быть списком", source=origin.source, json_pointer=origin.pointer
        )
    indexed: list[tuple[str, str, tuple[Any, Origin]]] = []
    for index, item in enumerate(raw):
        item_origin = origin.child("parameters", index)
        if isinstance(item, dict) and "$ref" in item:
            resolved = registry.resolve(item["$ref"], base=registry.entry_path, origin=item_origin)
            item, item_origin = resolved.value, resolved.origin
        if not isinstance(item, dict):
            raise SpecLoadError(
                "Parameter должен быть объектом",
                source=item_origin.source,
                json_pointer=item_origin.pointer,
            )
        indexed.append((str(item.get("name")), str(item.get("in")), (item, item_origin)))
    return indexed


def _build_parameter(
    raw: dict[str, Any], *, name: str, location: ParameterLocation, origin: Origin
) -> RawParameter:
    """Собрать Schema Object из полей, лежащих прямо на параметре."""
    schema = {key: raw[key] for key in _INLINE_SCHEMA_KEYS if key in raw}
    if not schema:
        raise UnsupportedConstructError(
            f"параметр {name!r} не описывает тип",
            source=origin.source,
            json_pointer=origin.pointer,
        )
    collection_format = raw.get("collectionFormat", "csv")
    if schema.get("type") == "array":
        if collection_format not in _COLLECTION_FORMATS:
            raise UnsupportedConstructError(
                f"параметр {name!r}: collectionFormat={collection_format!r} не поддержан; "
                f"известные: {sorted(_COLLECTION_FORMATS)}",
                source=origin.source,
                json_pointer=origin.pointer,
            )
        style, explode = _COLLECTION_FORMATS[collection_format]
        if location is not ParameterLocation.QUERY and style != "form":
            raise UnsupportedConstructError(
                f"параметр {name!r}: collectionFormat={collection_format!r} применим только "
                f"к query-параметрам",
                source=origin.source,
                json_pointer=origin.pointer,
            )
        if location is not ParameterLocation.QUERY:
            style, explode = "simple", False
    else:
        style = "form" if location is ParameterLocation.QUERY else "simple"
        explode = location is ParameterLocation.QUERY
    return RawParameter(
        name=name,
        location=location,
        required=bool(raw.get("required", location is ParameterLocation.PATH)),
        schema=schema,
        style=style,
        explode=explode,
        origin=origin,
    )


def _response_headers(raw: Any, origin: Origin) -> tuple[RawParameter, ...]:
    """Заголовки ответа. Пустой объект ``headers: {}`` — это отсутствие заголовков."""
    if not raw:
        return ()
    if not isinstance(raw, dict):
        raise SpecLoadError(
            "responses.<code>.headers должен быть объектом",
            source=origin.source,
            json_pointer=origin.pointer,
        )
    headers: list[RawParameter] = []
    for name in sorted(raw):
        header_origin = origin.child("headers", name)
        header = raw[name]
        if not isinstance(header, dict):
            raise SpecLoadError(
                "Header должен быть объектом",
                source=header_origin.source,
                json_pointer=header_origin.pointer,
            )
        headers.append(
            _build_parameter(
                header, name=name, location=ParameterLocation.HEADER, origin=header_origin
            )
        )
    return tuple(headers)

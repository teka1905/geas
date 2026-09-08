"""Адаптер OpenAPI 3.0.x.

Поддержано и покрыто тестами: ``components/schemas``, ``$ref`` на schemas,
parameters, requestBodies, responses и headers; ``requestBody.content``;
``responses.*.content``; параметры в path/query/header/cookie; ``nullable``;
``readOnly``/``writeOnly``; ``allOf``/``oneOf``/``anyOf``; ``discriminator``;
булев и типизированный ``additionalProperties``; enum; pattern; formats;
minimum/maximum; границы длины; массивы; ``style``/``explode``.

Маршрут берётся из шаблона пути как есть. ``servers[].url`` — это адрес
развёртывания, он различается между окружениями и в маршрут не подмешивается;
если префикс всё-таки нужен, он задаётся явно через ``sources.<name>.base_path``.
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

__all__ = ["OpenApi30Dialect"]

_SUPPORTED_STYLES = frozenset(
    {"simple", "form", "spaceDelimited", "pipeDelimited", "label", "matrix", "deepObject"}
)


class OpenApi30Dialect:
    """Приводит документ OpenAPI 3.0.x к нейтральной форме."""

    name = "openapi30"
    schema_config = SchemaDialectConfig(
        nullable_key="nullable",
        schema_ref_prefix="#/components/schemas/",
        supports_write_only=True,
    )

    def __init__(self, document: Any) -> None:
        self._document = document

    def default_base_path(self, document: Any) -> str:
        """OpenAPI 3.0 не диктует префикс маршрута."""
        return ""

    def operations(
        self, registry: SpecRegistry, *, base_path: str | None
    ) -> tuple[RawOperation, ...]:
        document = registry.entry_document()
        prefix = (base_path or "").rstrip("/")
        source = registry.document_id(registry.entry_path)
        root = Origin(source=source, pointer="")

        paths = document.get("paths") or {}
        if not isinstance(paths, dict):
            raise SpecLoadError("paths должен быть объектом", source=source)

        collected: list[RawOperation] = []
        for template in sorted(paths):
            item = paths[template]
            item_origin = root.child("paths", template)
            if isinstance(item, dict) and "$ref" in item:
                resolved = registry.resolve(
                    item["$ref"], base=registry.entry_path, origin=item_origin
                )
                item, item_origin = resolved.value, resolved.origin
            if not isinstance(item, dict):
                raise SpecLoadError(
                    "Path Item должен быть объектом",
                    source=source,
                    json_pointer=item_origin.pointer,
                )
            shared = _indexed_parameters(item.get("parameters"), registry, item_origin)
            for method in HTTP_METHODS:
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
                collected.append(
                    RawOperation(
                        operation_id=operation.get("operationId"),
                        method=method.upper(),
                        path=f"{prefix}{template}",
                        parameters=tuple(
                            sorted(
                                (_build_parameter(raw, origin) for raw, origin in merged),
                                key=lambda item: (item.location.value, item.name),
                            )
                        ),
                        bodies=_request_bodies(operation, registry, op_origin),
                        responses=_responses(operation, registry, op_origin),
                        origin=op_origin,
                        document_path=registry.entry_path,
                    )
                )
        return tuple(collected)


# ----------------------------------------------------------------- помощники


def _media_reason(media_type: str) -> str:
    return (
        f"media type {media_type!r} не является JSON-совместимым, "
        f"структурный контракт тела для него не строится"
    )


def _indexed_parameters(
    raw: Any, registry: SpecRegistry, origin: Origin
) -> list[tuple[str, str, tuple[Any, Origin]]]:
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


def _build_parameter(raw: dict[str, Any], origin: Origin) -> RawParameter:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise SpecLoadError(
            "у параметра нет имени", source=origin.source, json_pointer=origin.pointer
        )
    location = parse_location(raw.get("in"), origin=origin)
    if "content" in raw:
        raise UnsupportedConstructError(
            f"параметр {name!r} описан через 'content'; поддержан только 'schema'",
            source=origin.source,
            json_pointer=origin.pointer,
        )
    schema = raw.get("schema")
    if schema is None:
        raise SpecLoadError(
            f"у параметра {name!r} нет schema", source=origin.source, json_pointer=origin.pointer
        )
    style = raw.get("style") or _default_style(location)
    if not isinstance(style, str) or style not in _SUPPORTED_STYLES:
        raise UnsupportedConstructError(
            f"параметр {name!r}: неизвестный style={style!r}",
            source=origin.source,
            json_pointer=origin.pointer,
        )
    explode = raw.get("explode")
    if explode is None:
        explode = style == "form"
    if not isinstance(explode, bool):
        raise SpecLoadError(
            f"параметр {name!r}: explode должен быть булевым",
            source=origin.source,
            json_pointer=origin.pointer,
        )
    if raw.get("allowReserved"):
        raise UnsupportedConstructError(
            f"параметр {name!r}: allowReserved меняет правила процентного кодирования "
            f"и не поддержан",
            source=origin.source,
            json_pointer=origin.pointer,
        )
    return RawParameter(
        name=name,
        location=location,
        required=bool(raw.get("required", location is ParameterLocation.PATH)),
        schema=schema,
        style=style,
        explode=explode,
        origin=origin,
    )


def _default_style(location: ParameterLocation) -> str:
    if location in (ParameterLocation.QUERY, ParameterLocation.COOKIE):
        return "form"
    return "simple"


def _request_bodies(
    operation: dict[str, Any], registry: SpecRegistry, op_origin: Origin
) -> tuple[RawBody, ...]:
    raw = operation.get("requestBody")
    if raw is None:
        return ()
    origin = op_origin.child("requestBody")
    if isinstance(raw, dict) and "$ref" in raw:
        resolved = registry.resolve(raw["$ref"], base=registry.entry_path, origin=origin)
        raw, origin = resolved.value, resolved.origin
    if not isinstance(raw, dict):
        raise SpecLoadError(
            "requestBody должен быть объектом", source=origin.source, json_pointer=origin.pointer
        )
    required = bool(raw.get("required", False))
    content = raw.get("content") or {}
    if not isinstance(content, dict):
        raise SpecLoadError(
            "requestBody.content должен быть объектом",
            source=origin.source,
            json_pointer=origin.pointer,
        )
    bodies: list[RawBody] = []
    for media_type in sorted(content):
        media_origin = origin.child("content", media_type)
        media = content[media_type]
        if not isinstance(media, dict):
            raise SpecLoadError(
                "Media Type Object должен быть объектом",
                source=media_origin.source,
                json_pointer=media_origin.pointer,
            )
        canonical = normalize_media_type(media_type)
        schema = media.get("schema")
        if schema is None:
            reason = "у media type нет schema — структурный контракт тела неизвестен"
        elif not is_json_media_type(canonical):
            reason = _media_reason(canonical)
        else:
            reason = None
        bodies.append(
            RawBody(
                content_type=canonical,
                schema=schema,
                required=required,
                origin=media_origin.child("schema"),
                unsupported_reason=reason,
            )
        )
    bodies.sort(key=lambda item: item.content_type)
    return tuple(bodies)


def _responses(
    operation: dict[str, Any], registry: SpecRegistry, op_origin: Origin
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
            resolved = registry.resolve(response["$ref"], base=registry.entry_path, origin=origin)
            response, origin = resolved.value, resolved.origin
        if not isinstance(response, dict):
            raise SpecLoadError(
                "Response должен быть объектом", source=origin.source, json_pointer=origin.pointer
            )
        status = _parse_status(raw_status, origin)
        headers = _response_headers(response.get("headers"), registry, origin)
        content = response.get("content") or {}
        if not isinstance(content, dict):
            raise SpecLoadError(
                "responses.<code>.content должен быть объектом",
                source=origin.source,
                json_pointer=origin.pointer,
            )
        if not content:
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
        for media_type in sorted(content):
            media_origin = origin.child("content", media_type)
            media = content[media_type]
            if not isinstance(media, dict):
                raise SpecLoadError(
                    "Media Type Object должен быть объектом",
                    source=media_origin.source,
                    json_pointer=media_origin.pointer,
                )
            canonical = normalize_media_type(media_type)
            schema = media.get("schema")
            if schema is None:
                reason: str | None = "у media type нет schema — контракт тела неизвестен"
            elif not is_json_media_type(canonical):
                reason = _media_reason(canonical)
            else:
                reason = None
            collected.append(
                RawResponse(
                    status=status,
                    content_type=canonical,
                    schema=schema,
                    headers=headers,
                    origin=media_origin.child("schema"),
                    unsupported_reason=reason,
                )
            )
    collected.sort(key=lambda item: (str(item.status), item.content_type or ""))
    return tuple(collected)


def _response_headers(raw: Any, registry: SpecRegistry, origin: Origin) -> tuple[RawParameter, ...]:
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
        if isinstance(header, dict) and "$ref" in header:
            resolved = registry.resolve(
                header["$ref"], base=registry.entry_path, origin=header_origin
            )
            header, header_origin = resolved.value, resolved.origin
        if not isinstance(header, dict):
            raise SpecLoadError(
                "Header должен быть объектом",
                source=header_origin.source,
                json_pointer=header_origin.pointer,
            )
        schema = header.get("schema")
        if schema is None:
            raise SpecLoadError(
                f"у заголовка {name!r} нет schema",
                source=header_origin.source,
                json_pointer=header_origin.pointer,
            )
        headers.append(
            RawParameter(
                name=name,
                location=ParameterLocation.HEADER,
                required=bool(header.get("required", False)),
                schema=schema,
                style=header.get("style", "simple"),
                explode=bool(header.get("explode", False)),
                origin=header_origin,
            )
        )
    return tuple(headers)


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

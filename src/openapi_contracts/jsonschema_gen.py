"""IR → JSON Schema Draft 2020-12.

Диалект выбран осознанно (см. ADR 0001): 2020-12 — дефолт ``jsonschema>=4``, у него
есть ``$defs`` и ``if``/``then``, которыми ``discriminator`` выражается как
настоящее ограничение валидации, а не как подсказка генератору.

Schema Object OpenAPI **не** передаётся в валидатор напрямую: Swagger 2.0 и
OpenAPI 3.0 — это подмножество Draft 4 со своими расширениями. Здесь происходит
явная трансляция:

* ``nullable`` → ``"type": [T, "null"]`` либо дополнительный вариант ``{"type": "null"}``;
* булев ``exclusiveMinimum`` уже развёрнут в числовой на этапе нормализации;
* ``oneOf`` остаётся ``oneOf`` — заменять его на ``anyOf`` нельзя;
* ``discriminator`` с ``mapping`` разворачивается в ``allOf`` из ``if``/``then``
  плюс обязательность самого поля-дискриминатора.
"""

from __future__ import annotations

from typing import Any

from .models import (
    INTEGER_FORMAT_BOUNDS,
    AdditionalProperties,
    AllOfNode,
    AnyNode,
    ArrayNode,
    BooleanNode,
    IntegerNode,
    NullNode,
    NumberNode,
    ObjectNode,
    RefNode,
    SchemaNode,
    StringNode,
    UnionKind,
    UnionNode,
)

__all__ = ["JSON_SCHEMA_DIALECT", "node_to_json_schema", "to_json_schema"]

#: Идентификатор диалекта, который проставляется в каждый generated-документ.
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def to_json_schema(
    root: SchemaNode, definitions: tuple[tuple[str, SchemaNode], ...]
) -> dict[str, Any]:
    """Собрать самодостаточный JSON Schema-документ.

    В ``$defs`` попадают только определения, достижимые из корня: бандл каждой
    операции самодостаточен, а лишние определения раздували бы артефакт и его
    отпечаток.

    Ответ без тела (например 204) сюда не попадает вовсе — реестр хранит для него
    ``schema: null``, и валидация тела для такого варианта не выполняется.
    """
    document: dict[str, Any] = {"$schema": JSON_SCHEMA_DIALECT}
    reachable = _reachable_definitions(root, dict(definitions))
    if reachable:
        document["$defs"] = {
            name: node_to_json_schema(reachable[name]) for name in sorted(reachable)
        }
    document.update(node_to_json_schema(root))
    return document


def _reachable_definitions(
    root: SchemaNode, definitions: dict[str, SchemaNode]
) -> dict[str, SchemaNode]:
    """Собрать определения, достижимые из корня, включая взаимные ссылки."""
    from .models import iter_nodes

    reachable: dict[str, SchemaNode] = {}
    pending = [root]
    while pending:
        for node in iter_nodes(pending.pop()):
            if isinstance(node, RefNode) and node.name not in reachable:
                target = definitions.get(node.name)
                if target is None:
                    raise KeyError(f"определение {node.name!r} отсутствует в бандле")
                reachable[node.name] = target
                pending.append(target)
    return reachable


def node_to_json_schema(node: SchemaNode) -> dict[str, Any]:
    """Отрендерить один узел IR."""
    if isinstance(node, RefNode):
        ref = {"$ref": f"#/$defs/{node.name}"}
        return _nullable_wrap(ref, node.nullable)
    if isinstance(node, AnyNode):
        # Узел появляется только по явному waiver, поэтому «принимает что угодно»
        # здесь — это задокументированное решение, а не молчаливый fallback.
        # ``{}`` в JSON Schema принимает и ``null`` тоже, поэтому nullable здесь
        # ничего не добавляет.
        return {}
    if isinstance(node, NullNode):
        return {"type": "null"}
    if isinstance(node, BooleanNode):
        return _scalar(node, "boolean", {})
    if isinstance(node, StringNode):
        extra: dict[str, Any] = {}
        if node.format is not None:
            extra["format"] = node.format
        if node.pattern is not None:
            extra["pattern"] = node.pattern
        if node.min_length is not None:
            extra["minLength"] = node.min_length
        if node.max_length is not None:
            extra["maxLength"] = node.max_length
        return _scalar(node, "string", extra)
    if isinstance(node, IntegerNode):
        return _scalar(node, "integer", _numeric_bounds(node))
    if isinstance(node, NumberNode):
        return _scalar(node, "number", _numeric_bounds(node))
    if isinstance(node, ArrayNode):
        schema: dict[str, Any] = {"type": _type_of("array", node.nullable)}
        schema["items"] = node_to_json_schema(node.items)
        if node.min_items is not None:
            schema["minItems"] = node.min_items
        if node.max_items is not None:
            schema["maxItems"] = node.max_items
        if node.unique_items:
            schema["uniqueItems"] = True
        return schema
    if isinstance(node, ObjectNode):
        return _object(node)
    if isinstance(node, UnionNode):
        return _union(node)
    if isinstance(node, AllOfNode):
        composed: dict[str, Any] = {"allOf": [node_to_json_schema(part) for part in node.parts]}
        return _nullable_wrap(composed, node.nullable)
    raise TypeError(f"неизвестный узел IR: {type(node).__name__}")


def _type_of(name: str, nullable: bool) -> str | list[str]:
    return [name, "null"] if nullable else name


def _nullable_wrap(schema: dict[str, Any], nullable: bool) -> dict[str, Any]:
    """Обернуть схему, к которой нельзя просто дописать ``"null"`` в ``type``."""
    if not nullable:
        return schema
    return {"anyOf": [schema, {"type": "null"}]}


def _scalar(node: SchemaNode, kind: str, extra: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": _type_of(kind, node.nullable)}
    schema.update(extra)
    enum = getattr(node, "enum", None)
    if enum is not None:
        values = list(enum)
        if node.nullable:
            values.append(None)
        schema["enum"] = values
    return schema


def _numeric_bounds(node: IntegerNode | NumberNode) -> dict[str, Any]:
    bounds: dict[str, Any] = {}
    minimum = node.minimum
    maximum = node.maximum
    if isinstance(node, IntegerNode) and node.format in INTEGER_FORMAT_BOUNDS:
        format_minimum, format_maximum = INTEGER_FORMAT_BOUNDS[node.format]
        minimum = format_minimum if minimum is None else max(minimum, format_minimum)
        maximum = format_maximum if maximum is None else min(maximum, format_maximum)
    if minimum is not None:
        bounds["minimum"] = minimum
    if maximum is not None:
        bounds["maximum"] = maximum
    if node.exclusive_minimum is not None:
        bounds["exclusiveMinimum"] = node.exclusive_minimum
    if node.exclusive_maximum is not None:
        bounds["exclusiveMaximum"] = node.exclusive_maximum
    if node.multiple_of is not None:
        bounds["multipleOf"] = node.multiple_of
    if node.format is not None:
        bounds["format"] = node.format
    return bounds


def _object(node: ObjectNode) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": _type_of("object", node.nullable)}
    if node.properties:
        schema["properties"] = {
            prop.name: node_to_json_schema(prop.schema) for prop in node.properties
        }
    required = list(node.required_names)
    if required:
        schema["required"] = required
    if node.additional_properties is AdditionalProperties.FORBIDDEN:
        schema["additionalProperties"] = False
    elif isinstance(node.additional_properties, SchemaNode):
        schema["additionalProperties"] = node_to_json_schema(node.additional_properties)
    if node.min_properties is not None:
        schema["minProperties"] = node.min_properties
    if node.max_properties is not None:
        schema["maxProperties"] = node.max_properties
    return schema


def _union(node: UnionNode) -> dict[str, Any]:
    keyword = "oneOf" if node.kind is UnionKind.ONE_OF else "anyOf"
    variants = [node_to_json_schema(variant) for variant in node.variants]
    if node.nullable:
        variants.append({"type": "null"})
    schema: dict[str, Any] = {keyword: variants}

    discriminator = node.discriminator
    if discriminator is None:
        return schema

    # Дискриминатор — это ограничение, а не подсказка: поле обязано присутствовать,
    # а при явном mapping каждое его значение обязано соответствовать своей схеме.
    schema["required"] = [discriminator.property_name]
    if discriminator.mapping:
        schema["allOf"] = [
            {
                "if": {
                    "required": [discriminator.property_name],
                    "properties": {discriminator.property_name: {"const": value}},
                },
                "then": {"$ref": f"#/$defs/{target}"},
            }
            for value, target in discriminator.mapping
        ]
    return schema

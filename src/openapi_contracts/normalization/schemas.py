"""Нормализация OpenAPI Schema Object в IR.

Три инварианта, которые держит этот модуль.

**Position-awareness.** Ключевое слово распознаётся только там, где по структуре
Schema Object обязана быть схема. Значения внутри ``properties`` — это имена
пользовательских полей, а не ключевые слова: в реальных спецификациях сотни полей
называются ``type``, ``items``, ``required``, ``description``. Обход, который
смотрит на имя ключа без учёта позиции, ломает их все.

**Fail closed.** Нераспознанное ключевое слово, неразрешимый ``$ref``,
неоднозначный ``allOf``, схема без единого ограничения — это исключение с точным
JSON Pointer, а не ``schema.any`` и не пустой объект. Единственное послабление —
явный waiver.

**Изоляция направления и waiver'ов.** Нормализация идёт отдельно для ``request`` и
``response``, поэтому ``readOnly``/``writeOnly`` не мутируют общую схему. Если
waiver адресует точку внутри общего ``$ref``, этот ``$ref`` на пути waiver'а
разворачивается по месту, и послабление не достаётся другим операциям.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..errors import UnsupportedConstructError
from ..fingerprints import semantic_source_digest
from ..manifest import Policies, UnknownFormatPolicy
from ..models import (
    INTEGER_FORMAT_BOUNDS,
    AdditionalProperties,
    AllOfNode,
    AnyNode,
    ArrayNode,
    BooleanNode,
    Direction,
    Discriminator,
    IntegerNode,
    NullNode,
    NumberNode,
    ObjectNode,
    Origin,
    PropertySpec,
    RefNode,
    SchemaNode,
    StringNode,
    UnionKind,
    UnionNode,
)
from ..paths import (
    ADDITIONAL_PROPERTIES,
    ARRAY_ITEMS,
    ContractPath,
    format_contract_path,
    variant_segment,
)
from ..waivers import WaiverRule, WaiverSet, waiver_source_digest
from .refs import SpecRegistry

__all__ = ["SUPPORTED_FORMATS", "SchemaDialectConfig", "SchemaNormalizer"]

#: Максимальная глубина вложенности схемы. Защищает от рекурсии, собранной
#: YAML-якорями в обход ``$ref``.
_MAX_DEPTH = 100

#: ``format``, значение которых библиотека признаёт. Всё остальное — либо ошибка,
#: либо аннотация, в зависимости от ``policies.unknown_formats``.
SUPPORTED_FORMATS: dict[str, frozenset[str]] = {
    "string": frozenset(
        {
            "date",
            "date-time",
            "time",
            "duration",
            "email",
            "idn-email",
            "hostname",
            "idn-hostname",
            "ipv4",
            "ipv6",
            "uri",
            "uri-reference",
            "iri",
            "iri-reference",
            "uuid",
            "regex",
            "json-pointer",
            "relative-json-pointer",
            "byte",
            "binary",
            "password",
        }
    ),
    "integer": frozenset({"int32", "int64"}),
    "number": frozenset({"float", "double"}),
    "boolean": frozenset(),
    "object": frozenset(),
    "array": frozenset(),
}

#: Ключевые слова, которые библиотека понимает в позиции Schema Object.
_KNOWN_KEYWORDS = frozenset(
    {
        "$ref",
        "type",
        "format",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "minProperties",
        "maxProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "allOf",
        "oneOf",
        "anyOf",
        "discriminator",
        "readOnly",
        "writeOnly",
        # Аннотации: принимаются и отбрасываются, на семантику не влияют.
        "description",
        "title",
        "example",
        "examples",
        "externalDocs",
        "xml",
        "deprecated",
        "default",
    }
)

#: Ключевые слова, для которых нужна отдельная понятная диагностика.
_REJECTED_KEYWORDS = {
    "not": "отрицание схемы не представимо ни в JSON Schema-контракте библиотеки, ни в d42",
    "if": "условные схемы (if/then/else) появились в JSON Schema 2019-09 и в OpenAPI 3.0 не входят",
    "then": "условные схемы (if/then/else) в OpenAPI 3.0 не поддерживаются",
    "else": "условные схемы (if/then/else) в OpenAPI 3.0 не поддерживаются",
    "const": (
        "const появился в JSON Schema 2019-09; в OpenAPI 3.0 используйте enum из одного значения"
    ),
    "patternProperties": "patternProperties не входит в OpenAPI 3.0",
    "propertyNames": "propertyNames не входит в OpenAPI 3.0",
    "contains": "contains не входит в OpenAPI 3.0",
    "prefixItems": "prefixItems — это JSON Schema 2020-12, в OpenAPI 3.0 его нет",
    "dependentSchemas": "dependentSchemas не входит в OpenAPI 3.0",
    "dependentRequired": "dependentRequired не входит в OpenAPI 3.0",
    "unevaluatedProperties": "unevaluatedProperties не входит в OpenAPI 3.0",
    "unevaluatedItems": "unevaluatedItems не входит в OpenAPI 3.0",
    "$defs": "$defs — это JSON Schema 2019-09; используйте секцию определений своего диалекта",
}

#: Ключи, которые не влияют на семантику и просто отбрасываются.
_ANNOTATION_KEYWORDS = frozenset(
    {"description", "title", "example", "examples", "externalDocs", "xml", "deprecated", "default"}
)


@dataclass(frozen=True, kw_only=True, slots=True)
class SchemaDialectConfig:
    """Особенности диалекта, влияющие на разбор Schema Object."""

    #: Имя ключа, помечающего значение как nullable.
    nullable_key: str
    #: Префикс ссылок на именованные схемы (``#/definitions/`` или ``#/components/schemas/``).
    schema_ref_prefix: str
    #: Поддерживает ли диалект ``writeOnly``.
    supports_write_only: bool


@dataclass(slots=True)
class _Frame:
    """Позиция нормализатора: где мы в документе и где мы в контракте."""

    node: Any
    base: Path
    origin: Origin
    path: ContractPath
    depth: int


class SchemaNormalizer:
    """Приводит Schema Object к IR для одной пары (операция, направление)."""

    def __init__(
        self,
        *,
        registry: SpecRegistry,
        dialect: SchemaDialectConfig,
        operation_key: str,
        direction: Direction,
        waivers: WaiverSet,
        policies: Policies,
    ) -> None:
        self._registry = registry
        self._dialect = dialect
        self._operation_key = operation_key
        self._direction = direction
        self._waivers = waivers
        self._policies = policies
        self._definitions: dict[str, SchemaNode] = {}
        self._definition_sources: dict[str, tuple[str, str]] = {}
        self._building: set[str] = set()
        self._inline_stack: list[tuple[str, str]] = []
        self._waiver_prefixes = self._collect_waiver_prefixes()
        self._variant: tuple[int | str | None, str | None] = (None, None)

    # ------------------------------------------------------------------ API

    @property
    def definitions(self) -> tuple[tuple[str, SchemaNode], ...]:
        """Именованные определения бандла в стабильном порядке."""
        return tuple(sorted(self._definitions.items()))

    @property
    def waivers(self) -> WaiverSet:
        """Набор waiver'ов, с которым работает нормализатор."""
        return self._waivers

    @property
    def operation_key(self) -> str:
        """Стабильный ключ операции."""
        return self._operation_key

    @property
    def direction(self) -> Direction:
        """Направление, для которого строится контракт."""
        return self._direction

    @property
    def variant(self) -> tuple[int | str | None, str | None]:
        """Текущий вариант (статус, content type), если он задан."""
        return self._variant

    def normalize(
        self,
        node: Any,
        *,
        base: Path,
        origin: Origin,
        root: ContractPath = (),
        status: int | str | None = None,
        content_type: str | None = None,
    ) -> SchemaNode:
        """Нормализовать схему, стоящую в контракте по пути ``root``.

        Contract path всегда начинается с пространства имён направления:
        ``("body",)`` для тела и ``("param", <in>, <name>)`` для параметра.
        Благодаря этому waiver на ``/body/id`` и waiver на ``/param/query/id``
        не путаются.

        ``status`` и ``content_type`` описывают текущий вариант: waiver может быть
        сужен до одного варианта ответа, не задевая остальные.
        """
        previous = self._variant
        self._variant = (status, content_type)
        try:
            return self._normalize(_Frame(node=node, base=base, origin=origin, path=root, depth=0))
        finally:
            self._variant = previous

    # ------------------------------------------------------------- waiver'ы

    def _collect_waiver_prefixes(self) -> set[ContractPath]:
        """Все префиксы путей waiver'ов этой операции и направления.

        Если ``$ref`` стоит на пути к точке waiver'а, он разворачивается по месту:
        так послабление не протекает в другие операции, которые ссылаются на ту же
        именованную схему.
        """
        prefixes: set[ContractPath] = set()
        for waiver in self._waivers.waivers:
            if waiver.operation != self._operation_key or waiver.direction != self._direction:
                continue
            for depth in range(len(waiver.path) + 1):
                prefixes.add(waiver.path[:depth])
        return prefixes

    def _consult(self, rule: WaiverRule, frame: _Frame) -> Any:
        """Спросить waiver для текущей точки контракта."""
        status, content_type = self._variant
        waiver = self._waivers.consult(
            operation=self._operation_key,
            direction=self._direction,
            path=frame.path,
            rule=rule,
            source_digest=semantic_source_digest(frame.node),
            status=status,
            content_type=content_type,
        )
        return waiver

    def _fail(self, frame: _Frame, message: str, *, waivable: WaiverRule | None = None) -> None:
        """Бросить ошибку с координатами и, если применимо, готовым рецептом waiver'а."""
        hint = ""
        if waivable is not None:
            hint = (
                f"\nЕсли это осознанное исключение, добавьте в waivers.yaml:\n"
                f"  - operation: {self._operation_key}\n"
                f"    direction: {self._direction.value}\n"
                f"    json_pointer: {format_contract_path(frame.path)}\n"
                f"    rule: {waivable.value}\n"
                f"    expected_source: {semantic_source_digest(frame.node)}\n"
                f"    reason: <зачем>\n"
                f"    owner: <кто отвечает>\n"
                f"    issue: <ссылка>\n"
                f"    expires_at: <YYYY-MM-DD>"
            )
        raise UnsupportedConstructError(
            f"{message} (contract path {format_contract_path(frame.path)}){hint}",
            source=frame.origin.source,
            operation_key=self._operation_key,
            direction=self._direction.value,
            json_pointer=frame.origin.pointer,
        )

    # -------------------------------------------------------------- разбор

    def _normalize(
        self,
        frame: _Frame,
        *,
        allow_replace: bool = True,
        skip_rules: frozenset[WaiverRule] = frozenset(),
    ) -> SchemaNode:
        if frame.depth > _MAX_DEPTH:
            self._fail(frame, f"глубина схемы превысила {_MAX_DEPTH} — похоже на рекурсию")
        node = frame.node
        if isinstance(node, bool):
            self._fail(
                frame,
                "булева схема (true/false) — это JSON Schema 2019-09, в OpenAPI 3.0 её нет",
            )
        if not isinstance(node, dict):
            self._fail(frame, f"ожидался Schema Object, получено {type(node).__name__}")

        if allow_replace:
            waiver = self._consult(WaiverRule.REPLACE_SCHEMA, frame)
            if waiver is not None:
                replaced = _Frame(
                    node=waiver.replacement,
                    base=frame.base,
                    origin=frame.origin,
                    path=frame.path,
                    depth=frame.depth,
                )
                return self._normalize(replaced, allow_replace=False)

        adjustments: list[tuple[WaiverRule, Any, Any]] = []
        for rule, patch in (
            (WaiverRule.ALLOW_NULL, self._with_nullable_waiver),
            (WaiverRule.IGNORE_DISCRIMINATOR, self._without_discriminator_waiver),
            (WaiverRule.EXTEND_ENUM, self._with_extended_enum_waiver),
        ):
            if rule in skip_rules:
                continue
            waiver = self._consult(rule, frame)
            if waiver is not None:
                adjustments.append((rule, patch, waiver))
        if adjustments:
            patched = frame
            for _, patch, waiver in adjustments:
                patched = _Frame(
                    node=patch(patched, waiver),
                    base=frame.base,
                    origin=frame.origin,
                    path=frame.path,
                    depth=frame.depth,
                )
            return self._normalize(
                patched,
                allow_replace=False,
                skip_rules=skip_rules | {rule for rule, _, _ in adjustments},
            )

        if "$ref" in node:
            return self._normalize_ref(frame)

        self._reject_unknown_keywords(frame, node)
        return self._normalize_inline(frame, node)

    def _with_nullable_waiver(self, frame: _Frame, waiver: Any) -> dict[str, Any]:
        """Добавить nullable-маркер к закреплённому исходному фрагменту."""
        if frame.node.get(self._dialect.nullable_key) is True:
            self._fail(frame, "allow_null больше не нужен: источник уже допускает null")
        return {**frame.node, self._dialect.nullable_key: True}

    def _without_discriminator_waiver(self, frame: _Frame, waiver: Any) -> dict[str, Any]:
        """Удалить discriminator только из локальной копии схемы операции."""
        if "discriminator" not in frame.node:
            self._fail(frame, "ignore_discriminator больше не нужен: discriminator отсутствует")
        return {key: value for key, value in frame.node.items() if key != "discriminator"}

    def _with_extended_enum_waiver(self, frame: _Frame, waiver: Any) -> dict[str, Any]:
        """Дополнить существующий enum литералами из waiver'а."""
        raw = frame.node.get("enum")
        if not isinstance(raw, list) or not raw:
            self._fail(frame, "extend_enum применим только к существующему непустому enum")
        values = list(raw)
        for value in waiver.values:
            if not any(type(value) is type(item) and value == item for item in values):
                values.append(value)
        return {**frame.node, "enum": values}

    def _reject_unknown_keywords(self, frame: _Frame, node: dict[str, Any]) -> None:
        for key in sorted(node):
            if key in _KNOWN_KEYWORDS or key == self._dialect.nullable_key:
                continue
            if key.startswith("x-"):
                # Вендорные расширения по спецификации могут игнорироваться
                # инструментами; на семантику контракта они не влияют.
                continue
            if key in _REJECTED_KEYWORDS:
                self._fail(
                    frame,
                    f"ключевое слово {key!r} не поддерживается: {_REJECTED_KEYWORDS[key]}",
                    waivable=WaiverRule.ALLOW_ANY,
                )
            self._fail(
                frame,
                f"неизвестное ключевое слово {key!r} в Schema Object",
                waivable=WaiverRule.ALLOW_ANY,
            )

    # ----------------------------------------------------------------- $ref

    def _normalize_ref(self, frame: _Frame) -> SchemaNode:
        node = frame.node
        SpecRegistry.check_ref_siblings(
            {
                k: v
                for k, v in node.items()
                if k not in {"readOnly", "writeOnly", self._dialect.nullable_key}
            },
            origin=frame.origin,
        )
        resolved = self._registry.resolve(node["$ref"], base=frame.base, origin=frame.origin)
        nullable = bool(node.get(self._dialect.nullable_key, False))

        inline = frame.path in self._waiver_prefixes
        marker = (self._registry.document_id(resolved.document_path), resolved.pointer)

        if inline:
            if marker in self._inline_stack:
                self._fail(
                    frame,
                    f"путь waiver'а проходит через рекурсивный $ref {node['$ref']!r} — "
                    f"развернуть его по месту невозможно",
                )
            self._inline_stack.append(marker)
            try:
                inner = self._normalize(
                    _Frame(
                        node=resolved.value,
                        base=resolved.document_path,
                        origin=resolved.origin,
                        path=frame.path,
                        depth=frame.depth + 1,
                    )
                )
            finally:
                self._inline_stack.pop()
            return _with_nullable(inner, nullable)

        name = self._definition_name(resolved, frame)
        if name not in self._definitions and name not in self._building:
            self._building.add(name)
            try:
                self._definitions[name] = self._normalize(
                    _Frame(
                        node=resolved.value,
                        base=resolved.document_path,
                        origin=resolved.origin,
                        path=frame.path,
                        depth=frame.depth + 1,
                    )
                )
            finally:
                self._building.discard(name)
        return RefNode(origin=frame.origin, nullable=nullable, name=name)

    def _definition_name(self, resolved: Any, frame: _Frame) -> str:
        """Стабильное имя определения. Коллизии разводятся идентификатором файла."""
        document_id = self._registry.document_id(resolved.document_path)
        marker = (document_id, resolved.pointer)
        base_name = resolved.name or "Schema"
        if self._definition_sources.get(base_name) in (None, marker):
            self._definition_sources[base_name] = marker
            return base_name
        qualified = f"{Path(document_id).stem}_{base_name}"
        existing = self._definition_sources.get(qualified)
        if existing in (None, marker):
            self._definition_sources[qualified] = marker
            return qualified
        self._fail(
            frame,
            f"две разные схемы претендуют на имя {qualified!r}; переименуйте одну из них "
            f"в спецификации",
        )
        raise AssertionError("unreachable")

    # ---------------------------------------------------------- inline-узлы

    def _normalize_inline(self, frame: _Frame, node: dict[str, Any]) -> SchemaNode:
        waiver = self._consult(WaiverRule.ALLOW_ANY, frame)
        if waiver is not None:
            return AnyNode(origin=frame.origin, reason=waiver.reason)

        nullable = bool(node.get(self._dialect.nullable_key, False))
        composition_keys = [key for key in ("allOf", "oneOf", "anyOf") if key in node]
        if len(composition_keys) > 1:
            self._fail(
                frame,
                f"одновременно заданы {composition_keys}; такое пересечение неоднозначно",
                waivable=WaiverRule.ALLOW_ANY,
            )

        local_keys = set(node) - _ANNOTATION_KEYWORDS - {"allOf", "oneOf", "anyOf", "discriminator"}
        local_keys -= {"readOnly", "writeOnly", self._dialect.nullable_key}
        local_keys = {key for key in local_keys if not key.startswith("x-")}

        if composition_keys:
            composed = self._normalize_composition(frame, node, composition_keys[0])
            if not local_keys:
                return _with_nullable(composed, nullable)
            local = self._normalize_plain(frame, node)
            return AllOfNode(origin=frame.origin, nullable=nullable, parts=(local, composed))

        return _with_nullable(self._normalize_plain(frame, node), nullable)

    def _normalize_composition(self, frame: _Frame, node: dict[str, Any], key: str) -> SchemaNode:
        raw_parts = node[key]
        if not isinstance(raw_parts, list) or not raw_parts:
            self._fail(frame, f"{key} должен быть непустым списком схем")
        parts: list[SchemaNode] = []
        for index, raw in enumerate(raw_parts):
            parts.append(
                self._normalize(
                    _Frame(
                        node=raw,
                        base=frame.base,
                        origin=frame.origin.child(key, index),
                        path=(*frame.path, variant_segment(index)),
                        depth=frame.depth + 1,
                    )
                )
            )
        if key == "allOf":
            merged = self._try_merge_all_of(frame, tuple(parts))
            if merged is not None:
                return merged
            return AllOfNode(origin=frame.origin, parts=tuple(parts))

        discriminator = self._normalize_discriminator(frame, node, tuple(parts))
        return UnionNode(
            origin=frame.origin,
            kind=UnionKind.ONE_OF if key == "oneOf" else UnionKind.ANY_OF,
            variants=tuple(parts),
            discriminator=discriminator,
        )

    def _try_merge_all_of(self, frame: _Frame, parts: tuple[SchemaNode, ...]) -> ObjectNode | None:
        """Слить ``allOf`` только тогда, когда совместимость доказуема.

        Доказуемо — значит: все части после разрешения ссылок являются объектами,
        ни одна не nullable, конфликтующих одноимённых свойств нет, и политика
        дополнительных ключей совпадает. Иначе композиция сохраняется точно.
        """
        resolved: list[ObjectNode] = []
        for part in parts:
            target = part
            seen: set[str] = set()
            while isinstance(target, RefNode):
                if target.nullable or target.name in seen:
                    return None
                seen.add(target.name)
                candidate = self._definitions.get(target.name)
                if candidate is None:
                    return None
                target = candidate
            if not isinstance(target, ObjectNode) or target.nullable:
                return None
            resolved.append(target)

        properties: dict[str, PropertySpec] = {}
        additional: AdditionalProperties | SchemaNode = AdditionalProperties.ALLOWED
        for part in resolved:
            if part.additional_properties is not AdditionalProperties.ALLOWED:
                if (
                    additional is not AdditionalProperties.ALLOWED
                    and additional != part.additional_properties
                ):
                    return None
                additional = part.additional_properties
            for prop in part.properties:
                existing = properties.get(prop.name)
                if existing is None:
                    properties[prop.name] = prop
                    continue
                if existing.schema != prop.schema:
                    return None
                properties[prop.name] = PropertySpec(
                    name=prop.name,
                    schema=prop.schema,
                    required=existing.required or prop.required,
                )
        if additional is not AdditionalProperties.ALLOWED and any(
            part.additional_properties is AdditionalProperties.ALLOWED for part in resolved
        ):
            # Часть слагаемых запрещает лишние ключи, часть — разрешает.
            # Пересечение таких объектов зависит от того, какие свойства видит
            # каждое слагаемое по отдельности: сливать нельзя.
            return None
        return ObjectNode(
            origin=frame.origin,
            properties=tuple(sorted(properties.values(), key=lambda item: item.name)),
            additional_properties=additional,
        )

    def _normalize_discriminator(
        self, frame: _Frame, node: dict[str, Any], variants: tuple[SchemaNode, ...]
    ) -> Discriminator | None:
        raw = node.get("discriminator")
        if raw is None:
            return None
        if isinstance(raw, str):
            # Swagger 2.0: голая строка. Ограничение «поле обязано быть в required»
            # проверяется на уровне объекта; дополнительной валидации не добавляет.
            return None
        if not isinstance(raw, dict):
            self._fail(frame, "discriminator должен быть объектом с propertyName")
        unknown = sorted(set(raw) - {"propertyName", "mapping"})
        if unknown:
            self._fail(frame, f"discriminator: неизвестные ключи {unknown}")
        property_name = raw.get("propertyName")
        if not isinstance(property_name, str) or not property_name:
            self._fail(frame, "discriminator.propertyName обязателен и должен быть строкой")
        raw_mapping = raw.get("mapping") or {}
        if not isinstance(raw_mapping, dict):
            self._fail(frame, "discriminator.mapping должен быть объектом")
        variant_names = {v.name for v in variants if isinstance(v, RefNode)}
        mapping: list[tuple[str, str]] = []
        for value, target in sorted(raw_mapping.items()):
            if not isinstance(value, str) or not isinstance(target, str):
                self._fail(frame, "discriminator.mapping: ожидались строки")
            name = target.rsplit("/", 1)[-1]
            if name not in variant_names:
                self._fail(
                    frame,
                    f"discriminator.mapping[{value!r}] указывает на {target!r}, "
                    f"которого нет среди вариантов {sorted(variant_names)}",
                )
            mapping.append((value, name))
        return Discriminator(property_name=str(property_name), mapping=tuple(mapping))

    # --------------------------------------------------------- простые узлы

    def _int_bound(self, frame: _Frame, value: Any, *, keyword: str) -> int | None:
        """Целочисленная граница с координатами в ошибке.

        Голый :func:`_int_or_none` не знает ни операции, ни направления, ни
        указателя, поэтому его ошибка непригодна для поиска места в спецификации.
        """
        try:
            return _int_or_none(value)
        except UnsupportedConstructError:
            self._fail(frame, f"{keyword}: ожидалось целое число, получено {value!r}")
        raise AssertionError("unreachable")

    def _normalize_plain(self, frame: _Frame, node: dict[str, Any]) -> SchemaNode:
        declared_type = node.get("type")
        if isinstance(declared_type, list):
            self._fail(
                frame,
                "type в виде списка появился только в OpenAPI 3.1; в 3.0 используйте nullable",
                waivable=WaiverRule.ALLOW_ANY,
            )
        if declared_type is not None and not isinstance(declared_type, str):
            self._fail(frame, f"type должен быть строкой, получено {declared_type!r}")

        enum_values = node.get("enum")
        if enum_values is not None and (not isinstance(enum_values, list) or not enum_values):
            self._fail(frame, "enum должен быть непустым списком")

        if declared_type is None:
            declared_type = self._infer_type(frame, node, enum_values)

        if declared_type == "object":
            return self._normalize_object(frame, node)
        if declared_type == "array":
            return self._normalize_array(frame, node)
        if declared_type == "string":
            return self._normalize_string(frame, node, enum_values)
        if declared_type == "integer":
            return self._normalize_integer(frame, node, enum_values)
        if declared_type == "number":
            return self._normalize_number(frame, node, enum_values)
        if declared_type == "boolean":
            return self._normalize_boolean(frame, node, enum_values)
        if declared_type == "null":
            self._fail(
                frame,
                "type: null появился в OpenAPI 3.1; в 3.0 используйте nullable",
                waivable=WaiverRule.ALLOW_ANY,
            )
        if declared_type == "file":
            self._fail(
                frame,
                "type: file (Swagger 2.0) описывает бинарную выгрузку и не имеет "
                "структурного контракта",
                waivable=WaiverRule.ALLOW_ANY,
            )
        self._fail(frame, f"неизвестный type {declared_type!r}")
        raise AssertionError("unreachable")

    def _infer_type(self, frame: _Frame, node: dict[str, Any], enum_values: Any) -> str:
        """Вывести отсутствующий ``type`` только там, где вывод однозначен."""
        if "properties" in node or "additionalProperties" in node:
            return "object"
        if "items" in node:
            return "array"
        if enum_values:
            kinds = {_json_kind(value) for value in enum_values}
            kinds.discard("null")
            if len(kinds) == 1:
                return kinds.pop()
            self._fail(
                frame,
                f"enum смешивает типы {sorted(kinds)} и не имеет явного type",
                waivable=WaiverRule.ALLOW_ANY,
            )
        self._fail(
            frame,
            "схема без type и без единого ограничения принимает что угодно. "
            "Библиотека не подставляет 'любое значение' молча",
            waivable=WaiverRule.ALLOW_ANY,
        )
        raise AssertionError("unreachable")

    def _check_format(self, frame: _Frame, node: dict[str, Any], kind: str) -> str | None:
        raw = node.get("format")
        if raw is None:
            return None
        if not isinstance(raw, str):
            self._fail(frame, "format должен быть строкой")
        value = str(raw)
        if value in SUPPORTED_FORMATS.get(kind, frozenset()):
            return value
        if self._consult(WaiverRule.ALLOW_UNKNOWN_FORMAT, frame) is not None:
            return value
        if self._policies.unknown_formats is UnknownFormatPolicy.ANNOTATE:
            return value
        self._fail(
            frame,
            f"format {raw!r} неизвестен для type={kind!r}. Известные: "
            f"{sorted(SUPPORTED_FORMATS.get(kind, frozenset()))}. "
            f"Разрешить все неизвестные форматы можно через policies.unknown_formats: annotate",
            waivable=WaiverRule.ALLOW_UNKNOWN_FORMAT,
        )
        raise AssertionError("unreachable")

    def _normalize_object(self, frame: _Frame, node: dict[str, Any]) -> ObjectNode:
        raw_properties = node.get("properties") or {}
        if not isinstance(raw_properties, dict):
            self._fail(frame, "properties должен быть объектом")
        raw_required = node.get("required") or []
        if not isinstance(raw_required, list) or not all(
            isinstance(item, str) for item in raw_required
        ):
            self._fail(frame, "required должен быть списком строк")
        required = set(raw_required)
        dangling = sorted(required - set(raw_properties))
        if dangling and raw_properties:
            self._fail(
                frame,
                f"required перечисляет отсутствующие свойства {dangling}",
                waivable=WaiverRule.ALLOW_ANY,
            )

        properties: list[PropertySpec] = []
        for name in sorted(raw_properties):
            raw_property = raw_properties[name]
            if self._is_pruned(frame, name, raw_property):
                continue
            schema = self._normalize(
                _Frame(
                    node=raw_property,
                    base=frame.base,
                    origin=frame.origin.child("properties", name),
                    path=(*frame.path, name),
                    depth=frame.depth + 1,
                )
            )
            is_required = name in required
            if is_required and self._relaxed_required(frame, name, raw_property):
                is_required = False
            properties.append(PropertySpec(name=name, schema=schema, required=is_required))

        properties.extend(self._added_properties(frame, raw_properties))

        additional = self._normalize_additional(frame, node)
        return ObjectNode(
            origin=frame.origin,
            properties=tuple(sorted(properties, key=lambda item: item.name)),
            additional_properties=additional,
            min_properties=self._int_bound(
                frame, node.get("minProperties"), keyword="minProperties"
            ),
            max_properties=self._int_bound(
                frame, node.get("maxProperties"), keyword="maxProperties"
            ),
        )

    def _relaxed_required(self, frame: _Frame, name: str, raw_property: Any) -> bool:
        status, content_type = self._variant
        waiver = self._waivers.consult(
            operation=self._operation_key,
            direction=self._direction,
            path=(*frame.path, name),
            rule=WaiverRule.RELAX_REQUIRED,
            source_digest=semantic_source_digest(raw_property),
            status=status,
            content_type=content_type,
        )
        return waiver is not None

    def _added_properties(
        self, frame: _Frame, raw_properties: dict[str, Any]
    ) -> list[PropertySpec]:
        """Нормализовать свойства, отсутствующие в источнике и добавленные waiver'ами."""
        added: list[PropertySpec] = []
        status, content_type = self._variant
        for waiver in self._waivers.waivers:
            if (
                waiver.operation != self._operation_key
                or waiver.direction != self._direction
                or waiver.rule is not WaiverRule.ADD_PROPERTY
                or not waiver.matches_variant(status, content_type)
                or len(waiver.path) != len(frame.path) + 1
                or waiver.path[:-1] != frame.path
            ):
                continue
            name = waiver.path[-1]
            if name in raw_properties:
                continue
            used = self._waivers.consult(
                operation=self._operation_key,
                direction=self._direction,
                path=waiver.path,
                rule=WaiverRule.ADD_PROPERTY,
                source_digest=semantic_source_digest(None),
                status=status,
                content_type=content_type,
            )
            if used is None:  # pragma: no cover — найден тот же waiver выше
                continue
            schema = self._normalize(
                _Frame(
                    node=used.replacement,
                    base=frame.base,
                    origin=frame.origin.child("properties", name),
                    path=waiver.path,
                    depth=frame.depth + 1,
                ),
                allow_replace=False,
            )
            added.append(PropertySpec(name=name, schema=schema, required=False))
        return added

    def _is_pruned(self, frame: _Frame, name: str, raw_property: Any) -> bool:
        """Исключается ли свойство из текущего направления.

        ``readOnly`` смотрится и в самой схеме свойства, и рядом с ``$ref``:
        реальные генераторы спецификаций ставят его именно так, а «строгое»
        игнорирование соседей ``$ref`` молча потеряло бы направление.
        """
        if not isinstance(raw_property, dict):
            return False
        keyword: str | None = None
        rule: WaiverRule | None = None
        if self._direction is Direction.REQUEST and raw_property.get("readOnly", False):
            keyword = "readOnly"
            rule = WaiverRule.IGNORE_READ_ONLY
        elif (
            self._direction is Direction.RESPONSE
            and self._dialect.supports_write_only
            and raw_property.get("writeOnly", False)
        ):
            keyword = "writeOnly"
            rule = WaiverRule.IGNORE_WRITE_ONLY
        if keyword is not None and rule is not None:
            status, content_type = self._variant
            waiver = self._waivers.consult(
                operation=self._operation_key,
                direction=self._direction,
                path=(*frame.path, name),
                rule=rule,
                source_digest=waiver_source_digest(raw_property, rule),
                status=status,
                content_type=content_type,
            )
            return waiver is None
        return False

    def _normalize_additional(
        self, frame: _Frame, node: dict[str, Any]
    ) -> AdditionalProperties | SchemaNode:
        if "additionalProperties" not in node:
            return AdditionalProperties.ALLOWED
        raw = node["additionalProperties"]
        if raw is True:
            return AdditionalProperties.ALLOWED
        if raw is False:
            return AdditionalProperties.FORBIDDEN
        if isinstance(raw, dict):
            return self._normalize(
                _Frame(
                    node=raw,
                    base=frame.base,
                    origin=frame.origin.child("additionalProperties"),
                    path=(*frame.path, ADDITIONAL_PROPERTIES),
                    depth=frame.depth + 1,
                )
            )
        self._fail(frame, "additionalProperties должен быть булевым значением или схемой")
        raise AssertionError("unreachable")

    def _normalize_array(self, frame: _Frame, node: dict[str, Any]) -> ArrayNode:
        raw_items = node.get("items")
        if raw_items is None:
            self._fail(
                frame,
                "у массива не задан items: контракт элементов неизвестен",
                waivable=WaiverRule.ALLOW_ANY,
            )
        if isinstance(raw_items, list):
            self._fail(
                frame,
                "items в виде списка (кортежная валидация) в OpenAPI 3.0 не поддерживается",
                waivable=WaiverRule.ALLOW_ANY,
            )
        items = self._normalize(
            _Frame(
                node=raw_items,
                base=frame.base,
                origin=frame.origin.child("items"),
                path=(*frame.path, ARRAY_ITEMS),
                depth=frame.depth + 1,
            )
        )
        return ArrayNode(
            origin=frame.origin,
            items=items,
            min_items=self._int_bound(frame, node.get("minItems"), keyword="minItems"),
            max_items=self._int_bound(frame, node.get("maxItems"), keyword="maxItems"),
            unique_items=bool(node.get("uniqueItems", False)),
        )

    def _normalize_string(
        self, frame: _Frame, node: dict[str, Any], enum_values: Any
    ) -> StringNode:
        enum = None
        if enum_values is not None:
            enum = tuple(self._enum_of(frame, enum_values, str, "строк"))
        pattern = node.get("pattern")
        if pattern is not None and not isinstance(pattern, str):
            self._fail(frame, "pattern должен быть строкой")
        return StringNode(
            origin=frame.origin,
            format=self._check_format(frame, node, "string"),
            enum=enum,
            pattern=pattern,
            min_length=self._int_bound(frame, node.get("minLength"), keyword="minLength"),
            max_length=self._int_bound(frame, node.get("maxLength"), keyword="maxLength"),
        )

    def _normalize_integer(
        self, frame: _Frame, node: dict[str, Any], enum_values: Any
    ) -> IntegerNode:
        enum = None
        if enum_values is not None:
            enum = tuple(self._enum_of(frame, enum_values, int, "целых"))
        minimum, exclusive_minimum = self._bounds(frame, node, "minimum", "exclusiveMinimum")
        maximum, exclusive_maximum = self._bounds(frame, node, "maximum", "exclusiveMaximum")
        integer_format = self._check_format(frame, node, "integer")
        format_bounds = INTEGER_FORMAT_BOUNDS.get(integer_format or "")
        if format_bounds is not None:
            format_minimum, format_maximum = format_bounds
            minimum = format_minimum if minimum is None else max(minimum, format_minimum)
            maximum = format_maximum if maximum is None else min(maximum, format_maximum)
        return IntegerNode(
            origin=frame.origin,
            format=integer_format,
            enum=enum,
            minimum=self._int_bound(frame, minimum, keyword="minimum"),
            maximum=self._int_bound(frame, maximum, keyword="maximum"),
            exclusive_minimum=self._int_bound(frame, exclusive_minimum, keyword="exclusiveMinimum"),
            exclusive_maximum=self._int_bound(frame, exclusive_maximum, keyword="exclusiveMaximum"),
            multiple_of=self._int_bound(frame, node.get("multipleOf"), keyword="multipleOf"),
        )

    def _normalize_number(
        self, frame: _Frame, node: dict[str, Any], enum_values: Any
    ) -> NumberNode:
        enum = None
        if enum_values is not None:
            enum = tuple(float(v) for v in self._enum_of(frame, enum_values, (int, float), "чисел"))
        minimum, exclusive_minimum = self._bounds(frame, node, "minimum", "exclusiveMinimum")
        maximum, exclusive_maximum = self._bounds(frame, node, "maximum", "exclusiveMaximum")
        multiple_of = node.get("multipleOf")
        return NumberNode(
            origin=frame.origin,
            format=self._check_format(frame, node, "number"),
            enum=enum,
            minimum=None if minimum is None else float(minimum),
            maximum=None if maximum is None else float(maximum),
            exclusive_minimum=None if exclusive_minimum is None else float(exclusive_minimum),
            exclusive_maximum=None if exclusive_maximum is None else float(exclusive_maximum),
            multiple_of=None if multiple_of is None else float(multiple_of),
        )

    def _normalize_boolean(
        self, frame: _Frame, node: dict[str, Any], enum_values: Any
    ) -> BooleanNode:
        enum = None
        if enum_values is not None:
            enum = tuple(self._enum_of(frame, enum_values, bool, "булевых значений"))
        self._check_format(frame, node, "boolean")
        return BooleanNode(origin=frame.origin, enum=enum)

    def _enum_of(self, frame: _Frame, values: list[Any], kind: Any, label: str) -> list[Any]:
        """Проверить однородность enum.

        ``bool`` в Python — подкласс ``int``, поэтому булевы значения отсекаются
        отдельно везде, кроме собственно булева enum.
        """
        result: list[Any] = []
        for value in values:
            is_bool = isinstance(value, bool)
            ok = isinstance(value, kind) and (kind is bool or not is_bool)
            if not ok:
                self._fail(frame, f"enum обязан состоять из {label}, встречено {value!r}")
            result.append(value)
        if len(set(map(repr, result))) != len(result):
            self._fail(frame, "enum содержит повторяющиеся значения")
        return result

    def _bounds(
        self, frame: _Frame, node: dict[str, Any], bound_key: str, exclusive_key: str
    ) -> tuple[Any, Any]:
        """Развернуть булев ``exclusiveMinimum``/``exclusiveMaximum`` OpenAPI 3.0."""
        bound = node.get(bound_key)
        exclusive = node.get(exclusive_key)
        if bound is not None and not isinstance(bound, (int, float)):
            self._fail(frame, f"{bound_key} должен быть числом")
        if isinstance(bound, bool):
            self._fail(frame, f"{bound_key} должен быть числом")
        if exclusive is None:
            return bound, None
        if isinstance(exclusive, bool):
            if not exclusive:
                return bound, None
            if bound is None:
                self._fail(frame, f"{exclusive_key}: true задан без {bound_key}")
            return None, bound
        if isinstance(exclusive, (int, float)):
            self._fail(
                frame,
                f"числовой {exclusive_key} появился в OpenAPI 3.1; в 3.0 это булев флаг "
                f"рядом с {bound_key}",
                waivable=WaiverRule.ALLOW_ANY,
            )
        self._fail(frame, f"{exclusive_key} должен быть булевым")
        raise AssertionError("unreachable")


def _with_nullable(node: SchemaNode, nullable: bool) -> SchemaNode:
    """Пометить узел как допускающий ``null``, не создавая новый тип узла."""
    if not nullable or node.nullable:
        return node
    if isinstance(node, NullNode):
        return node
    return _replace_nullable(node)


def _replace_nullable(node: SchemaNode) -> SchemaNode:
    return replace(node, nullable=True)


def _int_or_none(value: Any) -> int | None:
    """Целочисленная граница или ``None``. Нечисловое значение — ошибка, не ``None``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise UnsupportedConstructError(f"ожидалось целое число, получено {value!r}")
    return value


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"

"""Сборка контрактов: manifest + источники + диалекты → операции и артефактные документы.

Здесь сходится весь путь «checked-in OpenAPI → нормализованный контракт»:

1. каждый источник читается через :class:`SpecRegistry` (без сети, внутри своего корня);
2. диалект приводит операции к нейтральной форме;
3. отбираются операции — по allowlist manifest либо все операции источника;
4. проверяется binding: ``operationId``, метод, маршрут, content type, статусы;
5. схемы нормализуются **отдельно для каждого направления**;
6. проверяются ``non_waivable``;
7. собирается канонический документ контракта, из которого дальше растут все артефакты.

Contract path внутри направления имеет ровно два корня:

* ``("body",)`` — тело запроса или ответа;
* ``("param", <in>, <name>)`` — параметр или заголовок.

Вариант (статус, content type) в путь не входит: он задаётся отдельными полями
``status``/``content_type`` у waiver'а. Так путь остаётся читаемым (``/body/rc``),
а область действия — точной.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dialects.base import Dialect, detect_dialect
from .errors import (
    ContractError,
    ManifestBindingError,
    ManifestError,
    NamespaceCollisionError,
    OperationLookupError,
    RefResolutionError,
    SpecLoadError,
    UnsupportedConstructError,
    WaiverError,
)
from .jsonschema_gen import to_json_schema
from .manifest import Manifest, NonWaivableRule, OperationSpec, Selection
from .media import MediaKind, media_kind, media_priority
from .models import (
    AllOfNode,
    AnyNode,
    ArrayNode,
    BodyContract,
    BooleanNode,
    Direction,
    IntegerNode,
    NullNode,
    NumberNode,
    ObjectNode,
    OperationContract,
    Origin,
    ParameterContract,
    ParameterLocation,
    PropertySpec,
    RefNode,
    RequestContract,
    ResponseContract,
    SchemaNode,
    StringNode,
    UncheckedVariant,
    UnionNode,
)
from .naming import d42_schema_name, python_path_from_key, slugify_key, to_pascal_case
from .normalization.parameters import normalize_parameter
from .normalization.raw import RawOperation, RawParameter
from .normalization.refs import SpecRegistry
from .normalization.schemas import SchemaNormalizer
from .paths import (
    ADDITIONAL_PROPERTIES,
    ARRAY_ITEMS,
    BODY_ROOT,
    ContractPath,
    format_contract_path,
    is_variant_segment,
    variant_index,
)
from .waivers import WaiverSet

__all__ = [
    "BODY_ROOT",
    "BuildResult",
    "BuiltOperation",
    "build_contracts",
    "resolve_contract_path",
]


def _param_root(location: ParameterLocation, name: str) -> ContractPath:
    return ("param", location.value, name)


@dataclass(frozen=True, kw_only=True, slots=True)
class BuiltOperation:
    """Собранная операция вместе с её каноническим документом."""

    contract: OperationContract
    #: Канонический документ контракта — основа всех generated-артефактов.
    document: dict[str, Any]
    #: Причины, по которым отдельные варианты не представимы контрактом.
    unsupported: tuple[str, ...]
    #: Те же варианты структурно: тело объявлено спецификацией, но не проверяется.
    unchecked: tuple[UncheckedVariant, ...]
    #: Есть ли в контракте рекурсия. d42 при этом генерируется с отсечкой цикла.
    recursive: bool
    #: Именованные определения IR направления ``request``.
    request_definitions: tuple[tuple[str, SchemaNode], ...] = ()
    #: Именованные определения IR направления ``response``.
    response_definitions: tuple[tuple[str, SchemaNode], ...] = ()


@dataclass(frozen=True, kw_only=True, slots=True)
class BuildResult:
    """Результат сборки всех выбранных операций."""

    operations: tuple[BuiltOperation, ...]

    def by_key(self, key: str) -> BuiltOperation:
        for item in self.operations:
            if item.contract.key == key:
                return item
        raise OperationLookupError(f"операция {key!r} не собрана")


def build_contracts(manifest: Manifest, waivers: WaiverSet) -> BuildResult:
    """Собрать контракты всех выбранных операций."""
    waivers.reset_usage()
    built: list[BuiltOperation] = []
    keys: set[str] = set()

    for source in sorted(manifest.sources, key=lambda item: item.name):
        registry = SpecRegistry(
            source_name=source.name,
            entry_path=manifest.source_path(source.name),
            root=manifest.source_root(source.name),
        )
        document = registry.entry_document()
        dialect = detect_dialect(document, source=source.name)
        raw_operations = dialect.operations(registry, base_path=source.base_path)
        for key, spec, raw in _select(manifest, source.name, source.selection, raw_operations):
            if key in keys:
                raise ManifestError(f"ключ операции {key!r} встречается дважды")
            keys.add(key)
            built.append(
                _build_operation(
                    key=key,
                    spec=spec,
                    raw=raw,
                    manifest=manifest,
                    waivers=waivers,
                    registry=registry,
                    dialect=dialect,
                    source_name=source.name,
                )
            )

    waivers.check_operations_known(keys)
    waivers.assert_all_used()
    _check_python_paths(built)
    return BuildResult(operations=tuple(sorted(built, key=lambda item: item.contract.key)))


# ------------------------------------------------------------------- отбор


def _select(
    manifest: Manifest,
    source_name: str,
    selection: Selection,
    raw_operations: tuple[RawOperation, ...],
) -> list[tuple[str, OperationSpec | None, RawOperation]]:
    """Определить, какие операции источника генерируются, и под какими ключами."""
    if selection is Selection.EXPLICIT:
        return _select_explicit(manifest, source_name, raw_operations)
    return _select_all(manifest, source_name, raw_operations)


def _select_explicit(
    manifest: Manifest, source_name: str, raw_operations: tuple[RawOperation, ...]
) -> list[tuple[str, OperationSpec | None, RawOperation]]:
    by_operation_id: dict[str, list[RawOperation]] = {}
    by_route: dict[tuple[str, str], RawOperation] = {}
    for raw in raw_operations:
        if raw.operation_id:
            by_operation_id.setdefault(raw.operation_id, []).append(raw)
        by_route[(raw.method, raw.path)] = raw

    selected: list[tuple[str, OperationSpec | None, RawOperation]] = []
    for spec in manifest.operations:
        if spec.source != source_name:
            continue
        if spec.operation_id:
            candidates = by_operation_id.get(spec.operation_id, [])
            if not candidates:
                raise ManifestBindingError(
                    f"в источнике нет операции с operationId={spec.operation_id!r}",
                    source=source_name,
                    operation_key=spec.key,
                )
            if len(candidates) > 1:
                raw = _pick_by_route(spec, candidates, source_name)
            else:
                raw = candidates[0]
                if spec.method and spec.path:
                    exact = by_route.get((spec.method, spec.path))
                    if exact is not None:
                        raw = exact
        elif spec.method and spec.path:
            found = by_route.get((spec.method, spec.path))
            if found is None:
                raise ManifestBindingError(
                    f"в источнике нет операции {spec.method} {spec.path}",
                    source=source_name,
                    operation_key=spec.key,
                )
            raw = found
        else:
            raise ManifestError(
                f"operations.{spec.key}: укажите operation_id либо пару method+path — "
                f"иначе операцию невозможно связать со спецификацией"
            )
        _check_binding(spec, raw, source_name)
        selected.append((spec.key, spec, raw))
    return selected


def _pick_by_route(
    spec: OperationSpec, candidates: list[RawOperation], source_name: str
) -> RawOperation:
    """Выбрать одну из операций с общим ``operationId`` по закреплённому маршруту.

    Генераторы спецификаций регулярно выпускают один ``operationId`` на нескольких
    ручках. Какая из них нужна, говорит только пара method + path из manifest;
    без неё выбор был бы произвольным, поэтому это ошибка, а не первая попавшаяся.
    """
    routes = sorted(f"{c.method} {c.path}" for c in candidates)
    if not (spec.method and spec.path):
        raise ManifestBindingError(
            f"operationId={spec.operation_id!r} встречается несколько раз ({routes}); "
            f"уточните method и path в manifest",
            source=source_name,
            operation_key=spec.key,
        )
    for candidate in candidates:
        if (candidate.method, candidate.path) == (spec.method, spec.path):
            return candidate
    raise ManifestBindingError(
        f"operationId={spec.operation_id!r} встречается несколько раз ({routes}), "
        f"и среди этих операций нет {spec.method} {spec.path}, закреплённой в manifest",
        source=source_name,
        operation_key=spec.key,
    )


def _select_all(
    manifest: Manifest, source_name: str, raw_operations: tuple[RawOperation, ...]
) -> list[tuple[str, OperationSpec | None, RawOperation]]:
    seen: dict[str, RawOperation] = {}
    selected: list[tuple[str, OperationSpec | None, RawOperation]] = []
    for raw in raw_operations:
        if not raw.operation_id:
            raise ManifestError(
                f"источник {source_name!r} выбран в режиме all, но у операции "
                f"{raw.method} {raw.path} нет operationId. Ключ невозможно построить "
                f"детерминированно — добавьте operationId в спецификацию или переведите "
                f"источник в режим explicit и задайте ключ вручную"
            )
        if raw.operation_id in seen:
            other = seen[raw.operation_id]
            raise ManifestError(
                f"источник {source_name!r}: operationId={raw.operation_id!r} используется дважды "
                f"({other.method} {other.path} и {raw.method} {raw.path}). "
                f"Ключи операций обязаны быть уникальными"
            )
        seen[raw.operation_id] = raw
        key = f"{source_name}.{raw.operation_id}"
        spec = manifest.operation(key)
        if spec is not None and spec.source != source_name:
            raise ManifestError(
                f"operations.{key}: source={spec.source!r} не совпадает с источником "
                f"{source_name!r}, из которого пришла операция"
            )
        if spec is not None:
            _check_binding(spec, raw, source_name)
        selected.append((key, spec, raw))
    return selected


def _check_binding(spec: OperationSpec, raw: RawOperation, source_name: str) -> None:
    """Проверить, что закреплённая в manifest привязка всё ещё верна."""
    if spec.operation_id and raw.operation_id != spec.operation_id:
        raise ManifestBindingError(
            f"operationId изменился: в manifest {spec.operation_id!r}, "
            f"в спецификации {raw.operation_id!r}",
            source=source_name,
            operation_key=spec.key,
        )
    if spec.method and raw.method != spec.method:
        raise ManifestBindingError(
            f"метод изменился: в manifest {spec.method}, в спецификации {raw.method}",
            source=source_name,
            operation_key=spec.key,
        )
    if spec.path and raw.path != spec.path:
        raise ManifestBindingError(
            f"маршрут изменился: в manifest {spec.path!r}, в спецификации {raw.path!r}",
            source=source_name,
            operation_key=spec.key,
        )
    if spec.request_content_type:
        available = {body.content_type for body in raw.bodies}
        if spec.request_content_type not in available:
            raise ManifestBindingError(
                f"request content type изменился: в manifest {spec.request_content_type!r}, "
                f"в спецификации {sorted(available) or 'тела нет'}",
                source=source_name,
                operation_key=spec.key,
            )
    for selector in spec.responses:
        variants = {(item.status, item.content_type) for item in raw.responses}
        if (selector.status, selector.content_type) not in variants:
            labels = sorted(f"{status}:{ct or '-'}" for status, ct in variants)
            raise ManifestBindingError(
                f"response-вариант {selector.label()} исчез из спецификации; доступны {labels}",
                source=source_name,
                operation_key=spec.key,
            )


def _check_python_paths(built: list[BuiltOperation]) -> None:
    """Финальная проверка namespace уже по фактически собранным операциям."""
    owners: dict[tuple[str, ...], str] = {}
    namespaces: dict[tuple[str, ...], str] = {}
    for item in sorted(built, key=lambda entry: entry.contract.key):
        path = item.contract.python_path
        if path in owners:
            raise NamespaceCollisionError(
                f"операции {owners[path]!r} и {item.contract.key!r} дают одинаковый Python path "
                f"{'.'.join(path)}. Задайте python_path в manifest"
            )
        owners[path] = item.contract.key
        for depth in range(1, len(path)):
            namespaces.setdefault(path[:depth], item.contract.key)
    for path, owner in owners.items():
        if path in namespaces:
            raise NamespaceCollisionError(
                f"операция {owner!r} занимает имя {'.'.join(path)}, которое уже используется "
                f"как namespace операцией {namespaces[path]!r}. Задайте python_path в manifest"
            )


# --------------------------------------------------------------- нормализация


def _build_operation(
    *,
    key: str,
    spec: OperationSpec | None,
    raw: RawOperation,
    manifest: Manifest,
    waivers: WaiverSet,
    registry: SpecRegistry,
    dialect: Dialect,
    source_name: str,
) -> BuiltOperation:
    python_path = spec.resolved_python_path() if spec else python_path_from_key(key)
    unchecked: list[UncheckedVariant] = []

    request_normalizer = SchemaNormalizer(
        registry=registry,
        dialect=dialect.schema_config,
        operation_key=key,
        direction=Direction.REQUEST,
        waivers=waivers,
        policies=manifest.policies,
    )
    response_normalizer = SchemaNormalizer(
        registry=registry,
        dialect=dialect.schema_config,
        operation_key=key,
        direction=Direction.RESPONSE,
        waivers=waivers,
        policies=manifest.policies,
    )

    def probe(direction: Direction) -> SchemaNormalizer:
        """Отдельный нормализатор для пробы текстового варианта.

        Вариант, который в контракт не попадёт, не должен добавлять свои определения в
        направление: иначе его ``$ref`` отключил бы d42 у соседей или породил лишнюю
        d42-схему.
        """
        return SchemaNormalizer(
            registry=registry,
            dialect=dialect.schema_config,
            operation_key=key,
            direction=direction,
            waivers=waivers,
            policies=manifest.policies,
        )

    parameters = _normalize_parameters(raw.parameters, request_normalizer, raw)
    bodies = _normalize_bodies(
        raw, spec, request_normalizer, probe, unchecked, key=key, registry=registry
    )
    responses = _normalize_responses(
        raw, spec, response_normalizer, probe, unchecked, key=key, registry=registry
    )

    contract = OperationContract(
        key=key,
        source=source_name,
        operation_id=raw.operation_id or "",
        method=raw.method,
        path=raw.path,
        python_path=python_path,
        request=RequestContract(
            path_parameters=tuple(p for p in parameters if p.location is ParameterLocation.PATH),
            query_parameters=tuple(p for p in parameters if p.location is ParameterLocation.QUERY),
            header_parameters=tuple(
                p for p in parameters if p.location is ParameterLocation.HEADER
            ),
            cookie_parameters=tuple(
                p for p in parameters if p.location is ParameterLocation.COOKIE
            ),
            bodies=bodies,
            definitions=request_normalizer.definitions,
        ),
        responses=responses,
        origin=raw.origin,
    )

    _check_path_parameters(contract)
    if spec is not None:
        _check_non_waivable(contract, spec)

    recursive = _is_recursive(request_normalizer.definitions) or _is_recursive(
        response_normalizer.definitions
    )
    document = _render_document(
        contract=contract,
        dialect_name=dialect.name,
        request_definitions=request_normalizer.definitions,
        response_definitions=response_normalizer.definitions,
        unsupported=tuple(item.describe() for item in unchecked),
        unchecked=tuple(unchecked),
        recursive=recursive,
        d42_enabled=spec.d42 if spec is not None else True,
    )
    return BuiltOperation(
        contract=contract,
        document=document,
        unsupported=tuple(item.describe() for item in unchecked),
        unchecked=tuple(unchecked),
        recursive=recursive,
        request_definitions=request_normalizer.definitions,
        response_definitions=response_normalizer.definitions,
    )


def _normalize_parameters(
    raw_parameters: tuple[RawParameter, ...],
    normalizer: SchemaNormalizer,
    raw: RawOperation,
) -> tuple[ParameterContract, ...]:
    collected: list[ParameterContract] = []
    for parameter in raw_parameters:
        collected.append(
            normalize_parameter(
                parameter,
                normalizer=normalizer,
                base=raw.document_path,
                root=_param_root(parameter.location, parameter.name),
            )
        )
    return tuple(sorted(collected, key=lambda item: (item.location.value, item.name)))


def _normalize_bodies(
    raw: RawOperation,
    spec: OperationSpec | None,
    normalizer: SchemaNormalizer,
    probe: Callable[[Direction], SchemaNormalizer],
    unchecked: list[UncheckedVariant],
    *,
    key: str,
    registry: SpecRegistry,
) -> tuple[BodyContract, ...]:
    pinned = spec.request_content_type if spec else None
    collected: list[BodyContract] = []
    for body in raw.bodies:
        if body.unsupported_reason is not None:
            # Непроверяемое тело записывается всегда, даже если в manifest закреплено
            # другое: запрос с таким Content-Type надо узнать, а не отклонить.
            unchecked.append(
                UncheckedVariant(
                    direction=Direction.REQUEST,
                    content_type=body.content_type,
                    reason=body.unsupported_reason,
                    required=body.required,
                )
            )
            continue
        if pinned is not None and body.content_type != pinned:
            continue
        twins = [
            other.content_type
            for other in raw.bodies
            if other is not body
            and other.unsupported_reason is None
            and media_kind(other.content_type) is MediaKind.JSON
            and other.schema == body.schema
        ]
        verdict = _text_verdict(
            body.schema,
            base=raw.document_path,
            origin=body.origin,
            content_type=body.content_type,
            status=None,
            probe=lambda: probe(Direction.REQUEST),
            registry=registry,
            pinned=pinned is not None,
            twins=twins,
            shared=body.shared_media_type,
            lenient=body.lenient,
            where="тело запроса",
            key=key,
            direction=Direction.REQUEST,
        )
        if verdict is not None:
            unchecked.append(
                UncheckedVariant(
                    direction=Direction.REQUEST,
                    content_type=body.content_type,
                    reason=verdict,
                    required=body.required,
                )
            )
            continue
        schema = normalizer.normalize(
            body.schema, base=raw.document_path, origin=body.origin, root=BODY_ROOT
        )
        collected.append(
            BodyContract(
                content_type=body.content_type,
                schema=_text_root(schema, body.content_type),
                required=body.required,
                origin=body.origin,
            )
        )
    return tuple(sorted(collected, key=lambda item: item.content_type))


def _normalize_responses(
    raw: RawOperation,
    spec: OperationSpec | None,
    normalizer: SchemaNormalizer,
    probe: Callable[[Direction], SchemaNormalizer],
    unchecked: list[UncheckedVariant],
    *,
    key: str,
    registry: SpecRegistry,
) -> tuple[ResponseContract, ...]:
    pinned = {(item.status, item.content_type) for item in spec.responses} if spec else set()
    pinned_statuses = {status for status, _ in pinned}
    checked_statuses = {item.status for item in raw.responses if item.unsupported_reason is None}
    collected: list[ResponseContract] = []
    for response in raw.responses:
        if response.unsupported_reason is not None:
            # Объявленный статус без контракта нужен моку, чтобы не подменить его вариантом
            # ``default``, и ошибкам выбора — чтобы назвать причину. У закреплённой
            # операции он записывается, если статус закреплён или у него нет проверяемых
            # вариантов вовсе: незакреплённый статус с JSON-вариантом мок по-прежнему
            # отдаёт через ``default``, и посторонний XML-сосед этого не меняет.
            if (
                pinned
                and response.status not in pinned_statuses
                and (response.status in checked_statuses)
            ):
                continue
            unchecked.append(
                UncheckedVariant(
                    direction=Direction.RESPONSE,
                    content_type=response.content_type or "-",
                    reason=response.unsupported_reason,
                    status=response.status,
                )
            )
            continue
        if pinned and (response.status, response.content_type) not in pinned:
            continue
        body = None
        if response.schema is not None:
            if response.content_type is not None:
                twins = [
                    other.content_type
                    for other in raw.responses
                    if other is not response
                    and other.status == response.status
                    and other.unsupported_reason is None
                    and other.content_type is not None
                    and media_kind(other.content_type) is MediaKind.JSON
                    and other.schema == response.schema
                ]
                verdict = _text_verdict(
                    response.schema,
                    base=raw.document_path,
                    origin=response.origin,
                    content_type=response.content_type,
                    status=response.status,
                    probe=lambda: probe(Direction.RESPONSE),
                    registry=registry,
                    pinned=(response.status, response.content_type) in pinned,
                    twins=twins,
                    shared=response.shared_media_type,
                    lenient=response.lenient,
                    where=f"ответ {response.status}",
                    key=key,
                    direction=Direction.RESPONSE,
                )
                if verdict is not None:
                    unchecked.append(
                        UncheckedVariant(
                            direction=Direction.RESPONSE,
                            content_type=response.content_type,
                            reason=verdict,
                            status=response.status,
                        )
                    )
                    continue
            body = normalizer.normalize(
                response.schema,
                base=raw.document_path,
                origin=response.origin,
                root=BODY_ROOT,
                status=response.status,
                content_type=response.content_type,
            )
            body = _text_root(body, response.content_type)
        headers = tuple(
            normalize_parameter(
                header,
                normalizer=normalizer,
                base=raw.document_path,
                root=_param_root(ParameterLocation.HEADER, header.name),
                status=response.status,
            )
            for header in response.headers
        )
        collected.append(
            ResponseContract(
                status=response.status,
                content_type=response.content_type,
                body=body,
                headers=headers,
                definitions=normalizer.definitions,
                origin=response.origin,
            )
        )
    return tuple(sorted(collected, key=lambda item: (str(item.status), item.content_type or "")))


def _text_verdict(
    schema: Any,
    *,
    base: Path,
    origin: Origin,
    content_type: str,
    status: int | str | None,
    probe: Callable[[], SchemaNormalizer],
    registry: SpecRegistry,
    pinned: bool,
    twins: list[str],
    shared: bool,
    lenient: bool,
    where: str,
    key: str,
    direction: Direction,
) -> str | None:
    """Решить судьбу текстового (или впервые поддержанного) варианта до контракта.

    Тело ``text/*`` — строка целиком. Схема пробуется отдельным нормализатором
    (:func:`_build_operation`), чтобы отвергнутый вариант не оставил определений и
    отметок waiver'ов. Возвращает ``None``, если вариант годится, либо причину для
    ``unchecked``.

    Вариант, закреплённый в manifest, просили явно: не-строка или схема, которая не
    разбирается, — ошибка генерации с причиной. Незакреплённый вариант с такой схемой
    генерацию не валит, а попадает в ``unchecked`` с причиной: в реальных спецификациях
    такие варианты почти всегда порождает генератор — общий ``consumes``/``produces``
    Swagger 2.0, умолчания Swashbuckle (``text/plain`` рядом с JSON с тем же ``$ref``),
    springdoc для ``text/csv`` со списком, — и падение генерации было бы регрессией.

    ``lenient`` — JSON-вариант, который прежние версии в контракт не брали (``*/*`` в
    OpenAPI 3.0, ``text/json``): строкой он быть не обязан, но схема, которая не
    разбирается, у незакреплённого варианта тоже уходит в ``unchecked``.
    """
    is_text = media_kind(content_type) is MediaKind.TEXT
    if not is_text and not lenient:
        return None
    if is_text:
        note = _text_origin_note(content_type, twins, shared)
        # Заведомо не строка — нормализация ради этого не нужна, а заготовка waiver'а из
        # её ошибки не помогла бы: вариант всё равно не строка.
        kind = _raw_schema_kind(schema, registry=registry, base=base, origin=origin)
        if kind is not None:
            if pinned:
                raise _text_error(
                    where, content_type, f"схема описывает {kind}", origin, key, direction
                )
            return f"схема описывает {kind}{note}"
        broken = _text_origin_note(content_type, twins, shared, broken=True)
    else:
        note = broken = (
            f": вариант {content_type} не проверяется — до поддержки текстовых тел он в "
            f"контракт не входил; закреплённый в manifest вариант с такой схемой — ошибка "
            f"генерации"
        )
    tester = probe()
    snapshot = tester.waivers.usage_snapshot()
    try:
        node = tester.normalize(
            schema,
            base=base,
            origin=origin,
            root=BODY_ROOT,
            status=status,
            content_type=None if status is None else content_type,
        )
    except (UnsupportedConstructError, WaiverError, RefResolutionError, SpecLoadError) as error:
        # Раньше такой вариант не нормализовался вовсе, поэтому ничто в его схеме — ни
        # неподдержанная конструкция, ни битый ``$ref``, ни waiver на ``/body`` без
        # ``content_type``, выписанный для JSON-соседа, — генерацию не ломало.
        tester.waivers.restore_usage(snapshot)
        if pinned:
            raise
        return f"схема не разбирается{broken}. Ошибка нормализации: {error.base_message}"
    # Отметки waiver'ов поставит основной нормализатор, если вариант будет принят.
    problem = _text_schema_problem(node, dict(tester.definitions)) if is_text else None
    tester.waivers.restore_usage(snapshot)
    if problem is None:
        return None
    if pinned:
        raise _text_error(where, content_type, problem, origin, key, direction)
    return f"{problem}{note}"


def _text_error(
    where: str, content_type: str, problem: str, origin: Origin, key: str, direction: Direction
) -> UnsupportedConstructError:
    return UnsupportedConstructError(
        f"{where} {content_type} — строка, но {problem}. Тело text/* проверяется как "
        f"строка: поправьте схему (type: string) либо уберите этот вариант из выбора в "
        f"manifest (request.content_type / responses)",
        source=origin.source,
        operation_key=key,
        direction=direction.value,
        json_pointer=origin.pointer,
    )


def _raw_schema_kind(
    schema: Any, *, registry: SpecRegistry, base: Path, origin: Origin
) -> str | None:
    """Тип сырой схемы, если он заведомо не строка: ``object``, ``array``, ``integer``, …

    Нужен для причины ``unchecked``, когда нормализация текстового варианта упала: у
    объекта ошибки с полем без схемы нормализатор предлагает waiver, а он бы не помог —
    вариант всё равно не строка. ``$ref`` разворачивается по цепочке; неразрешимая
    ссылка или схема без явных признаков — ``None``.
    """
    current, document, where, seen = schema, base, origin, 0
    while isinstance(current, dict) and "$ref" in current and seen < 32:
        try:
            resolved = registry.resolve(current["$ref"], base=document, origin=where)
        except ContractError:
            return None
        # Следующая ссылка разрешается относительно файла, в котором лежит цель.
        current, document, where = resolved.value, resolved.document_path, resolved.origin
        seen += 1
    if not isinstance(current, dict):
        return None
    declared = current.get("type")
    if isinstance(declared, str) and declared != "string":
        return declared
    if "properties" in current:
        return "object"
    if "items" in current:
        return "array"
    return None


def _text_origin_note(
    content_type: str, twins: list[str], shared: bool, *, broken: bool = False
) -> str:
    """Хвост причины: откуда, скорее всего, взялся непригодный текстовый вариант."""
    if twins:
        return (
            f", как и у {', '.join(sorted(twins))}: {content_type} с такой схемой — артефакт "
            f"генератора спецификации, а тело text/* проверяется только как строка"
        )
    if shared:
        return (
            ": media type взят из общего consumes/produces Swagger 2.0, а тело text/* "
            "проверяется только как строка"
        )
    if broken:
        return (
            f": вариант {content_type} не проверяется; закреплённый в manifest вариант с "
            f"такой схемой — ошибка генерации"
        )
    return (
        f": тело {content_type} проверяется только как строка, поэтому вариант не "
        f"проверяется. Поправьте схему (type: string); закреплённый в manifest вариант с "
        f"такой схемой — ошибка генерации"
    )


def _text_root(node: SchemaNode, content_type: str | None) -> SchemaNode:
    """Корень текстового тела без ограничений (waiver ``allow_any``) — любая строка.

    У тела ``text/*`` не бывает значения другого типа, поэтому контракт, JSON Schema и
    d42 говорят «строка», а не «что угодно».
    """
    if (
        content_type is not None
        and media_kind(content_type) is MediaKind.TEXT
        and isinstance(node, AnyNode)
    ):
        return StringNode(origin=node.origin)
    return node


def _text_schema_problem(
    node: SchemaNode,
    definitions: dict[str, SchemaNode],
    seen: frozenset[str] = frozenset(),
) -> str | None:
    """Чем схема не годится для текстового тела; ``None`` — это строка.

    Строкой считаются ``string``, ``$ref`` на строку, ``allOf``/``oneOf``/``anyOf``,
    все части которых — строки, и ``AnyNode`` (waiver ``allow_any``): у текстового
    тела любое значение — строка.
    """
    if node.nullable:
        return "схема допускает null, а у текстового тела null не бывает"
    if isinstance(node, RefNode):
        if node.name in seen:
            return f"ссылка {node.name!r} ссылается сама на себя"
        target = definitions.get(node.name)
        if target is None:
            return None
        return _text_schema_problem(target, definitions, seen | {node.name})
    if isinstance(node, (StringNode, AnyNode)):
        return None
    parts: tuple[SchemaNode, ...]
    if isinstance(node, AllOfNode):
        parts = node.parts
    elif isinstance(node, UnionNode):
        parts = node.variants
    else:
        return f"схема описывает {_node_kind(node)}"
    for part in parts:
        problem = _text_schema_problem(part, definitions, seen)
        if problem is not None:
            return problem
    return None


def _node_kind(node: SchemaNode) -> str:
    kinds: tuple[tuple[type[SchemaNode], str], ...] = (
        (ObjectNode, "object"),
        (ArrayNode, "array"),
        (IntegerNode, "integer"),
        (NumberNode, "number"),
        (BooleanNode, "boolean"),
        (NullNode, "null"),
    )
    for node_type, name in kinds:
        if isinstance(node, node_type):
            return name
    return type(node).__name__


def _check_path_parameters(contract: OperationContract) -> None:
    """Сверить шаблон маршрута с объявленными path-параметрами."""
    template_names = set(re.findall(r"\{([^}]*)\}", contract.path))
    declared = {item.name for item in contract.request.path_parameters}
    missing = sorted(template_names - declared)
    extra = sorted(declared - template_names)
    if missing:
        raise UnsupportedConstructError(
            f"в маршруте {contract.path!r} есть переменные {missing}, для которых нет "
            f"описанных path-параметров",
            operation_key=contract.key,
            direction=Direction.REQUEST.value,
        )
    if extra:
        raise UnsupportedConstructError(
            f"объявлены path-параметры {extra}, которых нет в маршруте {contract.path!r}",
            operation_key=contract.key,
            direction=Direction.REQUEST.value,
        )
    for name in template_names:
        if not name:
            raise UnsupportedConstructError(
                f"в маршруте {contract.path!r} есть пустая переменная '{{}}'",
                operation_key=contract.key,
            )


# ----------------------------------------------------------- non_waivable


def _check_non_waivable(contract: OperationContract, spec: OperationSpec) -> None:
    """Проверить свойства, закреплённые в manifest как неослабляемые."""
    for assertion in spec.non_waivable:
        roots = _direction_roots(contract, assertion.direction)
        if not roots:
            raise ManifestError(
                f"operations.{spec.key}.non_waivable: у операции нет тела в направлении "
                f"{assertion.direction.value}"
            )
        found = False
        for root, definitions in roots:
            resolved = resolve_contract_path(root, definitions, assertion.path)
            if resolved is None:
                continue
            found = True
            _assert_rules(spec.key, assertion, resolved)
        if not found:
            raise ManifestError(
                f"operations.{spec.key}.non_waivable: путь "
                f"{format_contract_path(assertion.path)} не найден в контракте "
                f"({assertion.direction.value})"
            )


def _direction_roots(
    contract: OperationContract, direction: Direction
) -> list[tuple[SchemaNode, tuple[tuple[str, SchemaNode], ...]]]:
    if direction is Direction.REQUEST:
        return [(body.schema, contract.request.definitions) for body in contract.request.bodies]
    return [
        (response.body, response.definitions)
        for response in contract.responses
        if response.body is not None
    ]


def _assert_rules(
    key: str, assertion: Any, resolved: tuple[SchemaNode, PropertySpec | None]
) -> None:
    node, prop = resolved
    pointer = format_contract_path(assertion.path)
    for rule in assertion.rules:
        if rule is NonWaivableRule.PRESENT:
            continue  # факт нахождения пути уже доказан
        if rule is NonWaivableRule.REQUIRED:
            if prop is None or not prop.required:
                raise ManifestError(
                    f"operations.{key}: non_waivable требует, чтобы {pointer} оставался "
                    f"обязательным, но в контракте он необязателен"
                )
        elif rule is NonWaivableRule.NON_NULL:
            if node.nullable:
                raise ManifestError(
                    f"operations.{key}: non_waivable запрещает null в {pointer}, "
                    f"но контракт допускает null"
                )
        elif rule is NonWaivableRule.NON_EMPTY_ENUM:
            enum = getattr(node, "enum", None)
            if not enum:
                raise ManifestError(
                    f"operations.{key}: non_waivable требует непустой enum в {pointer}, "
                    f"но enum отсутствует"
                )


def resolve_contract_path(
    root: SchemaNode,
    definitions: tuple[tuple[str, SchemaNode], ...],
    path: ContractPath,
) -> tuple[SchemaNode, PropertySpec | None] | None:
    """Пройти contract path по IR, прозрачно разворачивая ``RefNode``.

    Возвращает найденный узел и, если последний шаг был свойством объекта, его
    :class:`PropertySpec` — обязательность нужна правилам ``non_waivable``.
    Путь может начинаться с корня тела (``("body", ...)``): первый сегмент
    ``"body"`` пропускается.
    """
    index = dict(definitions)
    segments = list(path)
    if segments and segments[0] == BODY_ROOT[0]:
        segments = segments[1:]
    node = root
    prop: PropertySpec | None = None
    for segment in segments:
        node = _deref(node, index)
        prop = None
        if segment == ARRAY_ITEMS:
            if not isinstance(node, ArrayNode):
                return None
            node = node.items
        elif segment == ADDITIONAL_PROPERTIES:
            if not isinstance(node, ObjectNode) or not isinstance(
                node.additional_properties, SchemaNode
            ):
                return None
            node = node.additional_properties
        elif is_variant_segment(segment):
            position = variant_index(segment)
            variants = _composition_parts(node)
            if variants is None or position >= len(variants):
                return None
            node = variants[position]
        else:
            if not isinstance(node, ObjectNode):
                return None
            found = node.property(segment)
            if found is None:
                return None
            prop = found
            node = found.schema
    return _deref(node, index), prop


def _deref(node: SchemaNode, index: dict[str, SchemaNode]) -> SchemaNode:
    seen: set[str] = set()
    while isinstance(node, RefNode):
        if node.name in seen:
            return node
        seen.add(node.name)
        target = index.get(node.name)
        if target is None:
            return node
        node = target
    return node


def _composition_parts(node: SchemaNode) -> tuple[SchemaNode, ...] | None:
    from .models import AllOfNode

    if isinstance(node, UnionNode):
        return node.variants
    if isinstance(node, AllOfNode):
        return node.parts
    return None


def _is_recursive(definitions: tuple[tuple[str, SchemaNode], ...]) -> bool:
    """Есть ли цикл в графе именованных определений."""
    from .models import iter_nodes

    index = dict(definitions)
    edges = {
        name: {
            child.name
            for child in iter_nodes(node)
            if isinstance(child, RefNode) and child.name in index
        }
        for name, node in definitions
    }
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(name: str) -> bool:
        if name in visiting:
            return True
        if name in done:
            return False
        visiting.add(name)
        for child in edges.get(name, ()):
            if walk(child):
                return True
        visiting.discard(name)
        done.add(name)
        return False

    return any(walk(name) for name in sorted(edges))


# ------------------------------------------------------------- документ


def _render_document(
    *,
    contract: OperationContract,
    dialect_name: str,
    request_definitions: tuple[tuple[str, SchemaNode], ...],
    response_definitions: tuple[tuple[str, SchemaNode], ...],
    unsupported: tuple[str, ...],
    unchecked: tuple[UncheckedVariant, ...],
    recursive: bool,
    d42_enabled: bool,
) -> dict[str, Any]:
    """Канонический документ контракта — основа всех generated-артефактов."""
    slug = slugify_key(contract.key)
    pascal = "".join(to_pascal_case(part) for part in contract.key.split("."))

    request_schemas = [
        to_json_schema(body.schema, request_definitions) for body in contract.request.bodies
    ]
    request_exports = _variant_exports(
        [
            (body.content_type, body.schema, f"{pascal}Request", schema)
            for body, schema in zip(contract.request.bodies, request_schemas)
        ],
        d42_enabled=d42_enabled,
    )
    request: dict[str, Any] = {
        "parameters": [
            _parameter_document(parameter) for parameter in contract.request.parameters()
        ],
        "bodies": [
            {
                "content_type": body.content_type,
                "required": body.required,
                "schema": schema,
                "d42": export,
            }
            for body, schema, export in zip(
                contract.request.bodies, request_schemas, request_exports
            )
        ],
    }

    response_schemas = [
        None if response.body is None else to_json_schema(response.body, response_definitions)
        for response in contract.responses
    ]
    response_exports = _variant_exports(
        [
            (
                response.content_type or "",
                response.body,
                f"{pascal}Response{response.status}",
                schema,
            )
            for response, schema in zip(contract.responses, response_schemas)
        ],
        d42_enabled=d42_enabled,
    )
    responses: list[dict[str, Any]] = []
    for response, schema, export in zip(contract.responses, response_schemas, response_exports):
        responses.append(
            {
                "status": response.status,
                "content_type": response.content_type,
                "headers": [_parameter_document(header) for header in response.headers],
                "schema": schema,
                "d42": export,
            }
        )

    parameter_exports = _parameter_exports(contract, pascal) if d42_enabled else {}
    return {
        "artifact": {"kind": "operation-contract", "version": 1},
        "key": contract.key,
        "slug": slug,
        "source": contract.source,
        "dialect": dialect_name,
        "operation_id": contract.operation_id,
        "method": contract.method,
        "path": contract.path,
        "python_path": list(contract.python_path),
        "request": request,
        "responses": responses,
        "unsupported": list(unsupported),
        **_unchecked_document(unchecked),
        "d42": {
            "enabled": d42_enabled,
            "recursive": recursive,
            "request_module": f"{slug}_request" if d42_enabled and request["bodies"] else None,
            "response_module": f"{slug}_response"
            if d42_enabled and any(item["schema"] for item in responses)
            else None,
            "parameters_module": f"{slug}_request_parameters" if parameter_exports else None,
            "parameters": parameter_exports,
        },
    }


def _unchecked_document(unchecked: tuple[UncheckedVariant, ...]) -> dict[str, Any]:
    """Ключ ``unchecked`` документа — только если непроверяемые тела есть.

    Документ операции без них остаётся байт-в-байт прежним: обновление библиотеки не
    заставляет перегенерировать JSON-only артефакты, а runtime читает отсутствие ключа
    как пустой список.
    """
    if not unchecked:
        return {}
    return {"unchecked": [item.to_document() for item in unchecked]}


#: Суффикс имени d42-схемы параметров для каждого места запроса.
_PARAMETER_EXPORT_SUFFIXES = {
    ParameterLocation.PATH: "PathParams",
    ParameterLocation.QUERY: "QueryParams",
    ParameterLocation.HEADER: "Headers",
    ParameterLocation.COOKIE: "Cookies",
}


def _parameter_exports(contract: OperationContract, pascal: str) -> dict[str, str]:
    """Место запроса → имя переменной его d42-схемы; места без параметров пропускаются."""
    return {
        location.value: d42_schema_name(f"{pascal}{suffix}")
        for location, suffix in _PARAMETER_EXPORT_SUFFIXES.items()
        if any(parameter.location is location for parameter in contract.request.parameters())
    }


def _parameter_document(parameter: ParameterContract) -> dict[str, Any]:
    return {
        "name": parameter.name,
        "in": parameter.location.value,
        "required": parameter.required,
        "style": parameter.style,
        "explode": parameter.explode,
        "schema": to_json_schema(parameter.schema, ()),
    }


def _root_export(node: SchemaNode, fallback: str) -> str:
    """Имя переменной generated d42-схемы для корня варианта."""
    if isinstance(node, RefNode):
        return d42_schema_name(node.name)
    return d42_schema_name(fallback)


def _variant_exports(
    variants: list[tuple[str, SchemaNode | None, str, dict[str, Any] | None]],
    *,
    d42_enabled: bool,
) -> list[str | None]:
    """Имена d42-схем корней вариантов одного направления.

    ``variants`` — ``(content type, узел IR, базовое имя, JSON Schema)``. Базовое имя
    общее у вариантов одного статуса (и у всех тел запроса), поэтому два варианта с
    разными схемами — ``application/json`` и ``text/plain`` — затёрли бы друг друга в
    d42-модуле, и мок проверялся бы чужой схемой.

    Базовое имя остаётся у главного варианта (:func:`~geas.media.media_priority`) и у
    всех вариантов с той же схемой; остальным к имени добавляется content type. Так
    появление ``text/plain`` рядом с JSON не переименовывает схему JSON-варианта.
    Корень-``$ref`` называется по определению и конфликтовать не может. Совпадение
    имён при разных схемах проверяется, только если d42 для операции включён.
    """
    owners: dict[str, tuple[tuple[int, str], dict[str, Any]]] = {}
    for content_type, node, base, schema in variants:
        if node is None or schema is None or isinstance(node, RefNode):
            continue
        rank = media_priority(content_type)
        current = owners.get(base)
        if current is None or rank < current[0]:
            owners[base] = (rank, schema)

    names: list[str | None] = []
    taken: dict[str, tuple[str, dict[str, Any]]] = {}
    for content_type, node, base, schema in variants:
        if node is None:
            names.append(None)
            continue
        if isinstance(node, RefNode) or schema is None:
            names.append(_root_export(node, base))
            continue
        owner_schema = owners[base][1]
        name = (
            d42_schema_name(base)
            if schema == owner_schema
            else d42_schema_name(f"{base}{_content_type_suffix(content_type)}")
        )
        previous = taken.setdefault(name, (content_type, schema))
        if d42_enabled and previous[1] != schema:
            raise NamespaceCollisionError(
                f"d42-схемы вариантов {previous[0]!r} и {content_type!r} с разными схемами "
                f"получают одно имя {name!r}. Закрепите в manifest один из вариантов "
                f"(request.content_type / responses) либо выключите d42 для операции"
            )
        names.append(name)
    return names


def _content_type_suffix(content_type: str) -> str:
    """``application/merge-patch+json`` → ``ApplicationMergePatchJson``, ``*/*`` → ``Any``."""
    suffix = "".join(part.capitalize() for part in re.split(r"[^0-9A-Za-z]+", content_type) if part)
    return suffix or "Any"

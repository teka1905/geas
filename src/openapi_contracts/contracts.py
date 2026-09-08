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

from dataclasses import dataclass
from typing import Any

from .dialects.base import Dialect, detect_dialect
from .errors import (
    ManifestBindingError,
    ManifestError,
    NamespaceCollisionError,
    OperationLookupError,
    UnsupportedConstructError,
)
from .jsonschema_gen import to_json_schema
from .manifest import Manifest, NonWaivableRule, OperationSpec, Selection
from .models import (
    ArrayNode,
    BodyContract,
    Direction,
    ObjectNode,
    OperationContract,
    ParameterContract,
    ParameterLocation,
    PropertySpec,
    RefNode,
    RequestContract,
    ResponseContract,
    SchemaNode,
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

#: Корень contract path для тела.
BODY_ROOT: ContractPath = ("body",)


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
    #: Есть ли в контракте рекурсия (тогда d42 для операции не генерируется).
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
                routes = sorted(f"{c.method} {c.path}" for c in candidates)
                raise ManifestBindingError(
                    f"operationId={spec.operation_id!r} встречается несколько раз ({routes}); "
                    f"уточните method и path в manifest",
                    source=source_name,
                    operation_key=spec.key,
                )
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
    unsupported: list[str] = []

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

    parameters = _normalize_parameters(raw.parameters, request_normalizer, raw)
    bodies = _normalize_bodies(raw, spec, request_normalizer, unsupported)
    responses = _normalize_responses(raw, spec, response_normalizer, unsupported)

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
        unsupported=tuple(unsupported),
        recursive=recursive,
        d42_enabled=(spec.d42 if spec is not None else True) and not recursive,
    )
    return BuiltOperation(
        contract=contract,
        document=document,
        unsupported=tuple(unsupported),
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
    unsupported: list[str],
) -> tuple[BodyContract, ...]:
    pinned = spec.request_content_type if spec else None
    collected: list[BodyContract] = []
    for body in raw.bodies:
        if pinned is not None and body.content_type != pinned:
            continue
        if body.unsupported_reason is not None:
            unsupported.append(f"request body {body.content_type}: {body.unsupported_reason}")
            continue
        schema = normalizer.normalize(
            body.schema, base=raw.document_path, origin=body.origin, root=BODY_ROOT
        )
        collected.append(
            BodyContract(
                content_type=body.content_type,
                schema=schema,
                required=body.required,
                origin=body.origin,
            )
        )
    return tuple(sorted(collected, key=lambda item: item.content_type))


def _normalize_responses(
    raw: RawOperation,
    spec: OperationSpec | None,
    normalizer: SchemaNormalizer,
    unsupported: list[str],
) -> tuple[ResponseContract, ...]:
    pinned = {(item.status, item.content_type) for item in spec.responses} if spec else set()
    collected: list[ResponseContract] = []
    for response in raw.responses:
        if pinned and (response.status, response.content_type) not in pinned:
            continue
        label = f"response {response.status}:{response.content_type or '-'}"
        if response.unsupported_reason is not None:
            unsupported.append(f"{label}: {response.unsupported_reason}")
            continue
        body = None
        if response.schema is not None:
            body = normalizer.normalize(
                response.schema,
                base=raw.document_path,
                origin=response.origin,
                root=BODY_ROOT,
                status=response.status,
                content_type=response.content_type,
            )
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


def _check_path_parameters(contract: OperationContract) -> None:
    """Сверить шаблон маршрута с объявленными path-параметрами."""
    import re

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
    recursive: bool,
    d42_enabled: bool,
) -> dict[str, Any]:
    """Канонический документ контракта — основа всех generated-артефактов."""
    slug = slugify_key(contract.key)
    pascal = "".join(to_pascal_case(part) for part in contract.key.split("."))

    request: dict[str, Any] = {
        "parameters": [
            _parameter_document(parameter) for parameter in contract.request.parameters()
        ],
        "bodies": [
            {
                "content_type": body.content_type,
                "required": body.required,
                "schema": to_json_schema(body.schema, request_definitions),
                "d42": _root_export(body.schema, f"{pascal}Request"),
            }
            for body in contract.request.bodies
        ],
    }

    responses: list[dict[str, Any]] = []
    for response in contract.responses:
        item: dict[str, Any] = {
            "status": response.status,
            "content_type": response.content_type,
            "headers": [_parameter_document(header) for header in response.headers],
            "schema": None,
            "d42": None,
        }
        if response.body is not None:
            item["schema"] = to_json_schema(response.body, response_definitions)
            item["d42"] = _root_export(response.body, f"{pascal}Response{response.status}")
        responses.append(item)

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
        "d42": {
            "enabled": d42_enabled,
            "recursive": recursive,
            "request_module": f"{slug}_request" if d42_enabled and request["bodies"] else None,
            "response_module": f"{slug}_response"
            if d42_enabled and any(item["schema"] for item in responses)
            else None,
        },
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

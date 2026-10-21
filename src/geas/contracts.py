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

from dataclasses import dataclass, field
from typing import Any

from .dialects.base import Dialect, detect_dialect, source_operations
from .errors import (
    ManifestBindingError,
    ManifestError,
    NamespaceCollisionError,
    OperationLookupError,
    UnsupportedConstructError,
)
from .jsonschema_gen import to_json_schema
from .manifest import Manifest, NonWaivableRule, OperationSpec, Selection, SourceSpec
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
    BODY_ROOT,
    ContractPath,
    format_contract_path,
    is_variant_segment,
    variant_index,
)
from .runtime.operation import route_templates_overlap, route_variants
from .waivers import WaiverSet

__all__ = [
    "BODY_ROOT",
    "BuildResult",
    "BuiltOperation",
    "build_contracts",
    "resolve_contract_path",
    "route_hint",
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
        raw_operations = source_operations(
            dialect,
            registry,
            source=source.name,
            base_path=source.base_path,
            strip_prefix=source.strip_prefix,
        )
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
    _check_route_overlaps(built)
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
        # Операция вне ``strip_prefix`` сохраняет исходный путь, и он может совпасть
        # с итоговым маршрутом другой операции. Маршрут принадлежит той, что отображена.
        route = (raw.method, raw.path)
        if not raw.route_error or route not in by_route:
            by_route[route] = raw

    hint = route_hint(manifest.source(source_name))
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
                raw = _pick_by_route(spec, candidates, source_name, hint=hint)
            else:
                raw = candidates[0]
                # У операции с ``route_error`` маршрута нет: подмена по маршруту
                # заслонила бы её причину чужим «operationId изменился».
                if spec.method and spec.path and not raw.route_error:
                    exact = by_route.get((spec.method, spec.path))
                    if exact is not None:
                        raw = exact
        elif spec.method and spec.path:
            found = by_route.get((spec.method, spec.path))
            if found is None:
                raise ManifestBindingError(
                    f"в источнике нет операции {spec.method} {spec.path}{hint}",
                    source=source_name,
                    operation_key=spec.key,
                )
            raw = found
        else:
            raise ManifestError(
                f"operations.{spec.key}: укажите operation_id либо пару method+path — "
                f"иначе операцию невозможно связать со спецификацией"
            )
        _check_binding(spec, raw, source_name, hint=hint)
        selected.append((spec.key, spec, raw))
    return selected


def route_hint(source: SourceSpec) -> str:
    """Подсказка для промаха по маршруту: с ``strip_prefix`` manifest хранит итоговый путь."""
    if source.strip_prefix is None:
        return ""
    return (
        f"; маршруты источника отображены sources.{source.name}.strip_prefix="
        f"{source.strip_prefix!r} — укажите итоговый маршрут, его печатает 'geas list'"
    )


def _pick_by_route(
    spec: OperationSpec, candidates: list[RawOperation], source_name: str, *, hint: str
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
    matching = [c for c in candidates if (c.method, c.path) == (spec.method, spec.path)]
    if matching:
        # Как и в ``by_route``: маршрут принадлежит операции, отображённой ``strip_prefix``.
        return next((c for c in matching if not c.route_error), matching[0])
    raise ManifestBindingError(
        f"operationId={spec.operation_id!r} встречается несколько раз ({routes}), "
        f"и среди этих операций нет {spec.method} {spec.path}, закреплённой в manifest{hint}",
        source=source_name,
        operation_key=spec.key,
    )


def _select_all(
    manifest: Manifest, source_name: str, raw_operations: tuple[RawOperation, ...]
) -> list[tuple[str, OperationSpec | None, RawOperation]]:
    hint = route_hint(manifest.source(source_name))
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
        if raw.route_error:
            raise ManifestBindingError(raw.route_error, source=source_name, operation_key=key)
        spec = manifest.operation(key)
        if spec is not None and spec.source != source_name:
            raise ManifestError(
                f"operations.{key}: source={spec.source!r} не совпадает с источником "
                f"{source_name!r}, из которого пришла операция"
            )
        if spec is not None:
            _check_binding(spec, raw, source_name, hint=hint)
        selected.append((key, spec, raw))
    return selected


def _check_binding(spec: OperationSpec, raw: RawOperation, source_name: str, *, hint: str) -> None:
    """Проверить, что закреплённая в manifest привязка всё ещё верна.

    ``hint`` дописывается к расхождению маршрута: после включения ``strip_prefix``
    manifest часто ещё хранит исходный путь.
    """
    if raw.route_error:
        raise ManifestBindingError(raw.route_error, source=source_name, operation_key=spec.key)
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
            f"маршрут изменился: в manifest {spec.path!r}, в спецификации {raw.path!r}{hint}",
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


def _route_forms(item: BuiltOperation, *, with_setting: bool) -> tuple[str, ...]:
    """Шаблоны маршрута, которые принимает операция — с настройкой или строго."""
    if with_setting and item.document.get("accept_trailing_slash"):
        return route_variants(item.contract.path)
    return (item.contract.path,)


def _forms_overlap(first: BuiltOperation, second: BuiltOperation, *, with_setting: bool) -> bool:
    return any(
        route_templates_overlap(left, right)
        for left in _route_forms(first, with_setting=with_setting)
        for right in _route_forms(second, with_setting=with_setting)
    )


def _check_route_overlaps(built: list[BuiltOperation]) -> None:
    """Не дать ``accept_trailing_slash`` склеить две операции.

    Если ``GET /x`` принимает и ``/x/``, а в спецификации есть ``GET /x/`` (или
    ``GET /x/{id}`` и ``GET /x/new/``), мок одной операции перехватывал бы
    запросы другой, а какая ответит, решал бы порядок регистрации. Ошибка — только
    для пересечений, которые появились из-за настройки: те, что есть и без неё
    (например, один маршрут под несколькими ключами manifest), — дело самой
    спецификации. Источники проверяются вместе: все моки регистрируются на одном
    JJ-сервере.
    """
    ordered = sorted(built, key=lambda entry: entry.contract.key)
    # Префиксное дерево по сегментам всех форм маршрута: кандидаты в пары ищутся
    # только от операций с настройкой и только по совместимым веткам, а не
    # перебором всех пар. Точное решение по паре — в ``_check_route_pair``.
    roots: dict[str, _RouteNode] = {}
    for index, item in enumerate(ordered):
        root = roots.setdefault(item.contract.method, _RouteNode())
        for form in _route_forms(item, with_setting=True):
            root.insert(form.split("/"), index)

    pairs: set[tuple[int, int]] = set()
    for index, item in enumerate(ordered):
        if not item.document.get("accept_trailing_slash"):
            continue
        root = roots[item.contract.method]
        for form in _route_forms(item, with_setting=True):
            for other in root.candidates(form.split("/")):
                if other != index:
                    pairs.add((min(index, other), max(index, other)))
    for first, second in sorted(pairs):
        _check_route_pair(ordered[first], ordered[second])


@dataclass(slots=True)
class _RouteNode:
    """Узел префиксного дерева маршрутов: литеральные и шаблонные сегменты."""

    literal: dict[str, _RouteNode] = field(default_factory=dict)
    templated: dict[str, _RouteNode] = field(default_factory=dict)
    operations: set[int] = field(default_factory=set)

    def insert(self, segments: list[str], operation: int) -> None:
        node = self
        for segment in segments:
            children = node.templated if "{" in segment else node.literal
            node = children.setdefault(segment, _RouteNode())
        node.operations.add(operation)

    def candidates(self, segments: list[str]) -> set[int]:
        """Операции, чьи формы могут совпасть с ``segments`` (с запасом)."""
        if not segments:
            return set(self.operations)
        head, rest = segments[0], segments[1:]
        if "{" in head:
            children = [*self.literal.values(), *self.templated.values()]
        else:
            children = list(self.templated.values())
            if head in self.literal:
                children.append(self.literal[head])
        found: set[int] = set()
        for child in children:
            found |= child.candidates(rest)
        return found


def _check_route_pair(first: BuiltOperation, second: BuiltOperation) -> None:
    # Пары собираются только от операций с настройкой, так что список не пуст.
    enabled = [
        item.contract.key for item in (first, second) if item.document.get("accept_trailing_slash")
    ]
    if not _forms_overlap(first, second, with_setting=True):
        return
    if _forms_overlap(first, second, with_setting=False):
        return
    raise ManifestError(
        f"с accept_trailing_slash операции {first.contract.key!r} "
        f"({first.contract.method} {first.contract.path}) и "
        f"{second.contract.key!r} ({second.contract.method} "
        f"{second.contract.path}) принимают один и тот же маршрут. "
        f"Задайте accept_trailing_slash: false у "
        f"{', '.join(repr(key) for key in enabled)}; "
        f"у кого из них нет записи в manifest, добавьте её вручную"
    )


def _accepts_trailing_slash(spec: OperationSpec | None, source: SourceSpec) -> bool:
    """Значение операции, а если его нет, — источника."""
    if spec is not None and spec.accept_trailing_slash is not None:
        return spec.accept_trailing_slash
    return source.accept_trailing_slash


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
        d42_enabled=spec.d42 if spec is not None else True,
        accept_trailing_slash=_accepts_trailing_slash(spec, manifest.source(source_name)),
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
    accept_trailing_slash: bool,
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

    parameter_exports = _parameter_exports(contract, pascal) if d42_enabled else {}
    document: dict[str, Any] = {
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
            "parameters_module": f"{slug}_request_parameters" if parameter_exports else None,
            "parameters": parameter_exports,
        },
    }
    if accept_trailing_slash:
        # Ключ пишется только при включённой настройке: у проектов со строгим
        # маршрутом артефакты остаются прежними байт в байт.
        document["accept_trailing_slash"] = True
    return document


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

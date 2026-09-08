"""Версионируемый manifest: что генерируем и к чему привязываемся.

Manifest — единственный вход генератора. Он фиксирует:

* какие OpenAPI-источники читать и где проходит корень для межфайловых ``$ref``;
* режим выбора операций — ``explicit`` (allowlist) или ``all``;
* стабильный ключ операции и её привязку (``operationId``, метод, маршрут,
  content type запроса, выбранные статусы и content type ответов);
* необязательный явный ``python_path``;
* ``non_waivable`` — свойства, которые нельзя ослабить никаким waiver'ом;
* политики (максимальный срок waiver'а, отношение к неизвестным ``format``).

Разбор строгий: неизвестный ключ, неверный тип и противоречивая комбинация — это
:class:`~openapi_contracts.errors.ManifestError`, а не «поле проигнорировано».
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .errors import ContractError, ManifestError
from .models import Direction
from .naming import (
    check_namespace_names,
    python_identifier,
    python_path_from_key,
    to_snake_case,
)
from .paths import ContractPath, format_contract_path, parse_contract_path

__all__ = [
    "MANIFEST_VERSION",
    "Manifest",
    "NonWaivableAssertion",
    "NonWaivableRule",
    "OperationSpec",
    "Policies",
    "ResponseSelector",
    "Selection",
    "SourceSpec",
    "UnknownFormatPolicy",
    "default_manifest_document",
    "dump_manifest",
    "load_manifest",
]

#: Версия формата manifest. Ядро читает только эту версию.
MANIFEST_VERSION = 1

#: Срок жизни waiver'а по умолчанию, дни.
DEFAULT_WAIVER_MAX_DAYS = 90


class Selection(str, Enum):
    """Режим выбора операций источника."""

    #: Генерируются только перечисленные в ``operations`` записи.
    EXPLICIT = "explicit"
    #: Генерируются все операции источника.
    ALL = "all"


class UnknownFormatPolicy(str, Enum):
    """Что делать с ``format``, которого нет в allowlist."""

    REJECT = "reject"
    #: ``format`` сохраняется в артефактах, но не участвует в валидации.
    ANNOTATE = "annotate"


class NonWaivableRule(str, Enum):
    """Свойство контракта, закрепляемое через ``non_waivable``."""

    #: Поле обязано остаться обязательным.
    REQUIRED = "required"
    #: Поле не может стать nullable.
    NON_NULL = "non_null"
    #: У поля обязан быть непустой enum.
    NON_EMPTY_ENUM = "non_empty_enum"
    #: Поле обязано остаться в контракте (не удалено и не переименовано).
    PRESENT = "present"


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{where}: ожидался объект, получено {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise ManifestError(f"{where}: ключ {key!r} должен быть строкой")
    return dict(value)


def _reject_unknown(data: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ManifestError(f"{where}: неизвестные ключи {unknown}. Допустимые: {sorted(allowed)}")


def _require_str(data: dict[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}: поле {key!r} обязательно и должно быть непустой строкой")
    return value


@dataclass(frozen=True, kw_only=True, slots=True)
class Policies:
    """Настраиваемые политики проекта."""

    waiver_max_days: int = DEFAULT_WAIVER_MAX_DAYS
    unknown_formats: UnknownFormatPolicy = UnknownFormatPolicy.REJECT

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Policies:
        if data is None:
            return cls()
        data = _require_mapping(data, "policies")
        _reject_unknown(data, {"waiver_max_days", "unknown_formats"}, "policies")
        max_days = data.get("waiver_max_days", DEFAULT_WAIVER_MAX_DAYS)
        if not isinstance(max_days, int) or isinstance(max_days, bool) or max_days <= 0:
            raise ManifestError("policies.waiver_max_days: ожидалось положительное целое")
        raw_formats = data.get("unknown_formats", UnknownFormatPolicy.REJECT.value)
        try:
            formats = UnknownFormatPolicy(raw_formats)
        except ValueError as exc:
            allowed = [item.value for item in UnknownFormatPolicy]
            raise ManifestError(
                f"policies.unknown_formats: {raw_formats!r} не входит в {allowed}"
            ) from exc
        return cls(waiver_max_days=max_days, unknown_formats=formats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "waiver_max_days": self.waiver_max_days,
            "unknown_formats": self.unknown_formats.value,
        }


@dataclass(frozen=True, kw_only=True, slots=True)
class SourceSpec:
    """Один OpenAPI-источник."""

    name: str
    #: Путь к файлу спецификации относительно каталога manifest.
    path: str
    #: Корень для межфайловых ``$ref``; по умолчанию — каталог самой спецификации.
    root: str | None = None
    selection: Selection = Selection.EXPLICIT
    #: Префикс маршрута. ``None`` — взять тот, что диктует сам документ
    #: (``basePath`` в Swagger 2.0, пустая строка в OpenAPI 3.0).
    base_path: str | None = None

    @classmethod
    def from_dict(cls, name: str, data: Any) -> SourceSpec:
        where = f"sources.{name}"
        data = _require_mapping(data, where)
        _reject_unknown(data, {"path", "root", "selection", "base_path"}, where)
        python_identifier(to_snake_case(name), context=f"имя источника {name!r}")
        raw_selection = data.get("selection", Selection.EXPLICIT.value)
        try:
            selection = Selection(raw_selection)
        except ValueError as exc:
            allowed = [item.value for item in Selection]
            raise ManifestError(
                f"{where}.selection: {raw_selection!r} не входит в {allowed}"
            ) from exc
        root = data.get("root")
        if root is not None and not isinstance(root, str):
            raise ManifestError(f"{where}.root: ожидалась строка")
        base_path = data.get("base_path")
        if base_path is not None and not isinstance(base_path, str):
            raise ManifestError(f"{where}.base_path: ожидалась строка")
        return cls(
            name=name,
            path=_require_str(data, "path", where),
            root=root,
            selection=selection,
            base_path=base_path,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "selection": self.selection.value}
        if self.root is not None:
            data["root"] = self.root
        if self.base_path is not None:
            data["base_path"] = self.base_path
        return data


@dataclass(frozen=True, kw_only=True, slots=True)
class ResponseSelector:
    """Выбранный вариант ответа операции."""

    status: int | str
    content_type: str | None

    @classmethod
    def from_dict(cls, data: Any, where: str) -> ResponseSelector:
        data = _require_mapping(data, where)
        _reject_unknown(data, {"status", "content_type"}, where)
        status = data.get("status")
        if isinstance(status, bool) or not isinstance(status, (int, str)):
            raise ManifestError(f"{where}.status: ожидалось целое или 'default'")
        if isinstance(status, str) and status != "default":
            raise ManifestError(f"{where}.status: строковый статус допустим только как 'default'")
        content_type = data.get("content_type")
        if content_type is not None and not isinstance(content_type, str):
            raise ManifestError(f"{where}.content_type: ожидалась строка")
        return cls(status=status, content_type=content_type)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"status": self.status}
        if self.content_type is not None:
            data["content_type"] = self.content_type
        return data

    def label(self) -> str:
        """Человекочитаемый ярлык варианта — для ошибок и имён артефактов."""
        return f"{self.status}:{self.content_type or '-'}"


@dataclass(frozen=True, kw_only=True, slots=True)
class NonWaivableAssertion:
    """Свойство, закреплённое явно и недоступное для ослабления waiver'ом."""

    direction: Direction
    path: ContractPath
    rules: tuple[NonWaivableRule, ...]

    @classmethod
    def from_dict(cls, data: Any, where: str) -> NonWaivableAssertion:
        data = _require_mapping(data, where)
        _reject_unknown(data, {"direction", "json_pointer", "rules"}, where)
        try:
            direction = Direction(_require_str(data, "direction", where))
        except ValueError as exc:
            raise ManifestError(f"{where}.direction: ожидалось 'request' или 'response'") from exc
        raw_pointer = _require_str(data, "json_pointer", where)
        try:
            path = parse_contract_path(raw_pointer)
        except ContractError as exc:
            # Битый указатель в manifest — это ошибка manifest: без обёртки
            # потребитель получил бы ContractError без имени поля.
            raise ManifestError(f"{where}.json_pointer: {exc}") from exc
        raw_rules = data.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ManifestError(f"{where}.rules: ожидался непустой список правил")
        rules: list[NonWaivableRule] = []
        for raw in raw_rules:
            try:
                rules.append(NonWaivableRule(raw))
            except ValueError as exc:
                allowed = [item.value for item in NonWaivableRule]
                raise ManifestError(f"{where}.rules: {raw!r} не входит в {allowed}") from exc
        return cls(direction=direction, path=path, rules=tuple(sorted(set(rules), key=str)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction.value,
            "json_pointer": format_contract_path(self.path),
            "rules": [rule.value for rule in self.rules],
        }


@dataclass(frozen=True, kw_only=True, slots=True)
class OperationSpec:
    """Запись manifest об одной операции.

    В режиме ``explicit`` запись — это одновременно allowlist и закрепление
    привязки: любое расхождение с текущей спецификацией ломает генерацию.
    В режиме ``all`` запись необязательна и служит только для уточнения
    ``python_path``, выбранных вариантов и ``non_waivable``.
    """

    key: str
    source: str
    operation_id: str | None = None
    method: str | None = None
    path: str | None = None
    request_content_type: str | None = None
    responses: tuple[ResponseSelector, ...] = ()
    python_path: tuple[str, ...] = ()
    non_waivable: tuple[NonWaivableAssertion, ...] = ()
    #: Генерировать ли для операции d42-схемы. Отключается явно, если контракт
    #: рекурсивен: d42 не умеет выражать рекурсию, и молча пропустить это нельзя.
    d42: bool = True

    @classmethod
    def from_dict(cls, key: str, data: Any) -> OperationSpec:
        where = f"operations.{key}"
        data = _require_mapping(data, where)
        _reject_unknown(
            data,
            {
                "source",
                "operation_id",
                "method",
                "path",
                "request",
                "responses",
                "python_path",
                "non_waivable",
                "d42",
            },
            where,
        )
        source = _require_str(data, "source", where)

        method = data.get("method")
        if method is not None:
            if not isinstance(method, str):
                raise ManifestError(f"{where}.method: ожидалась строка")
            method = method.upper()

        request_content_type: str | None = None
        if "request" in data:
            request = _require_mapping(data["request"], f"{where}.request")
            _reject_unknown(request, {"content_type"}, f"{where}.request")
            raw = request.get("content_type")
            if raw is not None and not isinstance(raw, str):
                raise ManifestError(f"{where}.request.content_type: ожидалась строка")
            request_content_type = raw

        responses: list[ResponseSelector] = []
        raw_responses = data.get("responses", [])
        if not isinstance(raw_responses, list):
            raise ManifestError(f"{where}.responses: ожидался список")
        for index, item in enumerate(raw_responses):
            responses.append(ResponseSelector.from_dict(item, f"{where}.responses[{index}]"))
        seen: set[tuple[int | str, str | None]] = set()
        for selector in responses:
            marker = (selector.status, selector.content_type)
            if marker in seen:
                raise ManifestError(f"{where}.responses: дубликат варианта {selector.label()}")
            seen.add(marker)

        python_path = _parse_python_path(data.get("python_path"), key, where)

        non_waivable: list[NonWaivableAssertion] = []
        raw_non_waivable = data.get("non_waivable", [])
        if not isinstance(raw_non_waivable, list):
            raise ManifestError(f"{where}.non_waivable: ожидался список")
        for index, item in enumerate(raw_non_waivable):
            non_waivable.append(
                NonWaivableAssertion.from_dict(item, f"{where}.non_waivable[{index}]")
            )

        d42_enabled = data.get("d42", True)
        if not isinstance(d42_enabled, bool):
            raise ManifestError(f"{where}.d42: ожидалось булево значение")

        operation_id = data.get("operation_id")
        if operation_id is not None and not isinstance(operation_id, str):
            raise ManifestError(f"{where}.operation_id: ожидалась строка")
        route = data.get("path")
        if route is not None and not isinstance(route, str):
            raise ManifestError(f"{where}.path: ожидалась строка")

        return cls(
            key=key,
            source=source,
            operation_id=operation_id,
            method=method,
            path=route,
            request_content_type=request_content_type,
            responses=tuple(responses),
            python_path=python_path,
            non_waivable=tuple(non_waivable),
            d42=d42_enabled,
        )

    def resolved_python_path(self) -> tuple[str, ...]:
        """Явный ``python_path`` либо выведенный из ключа."""
        return self.python_path or python_path_from_key(self.key)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"source": self.source}
        if self.operation_id is not None:
            data["operation_id"] = self.operation_id
        if self.method is not None:
            data["method"] = self.method
        if self.path is not None:
            data["path"] = self.path
        if self.request_content_type is not None:
            data["request"] = {"content_type": self.request_content_type}
        if self.responses:
            data["responses"] = [item.to_dict() for item in self.responses]
        if self.python_path:
            data["python_path"] = list(self.python_path)
        if self.non_waivable:
            data["non_waivable"] = [item.to_dict() for item in self.non_waivable]
        if not self.d42:
            data["d42"] = False
        return data


def _parse_python_path(raw: Any, key: str, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) < 2 or not all(isinstance(x, str) for x in raw):
        raise ManifestError(
            f"{where}.python_path: ожидался список минимум из двух строк, "
            f"например ['ws2', 'add_ticket']"
        )
    segments = tuple(raw)
    for index, segment in enumerate(segments):
        python_identifier(segment, context=f"{where}.python_path[{index}] для ключа {key!r}")
    # Явный python_path — тоже путь в generated namespace: зарезервированные имена
    # ломают его так же, как выведенные из ключа.
    check_namespace_names(segments, context=f"{where}.python_path для ключа {key!r}")
    return segments


@dataclass(frozen=True, kw_only=True, slots=True)
class Manifest:
    """Разобранный и провалидированный manifest."""

    #: Каталог, относительно которого разрешаются все пути.
    base_dir: Path
    #: Каталог generated-артефактов относительно ``base_dir``.
    output_directory: str
    #: Python-пакет, соответствующий ``output_directory``.
    output_package: str
    policies: Policies
    sources: tuple[SourceSpec, ...]
    operations: tuple[OperationSpec, ...]
    #: Путь к файлу waivers относительно ``base_dir``.
    waivers_path: str = "waivers.yaml"
    _by_key: dict[str, OperationSpec] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        by_key: dict[str, OperationSpec] = {}
        source_names = {source.name for source in self.sources}
        for spec in self.operations:
            if spec.key in by_key:
                raise ManifestError(f"operations: дубликат ключа {spec.key!r}")
            if spec.source not in source_names:
                raise ManifestError(
                    f"operations.{spec.key}: источник {spec.source!r} не объявлен в sources"
                )
            by_key[spec.key] = spec
        object.__setattr__(self, "_by_key", by_key)
        self._check_python_path_collisions()

    def _check_python_path_collisions(self) -> None:
        """Убедиться, что generated namespace строится без коллизий."""
        from .errors import NamespaceCollisionError

        owners: dict[tuple[str, ...], str] = {}
        namespaces: dict[tuple[str, ...], str] = {}
        for spec in sorted(self.operations, key=lambda item: item.key):
            path = spec.resolved_python_path()
            if path in owners:
                raise NamespaceCollisionError(
                    f"операции {owners[path]!r} и {spec.key!r} дают одинаковый Python path "
                    f"{'.'.join(path)}. Задайте python_path в manifest"
                )
            owners[path] = spec.key
            for depth in range(1, len(path)):
                namespaces.setdefault(path[:depth], spec.key)
        for path, owner in owners.items():
            if path in namespaces:
                raise NamespaceCollisionError(
                    f"операция {owner!r} занимает имя {'.'.join(path)}, которое уже используется "
                    f"как namespace операцией {namespaces[path]!r}. Задайте python_path в manifest"
                )

    def source(self, name: str) -> SourceSpec:
        """Найти источник по имени."""
        for item in self.sources:
            if item.name == name:
                return item
        raise ManifestError(f"источник {name!r} не объявлен в sources")

    def operation(self, key: str) -> OperationSpec | None:
        """Найти запись операции по ключу."""
        return self._by_key.get(key)

    def source_path(self, name: str) -> Path:
        """Абсолютный путь к файлу спецификации источника."""
        return (self.base_dir / self.source(name).path).resolve()

    def source_root(self, name: str) -> Path:
        """Абсолютный корень, за который не выходят межфайловые ``$ref``."""
        spec = self.source(name)
        if spec.root is not None:
            return (self.base_dir / spec.root).resolve()
        return self.source_path(name).parent

    def output_dir(self) -> Path:
        """Абсолютный каталог generated-артефактов."""
        return (self.base_dir / self.output_directory).resolve()

    def to_dict(self) -> dict[str, Any]:
        """Каноническое представление manifest (детерминированный порядок ключей)."""
        return {
            "version": MANIFEST_VERSION,
            "output": {"directory": self.output_directory, "package": self.output_package},
            "waivers": self.waivers_path,
            "policies": self.policies.to_dict(),
            "sources": {
                source.name: source.to_dict()
                for source in sorted(self.sources, key=lambda s: s.name)
            },
            "operations": {
                spec.key: spec.to_dict() for spec in sorted(self.operations, key=lambda s: s.key)
            },
        }

    @classmethod
    def from_dict(cls, data: Any, *, base_dir: Path) -> Manifest:
        """Разобрать документ manifest."""
        data = _require_mapping(data, "manifest")
        _reject_unknown(
            data, {"version", "output", "policies", "sources", "operations", "waivers"}, "manifest"
        )
        version = data.get("version")
        # ``bool`` — подкласс ``int``, а ``1.0 == 1``: без явной проверки типа
        # ``version: true`` и ``version: 1.0`` молча прошли бы как версия 1.
        if isinstance(version, bool) or not isinstance(version, int) or version != MANIFEST_VERSION:
            raise ManifestError(
                f"manifest.version: поддерживается только целое {MANIFEST_VERSION}, "
                f"получено {version!r}"
            )

        output = _require_mapping(data.get("output"), "manifest.output")
        _reject_unknown(output, {"directory", "package"}, "manifest.output")
        directory = _require_str(output, "directory", "manifest.output")
        package = _require_str(output, "package", "manifest.output")
        for index, part in enumerate(package.split(".")):
            python_identifier(part, context=f"manifest.output.package, сегмент #{index}")

        waivers_path = data.get("waivers", "waivers.yaml")
        if not isinstance(waivers_path, str) or not waivers_path:
            raise ManifestError("manifest.waivers: ожидалась непустая строка")

        raw_sources = _require_mapping(data.get("sources"), "manifest.sources")
        if not raw_sources:
            raise ManifestError("manifest.sources: должен быть объявлен хотя бы один источник")
        sources = tuple(
            SourceSpec.from_dict(name, value) for name, value in sorted(raw_sources.items())
        )

        raw_operations = data.get("operations") or {}
        raw_operations = _require_mapping(raw_operations, "manifest.operations")
        operations = tuple(
            OperationSpec.from_dict(key, value) for key, value in sorted(raw_operations.items())
        )

        explicit_sources = {s.name for s in sources if s.selection is Selection.EXPLICIT}
        covered = {spec.source for spec in operations}
        uncovered = sorted(explicit_sources - covered)
        if uncovered and operations:
            # Пустой explicit-источник допустим сразу после init, но если операции уже
            # есть, «забытый» источник почти наверняка означает опечатку.
            raise ManifestError(
                f"manifest.sources: источники {uncovered} объявлены с selection=explicit, "
                f"но ни одна операция на них не ссылается"
            )

        return cls(
            base_dir=base_dir,
            output_directory=directory,
            output_package=package,
            policies=Policies.from_dict(data.get("policies")),
            sources=sources,
            operations=operations,
            waivers_path=waivers_path,
        )


def _load_document(path: Path) -> Any:
    """Прочитать YAML или JSON. Используется только ``yaml.safe_load``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"не удалось прочитать manifest {path}: {exc}") from exc
    try:
        if path.suffix.lower() == ".json":
            return json.loads(text)
        return yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest {path} не разбирается: {exc}") from exc


def load_manifest(path: Path | str) -> Manifest:
    """Загрузить manifest из YAML или JSON."""
    manifest_path = Path(path).resolve()
    if not manifest_path.is_file():
        raise ManifestError(f"manifest не найден: {manifest_path}")
    return Manifest.from_dict(_load_document(manifest_path), base_dir=manifest_path.parent)


def dump_manifest(manifest: Manifest) -> str:
    """Сериализовать manifest в YAML детерминированно."""
    return yaml.safe_dump(
        manifest.to_dict(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )


def default_manifest_document(*, directory: str, package: str, source_path: str) -> dict[str, Any]:
    """Минимальный manifest, который печатает ``openapi-contracts init``."""
    return {
        "version": MANIFEST_VERSION,
        "output": {"directory": directory, "package": package},
        "waivers": "waivers.yaml",
        "policies": Policies().to_dict(),
        "sources": {"main": {"path": source_path, "selection": Selection.EXPLICIT.value}},
        "operations": {},
    }

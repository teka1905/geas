"""Waivers — точечные, срочные и закреплённые послабления контракта.

Waiver — единственный способ пропустить конструкцию, которую нормализация иначе
отклонила бы. Он намеренно неудобен:

* обязателен полный набор полей, включая владельца, причину, тикет и срок;
* срок жизни ограничен политикой проекта (по умолчанию 90 дней);
* просроченный, неполный, неиспользованный, избыточный или устаревший waiver
  ломает генерацию;
* waiver действует ровно для одной пары (операция, направление) и одного
  contract path с одним правилом — общий ``$ref`` для других операций не слабеет;
* ``expected_source`` закрепляет исходный фрагмент: если бэкенд поправил схему,
  waiver перестаёт действовать до явного пересмотра;
* пересечение с ``non_waivable`` из manifest запрещено.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .errors import ContractError, WaiverError
from .fingerprints import ANNOTATION_KEYS, digest, semantic_source_digest, strip_annotations
from .manifest import Manifest, NonWaivableAssertion
from .models import Direction
from .paths import ContractPath, format_contract_path, parse_contract_path

__all__ = [
    "WAIVERS_VERSION",
    "Waiver",
    "WaiverRule",
    "WaiverSet",
    "empty_waivers_document",
    "load_waivers",
    "waiver_source_digest",
]

#: Версия формата файла waivers.
WAIVERS_VERSION = 1


class WaiverRule(str, Enum):
    """Что именно послабляет waiver."""

    #: Разрешить неподдержанную конструкцию, заменив её на ``AnyNode``.
    ALLOW_ANY = "allow_any"
    #: Разрешить ``format``, которого нет в allowlist (валидация формата отключается).
    ALLOW_UNKNOWN_FORMAT = "allow_unknown_format"
    #: Заменить фрагмент схемы на явно заданный в waiver.
    REPLACE_SCHEMA = "replace_schema"
    #: Сделать поле необязательным, хотя спецификация объявляет его required.
    RELAX_REQUIRED = "relax_required"
    #: Не исключать ошибочно помеченное ``readOnly``-поле из запроса.
    IGNORE_READ_ONLY = "ignore_read_only"
    #: Не исключать ошибочно помеченное ``writeOnly``-поле из ответа.
    IGNORE_WRITE_ONLY = "ignore_write_only"
    #: Разрешить ``null`` в точке, где источник забыл nullable-маркер.
    ALLOW_NULL = "allow_null"
    #: Не применять ошибочный ``discriminator`` в выбранной операции.
    IGNORE_DISCRIMINATOR = "ignore_discriminator"
    #: Дополнить неполный enum явно перечисленными литералами.
    EXTEND_ENUM = "extend_enum"
    #: Добавить отсутствующее в исходной объектной схеме необязательное свойство.
    ADD_PROPERTY = "add_property"
    #: Разрешить неподдержанную сериализацию параметра (значение не проверяется).
    ALLOW_UNSUPPORTED_SERIALIZATION = "allow_unsupported_serialization"


_EXCLUSIVE_RULES = frozenset(
    {WaiverRule.ALLOW_ANY, WaiverRule.REPLACE_SCHEMA, WaiverRule.ADD_PROPERTY}
)


def waiver_source_digest(fragment: Any, rule: WaiverRule | str) -> str:
    """Посчитать ``expected_source`` с учётом семантики конкретного правила."""
    parsed = WaiverRule(rule)
    if parsed is WaiverRule.IGNORE_READ_ONLY:
        return digest(strip_annotations(fragment, keys=ANNOTATION_KEYS - {"readOnly"}))
    if parsed is WaiverRule.IGNORE_WRITE_ONLY:
        return digest(strip_annotations(fragment, keys=ANNOTATION_KEYS - {"writeOnly"}))
    return semantic_source_digest(fragment)


_REQUIRED_FIELDS = (
    "operation",
    "direction",
    "json_pointer",
    "rule",
    "reason",
    "owner",
    "expires_at",
)
_ALLOWED_FIELDS = frozenset(
    {
        *_REQUIRED_FIELDS,
        "issue",
        "ticket",
        "expected_source",
        "replacement",
        "values",
        "status",
        "content_type",
    }
)


def _parse_expiry(raw: Any, where: str) -> dt.date:
    """Разобрать ``expires_at``.

    Принимается только календарная дата. YAML сам превращает ``2026-12-31`` в
    :class:`datetime.date`, а ``2026-12-31T10:00:00Z`` — в :class:`datetime.datetime`,
    который является подклассом ``date``; без явной проверки он бы проскочил и
    сломал сравнение уже в рантайме.
    """
    if isinstance(raw, dt.datetime):
        raise WaiverError(
            f"{where}.expires_at: ожидалась календарная дата без времени, получено {raw!r}"
        )
    if isinstance(raw, dt.date):
        return raw
    if isinstance(raw, str):
        try:
            return dt.date.fromisoformat(raw)
        except ValueError as exc:
            raise WaiverError(f"{where}.expires_at: {raw!r} не разбирается как YYYY-MM-DD") from exc
    raise WaiverError(f"{where}.expires_at: ожидалась дата YYYY-MM-DD, получено {raw!r}")


@dataclass(frozen=True, kw_only=True, slots=True)
class Waiver:
    """Одно послабление."""

    operation: str
    direction: Direction
    path: ContractPath
    rule: WaiverRule
    reason: str
    owner: str
    issue: str
    expires_at: dt.date
    #: Отпечаток исходного фрагмента спецификации на момент выписки waiver'а.
    expected_source: str
    #: Явная замена схемы — только для :attr:`WaiverRule.REPLACE_SCHEMA`.
    replacement: Any = None
    #: Литералы для :attr:`WaiverRule.EXTEND_ENUM`.
    values: tuple[Any, ...] = ()
    #: Сужение до конкретного статуса ответа. ``None`` — все варианты направления.
    status: int | str | None = None
    #: Сужение до конкретного content type. ``None`` — все варианты направления.
    content_type: str | None = None

    @property
    def variant(self) -> tuple[int | str | None, str | None]:
        """Вариант, к которому привязан waiver."""
        return (self.status, self.content_type)

    @property
    def scope(self) -> tuple[str, Direction, ContractPath, int | str | None, str | None]:
        """Область действия без учёта правила."""
        return (self.operation, self.direction, self.path, self.status, self.content_type)

    @property
    def identity(self) -> tuple[Any, ...]:
        """Полный ключ уникальности."""
        return (*self.scope, self.rule)

    def matches_variant(self, status: int | str | None, content_type: str | None) -> bool:
        """Применим ли waiver к конкретному варианту (статус, content type)."""
        if self.status is not None and self.status != status:
            return False
        return not (self.content_type is not None and self.content_type != content_type)

    def describe(self) -> str:
        """Короткое описание для сообщений об ошибках."""
        variant = ""
        if self.status is not None or self.content_type is not None:
            variant = (
                f" [{self.status if self.status is not None else '*'}:{self.content_type or '*'}]"
            )
        return (
            f"{self.operation} / {self.direction.value} / "
            f"{format_contract_path(self.path)} / {self.rule.value}{variant}"
        )

    @classmethod
    def from_dict(cls, data: Any, where: str) -> Waiver:
        if not isinstance(data, dict):
            raise WaiverError(f"{where}: ожидался объект")
        unknown = sorted(set(data) - _ALLOWED_FIELDS)
        if unknown:
            raise WaiverError(f"{where}: неизвестные поля {unknown}")
        missing = [name for name in _REQUIRED_FIELDS if not data.get(name)]
        if missing:
            raise WaiverError(
                f"{where}: не заполнены обязательные поля {missing}. Неполный waiver не принимается"
            )
        issue = data.get("issue") or data.get("ticket")
        if not issue or not isinstance(issue, str):
            raise WaiverError(f"{where}: обязательно поле 'issue' (или 'ticket') со ссылкой")
        if "issue" in data and "ticket" in data:
            raise WaiverError(f"{where}: укажите либо 'issue', либо 'ticket', но не оба")

        try:
            direction = Direction(data["direction"])
        except ValueError as exc:
            raise WaiverError(f"{where}.direction: ожидалось 'request' или 'response'") from exc
        try:
            rule = WaiverRule(data["rule"])
        except ValueError as exc:
            allowed = [item.value for item in WaiverRule]
            raise WaiverError(f"{where}.rule: {data['rule']!r} не входит в {allowed}") from exc

        expected_source = data.get("expected_source")
        if not isinstance(expected_source, str) or len(expected_source) != 64:
            raise WaiverError(
                f"{where}.expected_source: обязателен sha256-отпечаток исходного фрагмента "
                f"(64 hex-символа). Точное значение печатает сообщение об ошибке генерации"
            )

        replacement = data.get("replacement")
        replacement_rules = {WaiverRule.REPLACE_SCHEMA, WaiverRule.ADD_PROPERTY}
        if rule in replacement_rules and replacement is None:
            raise WaiverError(f"{where}: правило {rule.value} требует поля 'replacement'")
        if rule not in replacement_rules and replacement is not None:
            allowed_rules = ", ".join(sorted(item.value for item in replacement_rules))
            raise WaiverError(
                f"{where}: поле 'replacement' допустимо только для правил {allowed_rules}"
            )

        raw_values = data.get("values")
        if rule is WaiverRule.EXTEND_ENUM:
            if not isinstance(raw_values, list) or not raw_values:
                raise WaiverError(f"{where}: правило extend_enum требует непустого списка 'values'")
            values = tuple(raw_values)
        else:
            if raw_values is not None:
                raise WaiverError(f"{where}: поле 'values' допустимо только для extend_enum")
            values = ()

        for name in ("reason", "owner", "operation"):
            if not isinstance(data[name], str):
                raise WaiverError(f"{where}.{name}: ожидалась строка")

        status = data.get("status")
        if status is not None and (isinstance(status, bool) or not isinstance(status, (int, str))):
            raise WaiverError(f"{where}.status: ожидался код ответа или 'default'")

        content_type = data.get("content_type")
        if content_type is not None and not isinstance(content_type, str):
            raise WaiverError(f"{where}.content_type: ожидалась строка")

        try:
            path = parse_contract_path(data["json_pointer"])
        except ContractError as exc:
            # Битый указатель — это ошибка waiver'а: без обёртки потребитель
            # получил бы ContractError без имени файла и индекса записи.
            raise WaiverError(f"{where}.json_pointer: {exc}") from exc

        return cls(
            operation=data["operation"],
            direction=direction,
            path=path,
            rule=rule,
            reason=data["reason"],
            owner=data["owner"],
            issue=issue,
            expires_at=_parse_expiry(data["expires_at"], where),
            expected_source=expected_source,
            replacement=replacement,
            values=values,
            status=status,
            content_type=content_type,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "operation": self.operation,
            "direction": self.direction.value,
            "json_pointer": format_contract_path(self.path),
            "rule": self.rule.value,
            "reason": self.reason,
            "owner": self.owner,
            "issue": self.issue,
            "expires_at": self.expires_at.isoformat(),
            "expected_source": self.expected_source,
        }
        if self.replacement is not None:
            data["replacement"] = self.replacement
        if self.values:
            data["values"] = list(self.values)
        if self.status is not None:
            data["status"] = self.status
        if self.content_type is not None:
            data["content_type"] = self.content_type
        return data


@dataclass(slots=True)
class WaiverSet:
    """Набор waiver'ов с учётом использования.

    Использование фиксируется в момент обращения нормализатора. После генерации
    :meth:`assert_all_used` требует, чтобы каждый waiver действительно
    понадобился — иначе он просто накапливается в репозитории.
    """

    waivers: tuple[Waiver, ...] = ()
    _used: set[tuple[Any, ...]] = field(default_factory=set)
    _index: dict[tuple[str, Direction, ContractPath, WaiverRule], list[Waiver]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        index: dict[tuple[str, Direction, ContractPath, WaiverRule], list[Waiver]] = {}
        identities: set[tuple[Any, ...]] = set()
        by_scope: dict[tuple[Any, ...], list[Waiver]] = {}
        for waiver in self.waivers:
            if waiver.identity in identities:
                raise WaiverError(f"избыточный waiver: {waiver.describe()} объявлен дважды")
            others = by_scope.get(waiver.scope, [])
            conflict = next(
                (
                    other
                    for other in others
                    if other.rule in _EXCLUSIVE_RULES or waiver.rule in _EXCLUSIVE_RULES
                ),
                None,
            )
            if conflict is not None:
                raise WaiverError(
                    f"на одну точку контракта навешены несовместимые правила: "
                    f"{conflict.rule.value} и "
                    f"{waiver.rule.value} для {format_contract_path(waiver.path)}. "
                    f"Замена или снятие всего ограничения должны оставаться единственным "
                    f"правилом в этой точке"
                )
            identities.add(waiver.identity)
            by_scope.setdefault(waiver.scope, []).append(waiver)
            index.setdefault(
                (waiver.operation, waiver.direction, waiver.path, waiver.rule), []
            ).append(waiver)
        self._index = index

    # ------------------------------------------------------------- валидация

    def validate(self, manifest: Manifest, *, today: dt.date) -> None:
        """Проверить сроки, политику и пересечение с ``non_waivable``."""
        limit = today + dt.timedelta(days=manifest.policies.waiver_max_days)
        known_keys = {spec.key for spec in manifest.operations}
        for waiver in self.waivers:
            if waiver.expires_at < today:
                raise WaiverError(
                    f"waiver истёк {waiver.expires_at.isoformat()}: {waiver.describe()}. "
                    f"Продлите его осознанно или почините контракт",
                    operation_key=waiver.operation,
                )
            if waiver.expires_at > limit:
                raise WaiverError(
                    f"waiver {waiver.describe()} выписан до {waiver.expires_at.isoformat()}, "
                    f"это дальше {manifest.policies.waiver_max_days} дней от сегодня "
                    f"(политика policies.waiver_max_days)",
                    operation_key=waiver.operation,
                )
            if known_keys and waiver.operation not in known_keys:
                # В режиме all операций в manifest может не быть — тогда проверку
                # делает сам генератор, когда узнает список ключей.
                continue
        self._check_non_waivable(manifest)

    def _check_non_waivable(self, manifest: Manifest) -> None:
        for spec in manifest.operations:
            for assertion in spec.non_waivable:
                for waiver in self.waivers:
                    if waiver.operation != spec.key or waiver.direction != assertion.direction:
                        continue
                    if _paths_overlap(waiver.path, assertion.path):
                        raise WaiverError(
                            f"waiver {waiver.describe()} пересекается с non_waivable "
                            f"{format_contract_path(assertion.path)} операции {spec.key!r}",
                            operation_key=spec.key,
                            direction=assertion.direction.value,
                        )

    def check_operations_known(self, keys: set[str]) -> None:
        """Убедиться, что каждый waiver ссылается на существующую операцию."""
        for waiver in self.waivers:
            if waiver.operation not in keys:
                raise WaiverError(
                    f"waiver ссылается на неизвестную операцию {waiver.operation!r}: "
                    f"{waiver.describe()}"
                )

    # -------------------------------------------------------------- работа

    def consult(
        self,
        *,
        operation: str,
        direction: Direction,
        path: ContractPath,
        rule: WaiverRule,
        source_digest: str,
        status: int | str | None = None,
        content_type: str | None = None,
    ) -> Waiver | None:
        """Найти waiver для точки контракта и отметить его использованным.

        Если отпечаток исходного фрагмента разошёлся с ``expected_source``,
        waiver считается устаревшим и генерация падает: значит источник
        поправили, и послабление надо пересмотреть, а не продлевать вслепую.
        """
        candidates = [
            item
            for item in self._index.get((operation, direction, path, rule), [])
            if item.matches_variant(status, content_type)
        ]
        if not candidates:
            return None
        # Более узкий waiver выигрывает у более общего.
        candidates.sort(key=lambda item: (item.status is None, item.content_type is None))
        waiver = candidates[0]
        if waiver.expected_source != source_digest:
            raise WaiverError(
                f"waiver {waiver.describe()} устарел: исходный фрагмент изменился.\n"
                f"  ожидался expected_source: {waiver.expected_source}\n"
                f"  фактический:              {source_digest}\n"
                f"Пересмотрите послабление и обновите expected_source осознанно. "
                f"Если waiver задумывался только для одного варианта ответа, сузьте его "
                f"полями status и content_type",
                operation_key=operation,
                direction=direction.value,
            )
        self._used.add(waiver.identity)
        return waiver

    def assert_all_used(self) -> None:
        """Потребовать, чтобы каждый waiver действительно понадобился."""
        unused = [w for w in self.waivers if w.identity not in self._used]
        if unused:
            listing = "\n".join(f"  - {w.describe()}" for w in unused)
            raise WaiverError(
                "waiver'ы больше не нужны — контракт починился или точка изменилась.\n"
                f"{listing}\nУдалите их из waivers.yaml"
            )

    def reset_usage(self) -> None:
        """Сбросить отметки использования (нужно между прогонами генерации)."""
        self._used.clear()

    def to_dict(self) -> dict[str, Any]:
        """Каноническое представление файла waivers."""
        ordered = sorted(
            self.waivers,
            key=lambda w: (
                w.operation,
                w.direction.value,
                format_contract_path(w.path),
                str(w.status),
                w.content_type or "",
                w.rule.value,
            ),
        )
        return {"version": WAIVERS_VERSION, "waivers": [w.to_dict() for w in ordered]}


def _paths_overlap(left: ContractPath, right: ContractPath) -> bool:
    """Пересекаются ли области действия двух указателей (один — префикс другого)."""
    shortest = min(len(left), len(right))
    return left[:shortest] == right[:shortest]


def _load_document(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WaiverError(f"не удалось прочитать {path}: {exc}") from exc
    try:
        if path.suffix.lower() == ".json":
            return json.loads(text)
        return yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise WaiverError(f"{path} не разбирается: {exc}") from exc


def load_waivers(path: Path | str) -> WaiverSet:
    """Загрузить ``waivers.yaml``. Отсутствие файла — это пустой набор."""
    waivers_path = Path(path)
    if not waivers_path.exists():
        return WaiverSet()
    document = _load_document(waivers_path)
    if document is None:
        return WaiverSet()
    if not isinstance(document, dict):
        raise WaiverError(f"{waivers_path}: ожидался объект с ключами 'version' и 'waivers'")
    unknown = sorted(set(document) - {"version", "waivers"})
    if unknown:
        raise WaiverError(f"{waivers_path}: неизвестные ключи {unknown}")
    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != WAIVERS_VERSION:
        raise WaiverError(
            f"{waivers_path}: version должен быть целым {WAIVERS_VERSION}, получено {version!r}"
        )
    raw = document.get("waivers") or []
    if not isinstance(raw, list):
        raise WaiverError(f"{waivers_path}.waivers: ожидался список")
    items = tuple(
        Waiver.from_dict(item, f"{waivers_path.name}.waivers[{index}]")
        for index, item in enumerate(raw)
    )
    return WaiverSet(waivers=items)


def empty_waivers_document() -> str:
    """Пустой ``waivers.yaml``, который создаёт ``openapi-contracts init``."""
    return yaml.safe_dump(
        {"version": WAIVERS_VERSION, "waivers": []},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _unused_assertion_names(assertions: tuple[NonWaivableAssertion, ...]) -> list[str]:
    """Служебный помощник для отчётов CLI."""
    return [format_contract_path(item.path) for item in assertions]

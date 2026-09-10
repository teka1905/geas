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

**Область действия.** По умолчанию waiver закреплён за одной точкой контракта
(``scope: exact``). Один дефект корневой модели — например, генератор Swagger 2,
пометивший всю request-модель как ``readOnly``, — иначе требовал бы по записи на
каждый лист, и файл становился бы нередактируемым. Для таких случаев есть
``scope: subtree``: waiver закрепляется за узлом и всем, что под ним. Плата за
широту — отпечаток: ``expected_source`` subtree-waiver'а считается по всему
поддереву с развёрнутыми ``$ref``, поэтому любое изменение внутри ломает
генерацию явно, а не проскакивает молча.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
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
    "SUBTREE_RULES",
    "WAIVERS_VERSION",
    "ExpiringWaiver",
    "Waiver",
    "WaiverRule",
    "WaiverScope",
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


class WaiverScope(str, Enum):
    """Область действия waiver'а внутри контракта."""

    #: Ровно одна точка контракта — поведение по умолчанию и до версии 0.3.0.
    EXACT = "exact"
    #: Узел и всё поддерево под ним.
    SUBTREE = "subtree"


#: Правила, которые допускают ``scope: subtree``.
#:
#: Общий признак — правило осмысленно применяется к каждой точке поддерева по
#: отдельности и не зависит от того, что именно в этой точке стоит. Правила,
#: подставляющие конкретное содержимое (``replace_schema``, ``add_property``,
#: ``extend_enum``), сюда не входят: «заменить всё поддерево на эту схему» —
#: бессмыслица, а не послабление. ``allow_any`` не входит по другой причине:
#: он и так съедает поддерево целиком, обход внутрь просто не доходит.
SUBTREE_RULES = frozenset(
    {
        WaiverRule.IGNORE_READ_ONLY,
        WaiverRule.IGNORE_WRITE_ONLY,
        WaiverRule.RELAX_REQUIRED,
    }
)

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
        "scope",
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
    #: Область действия: одна точка либо всё поддерево под :attr:`path`.
    scope: WaiverScope = WaiverScope.EXACT
    #: Сужение до конкретного статуса ответа. ``None`` — все варианты направления.
    status: int | str | None = None
    #: Сужение до конкретного content type. ``None`` — все варианты направления.
    content_type: str | None = None

    @property
    def variant(self) -> tuple[int | str | None, str | None]:
        """Вариант, к которому привязан waiver."""
        return (self.status, self.content_type)

    @property
    def target(self) -> tuple[str, Direction, ContractPath, int | str | None, str | None]:
        """Куда нацелен waiver — без учёта правила и ширины области."""
        return (self.operation, self.direction, self.path, self.status, self.content_type)

    @property
    def identity(self) -> tuple[Any, ...]:
        """Полный ключ уникальности.

        ``scope`` в ключ намеренно не входит: ``exact`` и ``subtree`` на одной и
        той же точке с одним правилом — это не два разных waiver'а, а
        двусмысленность. Она отсекается как дубликат ещё при загрузке, поэтому
        :meth:`WaiverSet.consult` не может оказаться перед выбором наугад.
        """
        return (*self.target, self.rule)

    def matches_variant(self, status: int | str | None, content_type: str | None) -> bool:
        """Применим ли waiver к конкретному варианту (статус, content type)."""
        if self.status is not None and self.status != status:
            return False
        return not (self.content_type is not None and self.content_type != content_type)

    def covers(self, path: ContractPath) -> bool:
        """Попадает ли точка контракта в область действия waiver'а."""
        if self.scope is WaiverScope.SUBTREE:
            return path[: len(self.path)] == self.path
        return path == self.path

    def describe(self) -> str:
        """Короткое описание для сообщений об ошибках."""
        variant = ""
        if self.status is not None or self.content_type is not None:
            variant = (
                f" [{self.status if self.status is not None else '*'}:{self.content_type or '*'}]"
            )
        scope = " (поддерево)" if self.scope is WaiverScope.SUBTREE else ""
        return (
            f"{self.operation} / {self.direction.value} / "
            f"{format_contract_path(self.path)} / {self.rule.value}{variant}{scope}"
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

        raw_scope = data.get("scope", WaiverScope.EXACT.value)
        try:
            scope = WaiverScope(raw_scope)
        except ValueError as exc:
            allowed = [item.value for item in WaiverScope]
            raise WaiverError(f"{where}.scope: {raw_scope!r} не входит в {allowed}") from exc
        if scope is WaiverScope.SUBTREE and rule not in SUBTREE_RULES:
            allowed_rules = ", ".join(sorted(item.value for item in SUBTREE_RULES))
            raise WaiverError(
                f"{where}: правило {rule.value} не допускает 'scope: subtree'. "
                f"Поддеревом послабляются только правила, осмысленные в каждой точке "
                f"по отдельности ({allowed_rules}); {rule.value} задаёт содержимое "
                f"конкретного узла, и распространить его на всё поддерево нельзя. "
                f"Оставьте scope: exact и выпишите waiver на нужную точку"
            )

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
            scope=scope,
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
        if self.scope is not WaiverScope.EXACT:
            # ``exact`` не пишется: канонический вид файла у проектов, которые
            # subtree не используют, обязан остаться прежним.
            data["scope"] = self.scope.value
        if self.status is not None:
            data["status"] = self.status
        if self.content_type is not None:
            data["content_type"] = self.content_type
        return data


@dataclass(frozen=True, kw_only=True, slots=True)
class ExpiringWaiver:
    """Waiver, у которого срок истекает в ближайшем окне."""

    waiver: Waiver
    #: Сколько дней осталось; ``0`` — истекает сегодня.
    days_left: int

    def describe(self) -> str:
        """Строка для предупреждения CLI."""
        when = "истекает сегодня" if self.days_left == 0 else f"осталось дней: {self.days_left}"
        return (
            f"{self.waiver.describe()} — {when} "
            f"(expires_at: {self.waiver.expires_at.isoformat()}, "
            f"owner: {self.waiver.owner}, issue: {self.waiver.issue})"
        )


@dataclass(slots=True)
class WaiverSet:
    """Набор waiver'ов с учётом использования.

    Использование фиксируется в момент обращения нормализатора. После генерации
    :meth:`assert_all_used` требует, чтобы каждый waiver действительно
    понадобился — иначе он просто накапливается в репозитории.
    """

    waivers: tuple[Waiver, ...] = ()
    #: Точки контракта, на которых сработал waiver: identity → {(status, ct, path)}.
    _usage: dict[tuple[Any, ...], set[tuple[Any, ...]]] = field(default_factory=dict)
    _index: dict[tuple[str, Direction, ContractPath, WaiverRule], list[Waiver]] = field(
        default_factory=dict
    )
    #: Subtree-waiver'ы отдельно: их нельзя найти точным ключом.
    _subtree_index: dict[tuple[str, Direction, WaiverRule], list[Waiver]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        index: dict[tuple[str, Direction, ContractPath, WaiverRule], list[Waiver]] = {}
        subtree_index: dict[tuple[str, Direction, WaiverRule], list[Waiver]] = {}
        identities: set[tuple[Any, ...]] = set()
        by_target: dict[tuple[Any, ...], list[Waiver]] = {}
        for waiver in self.waivers:
            if waiver.identity in identities:
                raise WaiverError(f"избыточный waiver: {waiver.describe()} объявлен дважды")
            # Эксклюзивность считается по точке привязки, а не по области.
            # Subtree-правила (``ignore_read_only``, ``ignore_write_only``,
            # ``relax_required``) решают судьбу *слота* свойства в родительском
            # объекте — присутствие и обязательность, — а не содержимое узла.
            # Поэтому накрывающее поддерево не спорит с ``replace_schema`` или
            # ``add_property`` внутри себя: они правят разные вещи, и порядок
            # применения определён. Спорят только правила на одной точке.
            others = by_target.get(waiver.target, [])
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
            by_target.setdefault(waiver.target, []).append(waiver)
            if waiver.scope is WaiverScope.SUBTREE:
                subtree_index.setdefault(
                    (waiver.operation, waiver.direction, waiver.rule), []
                ).append(waiver)
            else:
                index.setdefault(
                    (waiver.operation, waiver.direction, waiver.path, waiver.rule), []
                ).append(waiver)
        self._index = index
        self._subtree_index = subtree_index

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
                    if not _paths_overlap(waiver.path, assertion.path):
                        continue
                    protected = format_contract_path(assertion.path)
                    detail = ""
                    if waiver.scope is WaiverScope.SUBTREE:
                        # Покрывающий waiver стоит выше защищённой точки, и по
                        # одному его пути непонятно, что именно он задел.
                        detail = (
                            f"\n  waiver покрывает поддерево: "
                            f"{format_contract_path(waiver.path)}"
                            f"\n  защищённая точка внутри:   {protected}"
                        )
                    raise WaiverError(
                        f"waiver {waiver.describe()} пересекается с non_waivable "
                        f"{protected} операции {spec.key!r}{detail}",
                        operation_key=spec.key,
                        direction=assertion.direction.value,
                    )

    def expiring(self, *, today: dt.date, warn_days: int) -> tuple[ExpiringWaiver, ...]:
        """Waiver'ы, которым осталось не больше ``warn_days`` дней.

        Это предупреждение, а не ошибка: дата истечения не должна наступать
        внезапно посреди чужого релиза. Просроченные сюда не попадают — их ловит
        :meth:`validate` и роняет сборку.
        """
        soon: list[ExpiringWaiver] = []
        for waiver in self.waivers:
            days_left = (waiver.expires_at - today).days
            if 0 <= days_left <= warn_days:
                soon.append(ExpiringWaiver(waiver=waiver, days_left=days_left))
        return tuple(sorted(soon, key=lambda item: (item.days_left, item.waiver.describe())))

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
        subtree_digest: Callable[[ContractPath, WaiverRule], str] | None = None,
    ) -> Waiver | None:
        """Найти waiver для точки контракта и отметить его использованным.

        Порядок поиска детерминирован: сначала точное совпадение, затем
        ближайший subtree-предок, затем более дальний. Точный waiver всегда
        выигрывает у покрывающего поддерева, а из двух поддеревьев — то, что
        ближе к точке.

        Если отпечаток исходного фрагмента разошёлся с ``expected_source``,
        waiver считается устаревшим и генерация падает: значит источник
        поправили, и послабление надо пересмотреть, а не продлевать вслепую. Для
        subtree-waiver'а сверяется отпечаток **всего поддерева**: его считает
        ``subtree_digest``, который передаёт нормализатор — только он знает
        исходный фрагмент, стоящий в точке привязки.
        """
        candidates = [
            item
            for item in self._index.get((operation, direction, path, rule), [])
            if item.matches_variant(status, content_type)
        ]
        if not candidates:
            candidates = self._subtree_candidates(
                operation=operation,
                direction=direction,
                path=path,
                rule=rule,
                status=status,
                content_type=content_type,
            )
        if not candidates:
            return None
        waiver = self._pick(candidates, path=path)

        expected_now = source_digest
        if waiver.scope is WaiverScope.SUBTREE:
            if subtree_digest is None:
                raise WaiverError(
                    f"waiver {waiver.describe()} объявлен с 'scope: subtree', но в этой точке "
                    f"контракта отпечаток поддерева посчитать нельзя. Сузьте waiver до "
                    f"scope: exact",
                    operation_key=operation,
                    direction=direction.value,
                )
            expected_now = subtree_digest(waiver.path, waiver.rule)

        if waiver.expected_source != expected_now:
            raise WaiverError(
                self._stale_message(waiver, path=path, actual=expected_now),
                operation_key=operation,
                direction=direction.value,
            )
        self._usage.setdefault(waiver.identity, set()).add((status, content_type, path))
        return waiver

    def _subtree_candidates(
        self,
        *,
        operation: str,
        direction: Direction,
        path: ContractPath,
        rule: WaiverRule,
        status: int | str | None,
        content_type: str | None,
    ) -> list[Waiver]:
        """Subtree-waiver'ы, накрывающие точку; остаются только ближайшие."""
        covering = [
            item
            for item in self._subtree_index.get((operation, direction, rule), ())
            if item.covers(path) and item.matches_variant(status, content_type)
        ]
        if not covering:
            return []
        nearest = max(len(item.path) for item in covering)
        return [item for item in covering if len(item.path) == nearest]

    @staticmethod
    def _pick(candidates: list[Waiver], *, path: ContractPath) -> Waiver:
        """Выбрать один waiver из равноправных кандидатов.

        Более узкий по варианту ответа выигрывает у более общего. Полная ничья
        означает, что выбор был бы сделан наугад, — это ошибка, а не «какой-то
        из них подойдёт».
        """

        def narrowness(item: Waiver) -> tuple[bool, bool]:
            return (item.status is None, item.content_type is None)

        ordered = sorted(candidates, key=narrowness)
        best = narrowness(ordered[0])
        tied = [item for item in ordered if narrowness(item) == best]
        if len(tied) > 1:
            listing = "\n".join(f"  - {item.describe()}" for item in tied)
            raise WaiverError(
                f"точку контракта {format_contract_path(path)} накрывают несколько "
                f"равнозначных waiver'ов, и выбор между ними был бы произвольным:\n"
                f"{listing}\nСузьте один из них полями status/content_type или json_pointer",
                operation_key=tied[0].operation,
                direction=tied[0].direction.value,
            )
        return ordered[0]

    @staticmethod
    def _stale_message(waiver: Waiver, *, path: ContractPath, actual: str) -> str:
        if waiver.scope is WaiverScope.SUBTREE:
            return (
                f"waiver {waiver.describe()} устарел: поддерево изменилось.\n"
                f"  waiver покрывает поддерево: {format_contract_path(waiver.path)}\n"
                f"  точка контракта:            {format_contract_path(path)}\n"
                f"  ожидался expected_source: {waiver.expected_source}\n"
                f"  фактический:              {actual}\n"
                f"Отпечаток subtree-waiver'а считается по всему поддереву, поэтому его "
                f"ломает любое изменение внутри — в том числе то, ради которого waiver "
                f"больше не нужен. Пересмотрите послабление и обновите expected_source "
                f"осознанно"
            )
        return (
            f"waiver {waiver.describe()} устарел: исходный фрагмент изменился.\n"
            f"  ожидался expected_source: {waiver.expected_source}\n"
            f"  фактический:              {actual}\n"
            f"Пересмотрите послабление и обновите expected_source осознанно. "
            f"Если waiver задумывался только для одного варианта ответа, сузьте его "
            f"полями status и content_type"
        )

    def coverage(self, waiver: Waiver) -> int:
        """Сколько точек контракта накрыл waiver за текущий прогон."""
        return len(self._usage.get(waiver.identity, ()))

    def subtree_coverage(self) -> tuple[tuple[Waiver, int], ...]:
        """Покрытие каждого subtree-waiver'а — то, на что смотрит ревьюер.

        Одна запись вместо семидесяти пяти читается легко, но по ней не видно,
        не слишком ли она широка. Число накрытых точек — ровно тот признак,
        который это показывает.
        """
        return tuple(
            (waiver, self.coverage(waiver))
            for waiver in self.waivers
            if waiver.scope is WaiverScope.SUBTREE
        )

    def assert_all_used(self) -> None:
        """Потребовать, чтобы каждый waiver действительно понадобился.

        Subtree-waiver считается использованным, если сработал хотя бы раз:
        поддерево — это заявка на область, а не на конкретное число точек.
        """
        unused = [w for w in self.waivers if not self._usage.get(w.identity)]
        if unused:
            listing = "\n".join(f"  - {w.describe()}" for w in unused)
            raise WaiverError(
                "waiver'ы больше не нужны — контракт починился или точка изменилась.\n"
                f"{listing}\nУдалите их из waivers.yaml"
            )

    def reset_usage(self) -> None:
        """Сбросить отметки использования (нужно между прогонами генерации)."""
        self._usage.clear()

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
    """Пустой ``waivers.yaml``, который создаёт ``geas init``."""
    return yaml.safe_dump(
        {"version": WAIVERS_VERSION, "waivers": []},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _unused_assertion_names(assertions: tuple[NonWaivableAssertion, ...]) -> list[str]:
    """Служебный помощник для отчётов CLI."""
    return [format_contract_path(item.path) for item in assertions]

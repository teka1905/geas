"""Семантический diff контрактов.

Отличает изменения **контракта** от изменений оформления. Правило простое:
семантика — это всё, что влияет на то, какие данные считаются валидными, и на то,
как операция называется в generated-коде. Всё остальное (диалект источника, слаг
файла, имена модулей d42) на отпечаток не влияет.

``description``, ``example``, ``default`` и порядок ключей в IR вообще не попадают,
поэтому их правка не меняет ни артефакты, ни отпечаток.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .fingerprints import canonical_json, digest

__all__ = [
    "Change",
    "ChangeKind",
    "ContractDiff",
    "OperationDiff",
    "diff_documents",
    "diff_operations",
    "semantic_fingerprint",
    "semantic_view",
]

#: Ключи документа контракта, не влияющие на семантику.
_NON_SEMANTIC_KEYS = frozenset({"artifact", "slug", "dialect", "d42"})


class ChangeKind(str, Enum):
    """Вид изменения."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(frozen=True, kw_only=True, slots=True)
class Change:
    """Одно изменение внутри документа контракта."""

    kind: ChangeKind
    pointer: str
    before: Any = None
    after: Any = None

    def describe(self) -> str:
        """Однострочное описание для вывода CLI."""
        if self.kind is ChangeKind.ADDED:
            return f"+ {self.pointer} = {canonical_json(self.after)}"
        if self.kind is ChangeKind.REMOVED:
            return f"- {self.pointer} = {canonical_json(self.before)}"
        return f"~ {self.pointer}: {canonical_json(self.before)} → {canonical_json(self.after)}"


@dataclass(frozen=True, kw_only=True, slots=True)
class OperationDiff:
    """Изменения одной операции."""

    key: str
    semantic: tuple[Change, ...]
    cosmetic: tuple[Change, ...]

    @property
    def is_semantic(self) -> bool:
        """Изменился ли контракт по существу."""
        return bool(self.semantic)


@dataclass(frozen=True, kw_only=True, slots=True)
class ContractDiff:
    """Полный diff между двумя наборами документов контрактов."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[OperationDiff, ...]

    @property
    def is_empty(self) -> bool:
        """Нет ли вообще никаких различий."""
        return not (self.added or self.removed or self.changed)

    @property
    def is_semantic(self) -> bool:
        """Есть ли изменения, меняющие контракт."""
        return bool(self.added or self.removed or any(item.is_semantic for item in self.changed))


def semantic_view(document: Mapping[str, Any]) -> dict[str, Any]:
    """Оставить от документа только то, что влияет на контракт."""
    return {key: value for key, value in document.items() if key not in _NON_SEMANTIC_KEYS}


def semantic_fingerprint(document: Mapping[str, Any]) -> str:
    """Отпечаток семантики одной операции."""
    return digest(semantic_view(document))


def diff_documents(before: Mapping[str, Any], after: Mapping[str, Any], key: str) -> OperationDiff:
    """Сравнить два документа одной операции."""
    changes = _walk(before, after, "")
    semantic: list[Change] = []
    cosmetic: list[Change] = []
    for change in changes:
        root = change.pointer.split("/")[1] if "/" in change.pointer else ""
        (cosmetic if root in _NON_SEMANTIC_KEYS else semantic).append(change)
    return OperationDiff(key=key, semantic=tuple(semantic), cosmetic=tuple(cosmetic))


def diff_operations(
    before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]]
) -> ContractDiff:
    """Сравнить два набора документов, ключ к ключу."""
    added = tuple(sorted(set(after) - set(before)))
    removed = tuple(sorted(set(before) - set(after)))
    changed: list[OperationDiff] = []
    for key in sorted(set(before) & set(after)):
        item = diff_documents(before[key], after[key], key)
        if item.semantic or item.cosmetic:
            changed.append(item)
    return ContractDiff(added=added, removed=removed, changed=tuple(changed))


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _walk(before: Any, after: Any, pointer: str) -> list[Change]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[Change] = []
        for name in sorted(set(before) | set(after)):
            child = f"{pointer}/{_escape(str(name))}"
            if name not in after:
                changes.append(Change(kind=ChangeKind.REMOVED, pointer=child, before=before[name]))
            elif name not in before:
                changes.append(Change(kind=ChangeKind.ADDED, pointer=child, after=after[name]))
            else:
                changes.extend(_walk(before[name], after[name], child))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes = []
        for index in range(max(len(before), len(after))):
            child = f"{pointer}/{index}"
            if index >= len(after):
                changes.append(Change(kind=ChangeKind.REMOVED, pointer=child, before=before[index]))
            elif index >= len(before):
                changes.append(Change(kind=ChangeKind.ADDED, pointer=child, after=after[index]))
            else:
                changes.extend(_walk(before[index], after[index], child))
        return changes
    if before != after:
        return [Change(kind=ChangeKind.CHANGED, pointer=pointer or "/", before=before, after=after)]
    return []

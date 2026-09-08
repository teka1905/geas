"""Тонкий адаптер над публичным API JJ.

Вся работа с JJ сосредоточена здесь. Это сделано намеренно: если однажды
понадобится приватное API JJ, оно окажется ровно в одном version-checked месте,
а не размажется по всей библиотеке.

На проверенном диапазоне ``jj>=2.9,<3`` приватное API **не требуется**. Всё, что
нужно, публично: :func:`jj.match`, :class:`jj.Response`, :func:`jj.mock.mocked`,
а у ``Mocked`` — свойства ``history``, методы ``fetch_history()`` и
``wait_for_requests()``. У элемента истории публичны ``method``, ``path``,
``segments``, ``params``, ``headers``, ``body`` и ``raw``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...errors import ContractMockError, MissingExtraError

__all__ = [
    "SUPPORTED_JJ",
    "build_matcher",
    "build_response",
    "check_version",
    "create_mocked",
    "history_request_parts",
]

#: Проверенный диапазон версий JJ.
SUPPORTED_JJ = ((2, 9), (3, 0))


def _import_jj() -> Any:
    try:
        import jj
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise MissingExtraError("jj", "operation-aware моки") from exc
    return jj


def _parse_version(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check_version() -> str:
    """Убедиться, что установленная версия JJ входит в проверенный диапазон."""
    jj = _import_jj()
    raw = str(getattr(jj, "version", "0"))
    version = _parse_version(raw)
    low, high = SUPPORTED_JJ
    if not version or not (low <= version[: len(low)] < high):
        raise ContractMockError(
            f"установлен jj {raw}, а библиотека проверена на "
            f">={'.'.join(map(str, low))},<{'.'.join(map(str, high))}. "
            f"Обновите jj или зафиксируйте совместимую версию"
        )
    return raw


def build_matcher(
    *,
    method: str,
    path: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, Any] | None = None,
) -> Any:
    """Собрать matcher запроса.

    ``path`` может содержать шаблонные сегменты ``{name}``: JJ разбирает их сам и
    отдаёт значения в ``segments`` элемента истории.
    """
    jj = _import_jj()
    return jj.match(
        method=method,
        path=path,
        params={key: str(value) for key, value in (params or {}).items()} or None,
        headers={key: str(value) for key, value in (headers or {}).items()} or None,
    )


def build_response(
    *,
    status: int,
    body: Any,
    has_body: bool,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Собрать ответ мока.

    Для варианта без тела (например 204) ``json`` не передаётся вовсе: иначе JJ
    отдал бы литерал ``null``, которого в контракте нет.
    """
    jj = _import_jj()
    if has_body:
        return jj.Response(status=status, json=body, headers=dict(headers or {}))
    return jj.Response(status=status, headers=dict(headers or {}))


def create_mocked(matcher: Any, response: Any) -> Any:
    """Создать ``jj.Mocked``.

    ``disposable=True`` задаётся явно, потому что по умолчанию JJ читает его из
    переменной окружения: v0.1 не поддерживает persistent-моки и не должен зависеть
    от настроек машины.
    """
    from jj.mock import mocked

    return mocked(matcher, response, disposable=True, prefetch_history=True)


def history_request_parts(item: Any) -> dict[str, Any]:
    """Разобрать элемент истории JJ в диалектно-нейтральные части.

    Элемент истории — это ``TypedDict`` с ключами ``request``/``response``.
    Работаем только с публичными свойствами запроса.
    """
    request = item["request"] if isinstance(item, dict) else item.request
    return {
        "method": request.method,
        "path": request.path,
        "segments": dict(request.segments or {}),
        "params": _pairs(request.params),
        "headers": _pairs(request.headers),
        "body": request.body,
        "raw": getattr(request, "raw", None),
    }


def _pairs(multi: Any) -> list[tuple[str, str]]:
    if multi is None:
        return []
    if hasattr(multi, "items"):
        return [(str(key), str(value)) for key, value in multi.items()]
    return [(str(key), str(value)) for key, value in multi]


def _unused(_: Sequence[Any]) -> None:  # pragma: no cover
    """Служебная заглушка, чтобы typing-импорты не считались лишними."""

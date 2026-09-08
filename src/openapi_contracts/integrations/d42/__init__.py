"""Опциональная интеграция с d42 2.x.

Пакет включается только при установленном extra ``d42``
(``pip install 'openapi-contract-fixtures[d42]'``); без него импорт падает
:class:`~openapi_contracts.errors.MissingExtraError` — понятной ошибкой вместо
``ModuleNotFoundError`` из недр библиотеки.

Состав:

* :func:`to_d42` — IR → живые объекты схем d42;
* :func:`render_module` / :func:`render_expression` — IR → детерминированный
  исходник Python со схемами;
* :func:`overlay_generators` и маркер :data:`EACH` — подмена генератора отдельного
  листа с сохранением контракта;
* :func:`build_fixture` — детерминированная генерация значения по схеме.

Границы ответственности d42 описаны в докстринге
:mod:`openapi_contracts.integrations.d42.converter`: часть ограничений OpenAPI в
d42 не выражается и держится на JSON-Schema-пути, остальное отклоняется явной
ошибкой, а не тихо ослабляется.
"""

from __future__ import annotations

from openapi_contracts.errors import MissingExtraError

try:  # pragma: no cover — ветка проверяется только на окружении без d42
    import d42 as _d42  # noqa: F401
except ImportError as error:  # pragma: no cover
    raise MissingExtraError("d42", "Интеграция с d42") from error

from openapi_contracts.integrations.d42.converter import to_d42
from openapi_contracts.integrations.d42.fixtures import build_fixture
from openapi_contracts.integrations.d42.overlays import EACH, overlay_generators
from openapi_contracts.integrations.d42.renderer import render_expression, render_module

__all__ = [
    "EACH",
    "build_fixture",
    "overlay_generators",
    "render_expression",
    "render_module",
    "to_d42",
]

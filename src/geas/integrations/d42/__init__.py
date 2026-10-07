"""Опциональная интеграция с d42 2.x.

Пакет включается только при установленном extra ``d42``
(``pip install 'geas[d42]'``); без него импорт падает
:class:`~geas.errors.MissingExtraError` — понятной ошибкой вместо
``ModuleNotFoundError`` из недр библиотеки.

Состав:

* :func:`to_d42` — IR → живые объекты схем d42;
* :func:`render_module` / :func:`render_expression` — IR → детерминированный
  исходник Python со схемами;
* :func:`overlay_generators` и маркер :data:`EACH` — подмена генератора отдельного
  листа с сохранением контракта;
* :func:`build_fixture` — детерминированная генерация значения по схеме;
* :class:`RecursionContract` и :class:`RecursiveRefSchema` — отсечка цикла в
  рекурсивных контрактах (см. :mod:`geas.integrations.d42.recursion`);
* :data:`json_int` / :class:`JsonIntSchema` — целочисленная ветка ``number``:
  ``schema.int`` без ``bool``.

Нужна d42 не ниже 2.3.0: интеграция опирается на ``optional.absent``
(``d42.declaration.types.is_absent``). На более старой d42 импорт падает
:class:`~geas.errors.MissingExtraError` с установленной версией, а не ложным
«установите extra».

Границы ответственности d42 описаны в докстринге
:mod:`geas.integrations.d42.converter`: часть ограничений OpenAPI в
d42 не выражается и держится на JSON-Schema-пути, остальное отклоняется явной
ошибкой, а не тихо ослабляется.
"""

from __future__ import annotations

from geas.errors import MissingExtraError

try:  # pragma: no cover — ветка проверяется только на окружении без d42
    import d42 as _d42
except ImportError as error:  # pragma: no cover
    raise MissingExtraError("d42", "Интеграция с d42") from error

try:
    from d42.declaration.types import is_absent as _is_absent  # noqa: F401
except ImportError as error:
    _installed = getattr(_d42, "__version__", "неизвестной версии")
    raise MissingExtraError(
        "d42", "Интеграция с d42", requirement=f"d42>=2.3,<3, а установлена d42 {_installed}"
    ) from error

from geas.integrations.d42.converter import to_d42
from geas.integrations.d42.fixtures import build_fixture
from geas.integrations.d42.json_int import JsonIntSchema, json_int
from geas.integrations.d42.overlays import EACH, overlay_generators
from geas.integrations.d42.recursion import RecursionContract, RecursiveRefSchema
from geas.integrations.d42.renderer import render_expression, render_module
from geas.integrations.d42.typed_dict import TypedDictSchema, typed_dict

__all__ = [
    "EACH",
    "JsonIntSchema",
    "RecursionContract",
    "RecursiveRefSchema",
    "TypedDictSchema",
    "build_fixture",
    "json_int",
    "overlay_generators",
    "render_expression",
    "render_module",
    "to_d42",
    "typed_dict",
]

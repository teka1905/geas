"""IR → детерминированный исходник Python со схемами d42.

Модуль печатает то же самое, что :func:`~openapi_contracts.integrations.d42.converter.to_d42`
собирает в памяти, но в виде текста ``.py``, который можно закоммитить, прочитать
глазами и подсветить в ревью diff'ом.

Гарантия эквивалентности
------------------------

Рендер и сборка объектов идут от **одного плана** (внутреннее дерево вызовов d42
в :mod:`~openapi_contracts.integrations.d42.converter`). Ветвлений «только для
текста» здесь нет, поэтому ``exec`` сгенерированного модуля даёт схемы, равные
(``==``) результату ``to_d42`` для тех же узлов IR.

Детерминизм
-----------

* определения печатаются в порядке зависимостей; независимые — по алфавиту,
  поэтому перестановка ключей в спецификации не двигает строки в артефакте;
* ключи объектов сортируются по имени, ``...: ...`` всегда идёт последним;
* строковые литералы печатаются в двойных кавычках, где это возможно без
  дополнительного экранирования;
* ``__all__`` отсортирован;
* в выводе нет ни временных меток, ни абсолютных путей, ни версий — ничего, что
  меняется само по себе; перегенерация без изменения спецификации даёт байт-в-байт
  тот же файл;
* перенос строк — ``LF``, кодировка ``UTF-8``, ровно один завершающий перевод строки,
  пробелов в конце строк нет.

Форматирование — идиоматичное для d42 (такое же, как у ``d42.represent``):
``schema.dict({...})`` разворачивается «скобка на первой строке, элементы с отступом
в 4 пробела, висячая запятая». Это не строго ``black``, зато читается так же, как
схемы, написанные руками, и полностью стабильно.

Выражение раскладывается по строкам, только если целиком не влезает в
:data:`LINE_LENGTH`. Неделимый атом (очень длинное имя свойства плюс скалярный
вызов) строку превысит — ровно как у ``black``, который тоже не рвёт атомы;
переносить такое ради колонки означало бы жертвовать читаемостью.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from openapi_contracts.errors import ArtifactError, NamespaceCollisionError, RefResolutionError
from openapi_contracts.integrations.d42.converter import (
    _DictPlan,
    _Leaf,
    _ListPlan,
    _NoLiteral,
    _Plan,
    _Ref,
    _UnionPlan,
    plan_node,
    referenced_names,
    topological_order,
)
from openapi_contracts.models import SchemaNode
from openapi_contracts.naming import d42_schema_name, python_identifier

__all__ = ["render_expression", "render_module"]

#: Максимальная длина строки в сгенерированном модуле.
LINE_LENGTH = 100

#: Ширина одного уровня отступа.
_INDENT = 4

#: Приписка к докстрингу: артефакт генерируемый, править руками нельзя.
_GENERATED_NOTICE = (
    "Файл сгенерирован автоматически. Не редактируйте его руками: правки будут\n"
    "потеряны при следующей генерации. Источник истины — OpenAPI-спецификация."
)

_HEADER = "from __future__ import annotations\n\nfrom d42 import optional, schema\n"

#: Определения и экспорты принимаются и как mapping, и как последовательность пар.
Definitions = Mapping[str, SchemaNode] | Sequence[tuple[str, SchemaNode]]


def render_module(
    *,
    module_docstring: str,
    definitions: Definitions,
    exports: Definitions,
) -> str:
    """Собрать исходник модуля со схемами d42.

    ``definitions`` — именованные определения бандла: имя определения → узел IR;
    имя переменной выводится через :func:`~openapi_contracts.naming.d42_schema_name`.
    ``exports`` — корневые схемы вариантов запроса и ответа: имя переменной (его
    задаёт вызывающий) → узел IR.

    Определения печатаются в порядке зависимостей, экспорты — после них по алфавиту.
    """
    definition_nodes = dict(definitions)
    export_nodes = dict(exports)

    plans = {
        name: plan_node(node, definition_nodes, definition=name)
        for name, node in definition_nodes.items()
    }
    export_plans = {name: plan_node(node, definition_nodes) for name, node in export_nodes.items()}

    refs = _definition_variables(definition_nodes)
    for name in sorted(export_plans):
        python_identifier(name, context=f"экспорт d42-схемы {name!r}")
        if name in refs.values():
            raise NamespaceCollisionError(
                f"экспорт {name!r} совпадает с именем generated-определения. "
                f"Переименуйте экспорт: числовой суффикс не добавляется автоматически"
            )

    for plan in export_plans.values():
        _require_known_refs(plan, refs)

    blocks: list[str] = [_docstring(module_docstring), _HEADER]
    for name in topological_order(plans):
        blocks.append(_assignment(refs[name], plans[name], refs))
    for name in sorted(export_plans):
        blocks.append(_assignment(name, export_plans[name], refs))

    names = sorted([*refs.values(), *export_plans])
    exported = "\n".join(f'{" " * _INDENT}"{name}",' for name in names)
    blocks.append(f"__all__ = [\n{exported}\n]" if names else "__all__: list[str] = []")

    text = "\n\n".join(blocks)
    return "\n".join(line.rstrip() for line in text.split("\n")).rstrip("\n") + "\n"


def render_expression(
    node: SchemaNode,
    definitions: Definitions,
    *,
    refs: Mapping[str, str],
) -> str:
    """Отрендерить выражение d42 для одного узла IR.

    ``refs`` сопоставляет имя определения уже напечатанной Python-переменной;
    ссылка на определение, которого там нет, — ошибка, а не подстановка «чего-нибудь».
    Первая строка результата рассчитана на начало строки; продолжения выравниваются
    по нулевому отступу.
    """
    plan = plan_node(node, dict(definitions))
    _require_known_refs(plan, refs)
    return _render(plan, refs, indent=0, used=0)


def _assignment(variable: str, plan: _Plan, refs: Mapping[str, str]) -> str:
    """Строка ``NAME = <выражение>`` с учётом занятой ширины под имя переменной."""
    expression = _render(plan, refs, indent=0, used=len(variable) + 3)
    return f"{variable} = {expression}"


def _definition_variables(definitions: Mapping[str, SchemaNode]) -> dict[str, str]:
    """Имя определения → имя Python-переменной, с проверкой коллизий."""
    variables: dict[str, str] = {}
    taken: dict[str, str] = {}
    for name in sorted(definitions):
        variable = python_identifier(
            d42_schema_name(name), context=f"generated d42-схема для {name!r}"
        )
        if variable in taken:
            raise NamespaceCollisionError(
                f"определения {taken[variable]!r} и {name!r} дают одно имя переменной "
                f"{variable!r}. Переименуйте одно из них в спецификации"
            )
        taken[variable] = name
        variables[name] = variable
    return variables


def _require_known_refs(plan: _Plan, refs: Mapping[str, str]) -> None:
    for name in sorted(referenced_names(plan)):
        if name not in refs:
            raise RefResolutionError(
                f"определение {name!r} не отрендерено: в refs нет переменной для него"
            )


def _docstring(text: str) -> str:
    """Докстринг модуля вместе с обязательной пометкой про генерацию."""
    body = text.strip()
    if '"""' in body:
        raise ArtifactError('докстринг модуля не может содержать """')
    if body.endswith("\\"):
        raise ArtifactError("докстринг модуля не может заканчиваться обратным слэшем")
    parts = [body, _GENERATED_NOTICE] if body else [_GENERATED_NOTICE]
    return '"""' + "\n\n".join(parts) + '\n"""'


# --------------------------------------------------------------------------------------
# Рендер выражений
# --------------------------------------------------------------------------------------


def _render(plan: _Plan, refs: Mapping[str, str], *, indent: int, used: int) -> str:
    """Выражение, которое влезает в ширину строки: в одну строку либо разложенное."""
    single = _single(plan, refs)
    if used + len(single) <= LINE_LENGTH:
        return single

    pad = " " * (indent + _INDENT)
    close = " " * indent
    if isinstance(plan, _DictPlan):
        lines = []
        for name, sub, is_optional in plan.entries:
            key = f"optional({_literal(name)})" if is_optional else _literal(name)
            # +2 — ": ", +1 — висячая запятая в конце строки.
            used_here = indent + _INDENT + len(key) + 3
            value = _render(sub, refs, indent=indent + _INDENT, used=used_here)
            lines.append(f"{pad}{key}: {value},")
        if plan.open:
            lines.append(f"{pad}...: ...,")
        body = "\n".join(lines)
        return f"schema.dict({{\n{body}\n{close}}})"
    if isinstance(plan, _UnionPlan):
        rendered = [
            _render(variant, refs, indent=indent + _INDENT, used=indent + _INDENT + 1)
            for variant in plan.variants
        ]
        body = "\n".join(f"{pad}{item}," for item in rendered)
        return f"schema.any(\n{body}\n{close})"
    if isinstance(plan, _ListPlan):
        items = _render(plan.items, refs, indent=indent + _INDENT, used=indent + _INDENT)
        return f"schema.list(\n{pad}{items}\n{close}){_calls(plan.calls)}"
    # Скалярные листы и ссылки не разбиваются: переносить нечего.
    return single


def _single(plan: _Plan, refs: Mapping[str, str]) -> str:
    """Однострочная форма выражения — она же критерий «влезает ли»."""
    if isinstance(plan, _Ref):
        return refs[plan.name]
    if isinstance(plan, _Leaf):
        base = f"schema.{plan.base}"
        if not isinstance(plan.literal, _NoLiteral):
            base = f"{base}({_literal(plan.literal)})"
        return base + _calls(plan.calls)
    if isinstance(plan, _DictPlan):
        items = []
        for name, sub, is_optional in plan.entries:
            key = f"optional({_literal(name)})" if is_optional else _literal(name)
            items.append(f"{key}: {_single(sub, refs)}")
        if plan.open:
            items.append("...: ...")
        return "schema.dict({" + ", ".join(items) + "})"
    if isinstance(plan, _ListPlan):
        return f"schema.list({_single(plan.items, refs)}){_calls(plan.calls)}"
    variants = ", ".join(_single(variant, refs) for variant in plan.variants)
    return f"schema.any({variants})"


def _calls(calls: tuple[tuple[str, tuple[Any, ...]], ...]) -> str:
    """Цепочка ``.len(1, 5).unique()``."""
    return "".join(f".{name}({', '.join(_literal(arg) for arg in args)})" for name, args in calls)


def _literal(value: Any) -> str:
    """Python-литерал аргумента: детерминированный и без сюрпризов с кавычками."""
    if value is Ellipsis:
        return "..."
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        quoted = repr(value)
        # repr выбирает одинарные кавычки, пока в строке нет апострофа; если при этом
        # нет и двойной кавычки, замена кавычек безопасна и не меняет экранирование.
        if quoted.startswith("'") and '"' not in value:
            return '"' + quoted[1:-1] + '"'
        return quoted
    raise ArtifactError(f"значение {value!r} ({type(value).__name__}) не рендерится в литерал")

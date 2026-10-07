"""IR → детерминированный исходник Python со схемами d42.

Модуль печатает то же самое, что :func:`~geas.integrations.d42.converter.to_d42`
собирает в памяти, но в виде текста ``.py``, который можно закоммитить, прочитать
глазами и подсветить в ревью diff'ом.

Гарантия эквивалентности
------------------------

Рендер и сборка объектов идут от **одного плана** (внутреннее дерево вызовов d42
в :mod:`~geas.integrations.d42.converter`). Ветвлений «только для
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

Рекурсия
--------

Если в модуле есть отсечённые циклы (см. раздел «Рекурсия» у
:mod:`~geas.integrations.d42.converter`), после импортов печатается
``_RECURSION = RecursionContract({...})`` с JSON Schema отсечённых определений,
отсечённый узел рендерится как ``_RECURSION.ref("Имя")``, а после определений
идёт ``_RECURSION.bind({...})``. JSON Schema встраивается в модуль, а не читается
из файла контракта: модуль самодостаточен, и ``exec`` по-прежнему даёт схемы,
равные результату ``to_d42``.

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
from dataclasses import dataclass
from typing import Any

from geas.errors import ArtifactError, NamespaceCollisionError, RefResolutionError
from geas.integrations.d42.converter import (
    _JSON_INT,
    DroppedBranchNote,
    _Cut,
    _DictPlan,
    _Leaf,
    _ListPlan,
    _NoLiteral,
    _Plan,
    _Ref,
    _UnionPlan,
    cut_cycles,
    cut_targets,
    plan_node,
    referenced_names,
    topological_order,
)
from geas.jsonschema_gen import definitions_json_schema
from geas.models import SchemaNode
from geas.naming import d42_schema_name, python_identifier

__all__ = ["ModuleNotes", "cycle_cuts", "module_notes", "render_expression", "render_module"]

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
_TYPED_DICT_IMPORT = "from geas.integrations.d42.typed_dict import typed_dict\n"
_RECURSION_IMPORT = "from geas.integrations.d42.recursion import RecursionContract\n"
_JSON_INT_IMPORT = "from geas.integrations.d42.json_int import json_int\n"

#: Имя переменной модуля с контрактом рекурсии. Не пересекается с generated-именами:
#: у определений и экспортов обязательный суффикс ``Schema``.
_RECURSION = "_RECURSION"

#: Определения и экспорты принимаются и как mapping, и как последовательность пар.
Definitions = Mapping[str, SchemaNode] | Sequence[tuple[str, SchemaNode]]


def render_module(
    *,
    module_docstring: str,
    definitions: Definitions,
    exports: Definitions,
    roots: Sequence[SchemaNode] | None = None,
) -> str:
    """Собрать исходник модуля со схемами d42.

    ``definitions`` — именованные определения бандла: имя определения → узел IR;
    имя переменной выводится через :func:`~geas.naming.d42_schema_name`.
    ``exports`` — корневые схемы вариантов запроса и ответа: имя переменной (его
    задаёт вызывающий) → узел IR.
    ``roots`` — корни, от которых отсекаются циклы (тела вариантов в порядке
    контракта). По умолчанию — экспорты по алфавиту.

    Определения печатаются в порядке зависимостей, экспорты — после них по алфавиту.
    """
    definition_nodes = dict(definitions)
    export_nodes = dict(exports)

    raw_plans = {
        name: plan_node(node, definition_nodes, definition=name)
        for name, node in definition_nodes.items()
    }
    export_plans = {name: plan_node(node, definition_nodes) for name, node in export_nodes.items()}
    plans, _ = cut_cycles(_root_plans(roots, export_plans, definition_nodes), raw_plans)
    cut = sorted(cut_targets(plans.values()))

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

    all_plans = [*plans.values(), *export_plans.values()]
    header = _HEADER
    if any(_uses_json_int(plan) for plan in all_plans):
        header += _JSON_INT_IMPORT
    if cut:
        header += _RECURSION_IMPORT
    if any(_uses_typed_dict(plan) for plan in all_plans):
        header += _TYPED_DICT_IMPORT
    blocks: list[str] = [_docstring(module_docstring), header]
    if cut:
        json_definitions = definitions_json_schema(cut, definition_nodes)
        prefix = f"{_RECURSION} = RecursionContract("
        blocks.append(f"{prefix}{_render_json(json_definitions, indent=0, used=len(prefix))})")
    for name in topological_order(plans):
        blocks.append(_assignment(refs[name], plans[name], refs))
    if cut:
        bindings = "\n".join(f"{' ' * _INDENT}{_literal(name)}: {refs[name]}," for name in cut)
        blocks.append(f"{_RECURSION}.bind({{\n{bindings}\n}})")
    for name in sorted(export_plans):
        blocks.append(_assignment(name, export_plans[name], refs))

    names = sorted([*refs.values(), *export_plans])
    exported = "\n".join(f'{" " * _INDENT}"{name}",' for name in names)
    blocks.append(f"__all__ = [\n{exported}\n]" if names else "__all__: list[str] = []")

    text = "\n\n".join(blocks)
    return "\n".join(line.rstrip() for line in text.split("\n")).rstrip("\n") + "\n"


@dataclass(frozen=True, slots=True)
class ModuleNotes:
    """Что модуль сделал с контрактом ради d42 — для отчёта ``geas update``."""

    #: Отсечённые рёбра ``(определение, на которое ссылается отсечённый узел)``.
    cuts: tuple[tuple[str, str], ...]
    #: Ветки ``allOf``, выброшенные как пустые, в стабильном порядке.
    dropped: tuple[DroppedBranchNote, ...]


def module_notes(*, definitions: Definitions, roots: Sequence[SchemaNode]) -> ModuleNotes:
    """Где модуль с такими определениями и корнями отсечёт циклы и какие ветки выбросит.

    Результат — то же, что сделает :func:`render_module` с теми же аргументами.
    """
    definition_nodes = dict(definitions)
    dropped: list[DroppedBranchNote] = []
    plans = {
        name: plan_node(node, definition_nodes, definition=name, dropped=dropped)
        for name, node in definition_nodes.items()
    }
    root_plans = [plan_node(node, definition_nodes, dropped=dropped) for node in roots]
    _, back = cut_cycles(root_plans, plans)
    unique = sorted(set(dropped), key=lambda note: (note.where, note.branch, note.reason))
    return ModuleNotes(cuts=back, dropped=tuple(unique))


def cycle_cuts(
    *, definitions: Definitions, roots: Sequence[SchemaNode]
) -> tuple[tuple[str, str], ...]:
    """Где модуль с такими определениями и корнями отсечёт циклы.

    Возвращает рёбра ``(определение, на которое ссылается отсечённый узел)`` — то же,
    что сделает :func:`render_module` с теми же аргументами.
    """
    return module_notes(definitions=definitions, roots=roots).cuts


def _root_plans(
    roots: Sequence[SchemaNode] | None,
    export_plans: Mapping[str, _Plan],
    definitions: Mapping[str, SchemaNode],
) -> list[_Plan]:
    """Планы корней, от которых отсекаются циклы."""
    if roots is None:
        return [export_plans[name] for name in sorted(export_plans)]
    return [plan_node(node, definitions) for node in roots]


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
        if plan.additional is not None:
            base = _DictPlan(entries=plan.entries, open=False)
            rendered_base = _render(
                base,
                refs,
                indent=indent + _INDENT,
                used=indent + _INDENT,
            )
            rendered_additional = _render(
                plan.additional,
                refs,
                indent=indent + _INDENT,
                used=indent + _INDENT + len("additional=") + 1,
            )
            return (
                f"typed_dict(\n{pad}{rendered_base},\n"
                f"{pad}additional={rendered_additional},\n{close})"
            )
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
    if isinstance(plan, _Cut):
        return f"{_RECURSION}.ref({_literal(plan.name)})"
    if isinstance(plan, _Leaf):
        base = plan.base if plan.base == _JSON_INT else f"schema.{plan.base}"
        if not isinstance(plan.literal, _NoLiteral):
            base = f"{base}({_literal(plan.literal)})"
        return base + _calls(plan.calls)
    if isinstance(plan, _DictPlan):
        items = []
        for name, sub, is_optional in plan.entries:
            key = f"optional({_literal(name)})" if is_optional else _literal(name)
            items.append(f"{key}: {_single(sub, refs)}")
        if plan.additional is not None:
            base = "schema.dict({" + ", ".join(items) + "})"
            return f"typed_dict({base}, additional={_single(plan.additional, refs)})"
        if plan.open:
            items.append("...: ...")
        return "schema.dict({" + ", ".join(items) + "})"
    if isinstance(plan, _ListPlan):
        return f"schema.list({_single(plan.items, refs)}){_calls(plan.calls)}"
    variants = ", ".join(_single(variant, refs) for variant in plan.variants)
    return f"schema.any({variants})"


def _render_json(value: Any, *, indent: int, used: int) -> str:
    """Python-литерал JSON-значения: в одну строку, если влезает, иначе разложенный.

    Ключи словарей сортируются — порядок в артефакте не зависит от порядка в
    спецификации.
    """
    single = _json_single(value)
    if used + len(single) <= LINE_LENGTH or not isinstance(value, (dict, list)) or not value:
        return single
    pad = " " * (indent + _INDENT)
    close = " " * indent
    if isinstance(value, dict):
        lines = []
        for key in sorted(value):
            rendered_key = _literal(key)
            item = _render_json(
                value[key],
                indent=indent + _INDENT,
                used=indent + _INDENT + len(rendered_key) + 3,
            )
            lines.append(f"{pad}{rendered_key}: {item},")
        return "{\n" + "\n".join(lines) + f"\n{close}}}"
    items = [
        f"{pad}{_render_json(item, indent=indent + _INDENT, used=indent + _INDENT + 1)},"
        for item in value
    ]
    return "[\n" + "\n".join(items) + f"\n{close}]"


def _json_single(value: Any) -> str:
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(f"{_literal(key)}: {_json_single(value[key])}" for key in sorted(value))
            + "}"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_json_single(item) for item in value) + "]"
    return _literal(value)


def _uses_typed_dict(plan: _Plan) -> bool:
    """Есть ли типизированный словарь в плане или его дочерних узлах."""
    if isinstance(plan, _DictPlan):
        return plan.additional is not None or any(
            _uses_typed_dict(child) for _, child, _ in plan.entries
        )
    if isinstance(plan, _ListPlan):
        return _uses_typed_dict(plan.items)
    if isinstance(plan, _UnionPlan):
        return any(_uses_typed_dict(variant) for variant in plan.variants)
    return False


def _uses_json_int(plan: _Plan) -> bool:
    """Есть ли целочисленная ветка ``number`` в плане или его дочерних узлах."""
    if isinstance(plan, _Leaf):
        return plan.base == _JSON_INT
    if isinstance(plan, _DictPlan):
        return any(_uses_json_int(child) for _, child, _ in plan.entries) or (
            plan.additional is not None and _uses_json_int(plan.additional)
        )
    if isinstance(plan, _ListPlan):
        return _uses_json_int(plan.items)
    if isinstance(plan, _UnionPlan):
        return any(_uses_json_int(variant) for variant in plan.variants)
    return False


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

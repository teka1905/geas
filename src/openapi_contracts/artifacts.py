"""Generated-артефакты: рендер, детерминированная запись и проверка на drift.

Набор артефактов на один проект:

===============================  =================================================
Файл                             Что это
===============================  =================================================
``__init__.py``                  реэкспорт ``operations`` и реестра
``operations.py``                статический typed namespace операций
``_registry.py``                 индекс ключ → файл контракта и сам реестр
``contracts/<slug>.json``        нормализованный контракт (JSON Schema внутри)
``_d42/<slug>_<direction>.py``   generated d42-схемы
``_generated.json``              версия формата, отпечатки, список owned-файлов
===============================  =================================================

Детерминированность: стабильная сортировка, UTF-8, ``\\n``, отсутствие timestamp,
абсолютных путей и случайных значений. Повторный ``update`` без изменения входов
даёт байт-в-байт тот же результат.

Безопасность записи: сначала полностью рендерится весь набор (все ошибки
случаются здесь), потом файлы пишутся через временный файл рядом и
``os.replace``. Удаляются **только** файлы, записанные генератором как owned, и
только после проверки на выход за каталог и на symlink.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import BuildResult
from .errors import ArtifactDriftError, ArtifactError
from .fingerprints import digest
from .manifest import Manifest
from .naming import to_pascal_case
from .semantic_diff import semantic_fingerprint

__all__ = [
    "ARTIFACT_FORMAT_VERSION",
    "GENERATED_INDEX",
    "Artifact",
    "ArtifactSet",
    "DriftReport",
    "check_artifacts",
    "read_generated_index",
    "render_artifacts",
    "write_artifacts",
]

#: Версия формата generated-артефактов. Несовпадение — ошибка, а не «наверное сойдёт».
ARTIFACT_FORMAT_VERSION = 1

#: Имя файла с описанием generated-набора.
GENERATED_INDEX = "_generated.json"

_HEADER = (
    "# Файл сгенерирован автоматически командой 'openapi-contracts update'.\n"
    "# Не редактируйте его руками: изменения будут затёрты, а 'openapi-contracts check'\n"
    "# уронит CI на расхождении.\n"
)


@dataclass(frozen=True, kw_only=True, slots=True)
class Artifact:
    """Один generated-файл."""

    #: Путь относительно каталога вывода, в POSIX-форме.
    path: str
    content: str

    @property
    def digest(self) -> str:
        """SHA-256 содержимого."""
        import hashlib

        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True, slots=True)
class ArtifactSet:
    """Полный набор generated-файлов."""

    files: tuple[Artifact, ...]
    #: Операции, для которых d42 отключён, и причина — попадает в вывод ``update``.
    d42_disabled: tuple[tuple[str, str], ...] = ()

    def paths(self) -> tuple[str, ...]:
        """Owned-пути в стабильном порядке."""
        return tuple(sorted(item.path for item in self.files))

    def by_path(self) -> dict[str, Artifact]:
        return {item.path: item for item in self.files}


@dataclass(frozen=True, kw_only=True, slots=True)
class DriftReport:
    """Расхождение рабочего дерева с тем, что даёт генератор."""

    missing: tuple[str, ...]
    changed: tuple[str, ...]
    extra: tuple[str, ...]
    format_mismatch: str | None = None

    @property
    def is_clean(self) -> bool:
        """Нет ли расхождений."""
        return not (self.missing or self.changed or self.extra or self.format_mismatch)

    def describe(self) -> str:
        """Человекочитаемый отчёт."""
        lines: list[str] = []
        if self.format_mismatch:
            lines.append(f"версия формата артефактов: {self.format_mismatch}")
        for path in self.missing:
            lines.append(f"  отсутствует: {path}")
        for path in self.changed:
            lines.append(f"  отличается:  {path}")
        for path in self.extra:
            lines.append(f"  лишний:      {path}")
        return "\n".join(lines)


# ------------------------------------------------------------------ рендер


def render_artifacts(manifest: Manifest, result: BuildResult) -> ArtifactSet:
    """Отрендерить весь набор артефактов детерминированно."""
    files: list[Artifact] = []
    index: dict[str, str] = {}
    fingerprints: dict[str, str] = {}

    # d42 рендерится первым: его отказ не должен ронять операцию целиком, но обязан
    # быть записан в документ контракта до того, как документ попадёт в артефакт.
    d42_files, d42_disabled = _render_d42_modules(result)

    for built in result.operations:
        document = _document_with_d42_status(built, d42_disabled.get(built.contract.key))
        slug = document["slug"]
        index[built.contract.key] = slug
        fingerprints[built.contract.key] = semantic_fingerprint(document)
        files.append(Artifact(path=f"contracts/{slug}.json", content=_json_text(document)))

    files.extend(d42_files)
    files.append(Artifact(path="__init__.py", content=_render_init()))
    files.append(Artifact(path="_registry.py", content=_render_registry(index)))
    files.append(
        Artifact(
            path="operations.py",
            content=_render_operations(
                [(built.contract.key, built.contract.python_path) for built in result.operations]
            ),
        )
    )

    owned = sorted({item.path for item in files} | {GENERATED_INDEX})
    generated = {
        "artifact_format": ARTIFACT_FORMAT_VERSION,
        "generator": _generator_version(),
        "output": {
            "directory": manifest.output_directory,
            "package": manifest.output_package,
        },
        "operations": {
            key: {"slug": index[key], "fingerprint": fingerprints[key]} for key in sorted(index)
        },
        "fingerprint": digest(fingerprints),
        "owned": owned,
        "digests": {item.path: item.digest for item in sorted(files, key=lambda i: i.path)},
    }
    files.append(Artifact(path=GENERATED_INDEX, content=_json_text(generated)))
    return ArtifactSet(
        files=tuple(sorted(files, key=lambda item: item.path)),
        d42_disabled=tuple(sorted(d42_disabled.items())),
    )


def _document_with_d42_status(built: Any, reason: str | None) -> dict[str, Any]:
    """Документ контракта с учётом того, удалось ли отрендерить d42.

    Конструкция может быть точно выразима контрактом и JSON Schema, но не
    выразима в d42 (пересечение ``allOf``, ``multipleOf``, рекурсия). Ронять
    из-за этого всю операцию неправильно: ядро не
    обязано зависеть от опциональной интеграции. Поэтому d42 отключается
    **точечно**, причина записывается в артефакт и всплывает в ``update``, а
    обращение к отсутствующей d42-схеме падает с этой же причиной.
    """
    document: dict[str, Any] = built.document
    if reason is None:
        return document
    patched = dict(document)
    patched["d42"] = {
        **document.get("d42", {}),
        "enabled": False,
        "request_module": None,
        "response_module": None,
        "reason": reason,
    }
    patched["unsupported"] = [*document.get("unsupported", []), f"d42: {reason}"]
    patched["request"] = _strip_d42_exports(document["request"], key="bodies")
    patched["responses"] = [{**item, "d42": None} for item in document["responses"]]
    return patched


def _strip_d42_exports(request: dict[str, Any], *, key: str) -> dict[str, Any]:
    return {**request, key: [{**item, "d42": None} for item in request[key]]}


def _generator_version() -> str:
    from . import __version__

    return __version__


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _render_d42_modules(result: BuildResult) -> tuple[list[Artifact], dict[str, str]]:
    """Отрендерить d42-модули, отключая d42 точечно там, где он невыразим.

    Рендер опирается на тот же «план» схемы, что и построение живого объекта d42,
    поэтому исходник и объект не могут разъехаться. Платой за это является импорт
    ``d42``: если extra не установлен, а операции его требуют, поднимается
    :class:`MissingExtraError` с точной командой установки.

    Если же d42 установлен, но конструкцию выразить не умеет, операция **не**
    падает: возвращается причина, которая попадёт в документ контракта.
    """
    needed = [
        built for built in result.operations if (built.document.get("d42") or {}).get("enabled")
    ]
    if not needed:
        return [], {}
    try:
        from .integrations.d42.renderer import render_module
    except ImportError as exc:
        from .errors import MissingExtraError

        keys = ", ".join(built.contract.key for built in needed)
        raise MissingExtraError("d42", f"генерация d42-схем для операций {keys}") from exc

    from .errors import UnsupportedConstructError

    files: list[Artifact] = []
    disabled: dict[str, str] = {}
    for built in needed:
        section = built.document["d42"]
        rendered: list[Artifact] = []
        try:
            for direction, definitions in (
                ("request", built.request_definitions),
                ("response", built.response_definitions),
            ):
                module = section.get(f"{direction}_module")
                if not module:
                    continue
                exports = _d42_exports(built, direction)
                names = {_d42_variable(name) for name, _ in definitions}
                rendered.append(
                    Artifact(
                        path=f"_d42/{module}.py",
                        content=render_module(
                            module_docstring=(
                                f"Generated d42-схемы операции ``{built.contract.key}`` "
                                f"({direction})."
                            ),
                            definitions=dict(definitions),
                            exports={
                                name: node for name, node in exports.items() if name not in names
                            },
                        ),
                    )
                )
        except UnsupportedConstructError as error:
            disabled[built.contract.key] = error.base_message
            continue
        files.extend(rendered)

    if files:
        files.append(
            Artifact(
                path="_d42/__init__.py",
                content=(
                    '"""Generated d42-схемы. Модуль импортируется только при установленном '
                    'extra [d42]."""\n'
                ),
            )
        )
    return files, disabled


def _d42_variable(definition_name: str) -> str:
    from .naming import d42_schema_name

    return d42_schema_name(definition_name)


def _d42_exports(built: Any, direction: str) -> dict[str, Any]:
    """Корневые схемы вариантов направления: имя переменной → узел IR.

    Если корень варианта — это ``$ref`` на именованное определение, отдельная
    переменная не нужна: определение уже отрендерено под тем же именем.
    """
    from .models import RefNode

    exports: dict[str, Any] = {}
    if direction == "request":
        for item, body in zip(built.document["request"]["bodies"], built.contract.request.bodies):
            name = item.get("d42")
            if name and not isinstance(body.schema, RefNode):
                exports[name] = body.schema
        return exports
    for item, response in zip(built.document["responses"], built.contract.responses):
        name = item.get("d42")
        if name and response.body is not None and not isinstance(response.body, RefNode):
            exports[name] = response.body
    return exports


def _render_init() -> str:
    return (
        '"""Generated-пакет контрактов.\n\n'
        "Точка входа — ``operations``: статический namespace операций с типами и\n"
        "автодополнением. Строковый доступ ``operations.by_key(...)`` оставлен для\n"
        "инструментов.\n"
        '"""\n\n'
        f"{_HEADER}"
        "from __future__ import annotations\n\n"
        "from ._registry import REGISTRY\n"
        "from .operations import Operations, operations\n\n"
        '__all__ = ["Operations", "REGISTRY", "operations"]\n'
    )


def _render_registry(index: Mapping[str, str]) -> str:
    entries = "".join(f"    {key!r}: {index[key]!r},\n" for key in sorted(index))
    return (
        '"""Индекс операций и сам реестр.\n\n'
        "Документы контрактов читаются лениво, поэтому импорт этого модуля дёшев и не\n"
        "требует ни d42, ни JJ.\n"
        '"""\n\n'
        f"{_HEADER}"
        "from __future__ import annotations\n\n"
        "from pathlib import Path\n\n"
        "from openapi_contracts.runtime import OperationRegistry\n\n"
        "#: Стабильный ключ операции → имя файла контракта.\n"
        "INDEX: dict[str, str] = {\n"
        f"{entries}"
        "}\n\n"
        "#: Каталог с нормализованными контрактами.\n"
        'CONTRACTS_DIR = Path(__file__).parent / "contracts"\n\n'
        "REGISTRY = OperationRegistry(\n"
        "    index=INDEX,\n"
        "    contracts_dir=CONTRACTS_DIR,\n"
        '    d42_package=f"{__package__}._d42",\n'
        ")\n\n"
        '__all__ = ["CONTRACTS_DIR", "INDEX", "REGISTRY"]\n'
    )


def _render_operations(entries: list[tuple[str, tuple[str, ...]]]) -> str:
    """Собрать статический typed namespace.

    Атрибуты объявлены как ``property``: тип виден IDE и mypy, а документ контракта
    читается только при первом обращении.
    """
    tree: dict[tuple[str, ...], dict[str, Any]] = {}

    def node(prefix: tuple[str, ...]) -> dict[str, Any]:
        return tree.setdefault(prefix, {"children": {}, "operations": {}})

    node(())
    for key, path in sorted(entries, key=lambda item: item[1]):
        for depth in range(1, len(path)):
            parent = node(path[: depth - 1])
            parent["children"][path[depth - 1]] = path[:depth]
            node(path[:depth])
        node(path[:-1])["operations"][path[-1]] = key

    _check_class_names(tree)

    lines: list[str] = [
        '"""Статический namespace операций.',
        "",
        "Имена строятся из стабильных ключей manifest, а не из ``operationId``:",
        "переименование ``operationId`` на стороне бэкенда ломает binding, но публичное",
        "Python-имя само не меняется.",
        '"""',
        "",
        _HEADER.rstrip("\n"),
        "from __future__ import annotations",
        "",
        "from openapi_contracts.runtime import OperationHandle, OperationRegistry",
        "",
        "from ._registry import REGISTRY",
        "",
        '__all__ = ["Operations", "operations"]',
        "",
    ]

    for prefix in sorted(tree, key=lambda item: (-len(item), item)):
        if not prefix:
            continue
        lines.extend(_render_namespace_class(prefix, tree[prefix]))

    root = tree[()]
    lines.append("")
    lines.append("class Operations:")
    lines.append('    """Корень generated namespace."""')
    lines.append("")
    lines.append("    def __init__(self, registry: OperationRegistry) -> None:")
    lines.append("        self._registry = registry")
    for name in sorted(root["children"]):
        lines.append(f"        self._{name} = {_class_name(root['children'][name])}(registry)")
    if not root["children"]:
        lines.append("        pass" if not root["operations"] else "")
    lines.append("")
    for name in sorted(root["children"]):
        lines.append("    @property")
        lines.append(f"    def {name}(self) -> {_class_name(root['children'][name])}:")
        lines.append(f'        """Пространство имён ``{name}``."""')
        lines.append(f"        return self._{name}")
        lines.append("")
    for name in sorted(root["operations"]):
        lines.extend(_render_operation_property(name, root["operations"][name]))
    lines.append("    def by_key(self, key: str) -> OperationHandle:")
    lines.append('        """Найти операцию по стабильному ключу manifest.')
    lines.append("")
    lines.append("        Строковый доступ — escape hatch для инструментов и CLI.")
    lines.append("        В тестах рекомендуется generated namespace.")
    lines.append('        """')
    lines.append("        return self._registry.by_key(key)")
    lines.append("")
    lines.append("    def keys(self) -> tuple[str, ...]:")
    lines.append('        """Все ключи операций."""')
    lines.append("        return self._registry.keys()")
    lines.append("")
    lines.append("")
    lines.append("operations = Operations(REGISTRY)")
    return "\n".join(lines).rstrip("\n") + "\n"


def _class_name(prefix: tuple[str, ...]) -> str:
    """Имя класса namespace для пути ``prefix``.

    Сегменты соединяются подчёркиванием, потому что ``to_pascal_case``
    подчёркивания вычищает: без разделителя пути ``a_b.c`` и ``a.b_c`` дали бы
    один класс ``_NsABC``, и второе определение молча затёрло бы первое — часть
    операций просто исчезла бы из generated namespace.
    """
    return "_Ns" + "_".join(to_pascal_case(part) for part in prefix)


def _check_class_names(tree: Mapping[tuple[str, ...], Any]) -> None:
    """Убедиться, что разные namespace не претендуют на одно имя класса.

    Разделитель в :func:`_class_name` снимает все реалистичные коллизии, но
    ``to_pascal_case`` не инъективен (``aB`` и ``a_b`` дают ``AB``). Остаточный
    случай — ошибка генерации, а не молча испорченный namespace.
    """
    from .errors import NamespaceCollisionError

    taken: dict[str, tuple[str, ...]] = {}
    for prefix in sorted(tree):
        if not prefix:
            continue
        name = _class_name(prefix)
        other = taken.get(name)
        if other is not None:
            raise NamespaceCollisionError(
                f"пространства имён {'.'.join(other)!r} и {'.'.join(prefix)!r} дают один "
                f"класс {name!r} в generated operations.py. Задайте python_path в manifest"
            )
        taken[name] = prefix


def _render_namespace_class(prefix: tuple[str, ...], node: dict[str, Any]) -> list[str]:
    lines = [
        "",
        f"class {_class_name(prefix)}:",
        f'    """Операции пространства имён ``{".".join(prefix)}``."""',
        "",
        "    def __init__(self, registry: OperationRegistry) -> None:",
        "        self._registry = registry",
    ]
    for name in sorted(node["children"]):
        lines.append(f"        self._{name} = {_class_name(node['children'][name])}(registry)")
    lines.append("")
    for name in sorted(node["children"]):
        lines.append("    @property")
        lines.append(f"    def {name}(self) -> {_class_name(node['children'][name])}:")
        lines.append(f'        """Пространство имён ``{".".join((*prefix, name))}``."""')
        lines.append(f"        return self._{name}")
        lines.append("")
    for name in sorted(node["operations"]):
        lines.extend(_render_operation_property(name, node["operations"][name]))
    return lines


def _render_operation_property(name: str, key: str) -> list[str]:
    return [
        "    @property",
        f"    def {name}(self) -> OperationHandle:",
        f'        """Операция ``{key}``."""',
        f"        return self._registry.by_key({key!r})",
        "",
    ]


# ------------------------------------------------------------------ запись


def _safe_target(output_dir: Path, relative: str) -> Path:
    """Проверить, что путь безопасно ведёт внутрь каталога вывода."""
    if relative.startswith("/") or ".." in Path(relative).parts:
        raise ArtifactError(f"недопустимый путь артефакта: {relative!r}")
    target = output_dir / relative
    resolved_parent = target.parent.resolve()
    if not resolved_parent.is_relative_to(output_dir.resolve()):
        raise ArtifactError(
            f"путь {relative!r} выводит за каталог артефактов {output_dir} — "
            f"похоже на symlink или на '..' в имени"
        )
    if target.is_symlink():
        raise ArtifactError(
            f"{target} — символическая ссылка. Генератор не пишет и не удаляет ссылки"
        )
    return target


def write_artifacts(output_dir: Path, artifacts: ArtifactSet) -> tuple[str, ...]:
    """Записать набор атомарно и убрать устаревшие owned-файлы.

    Возвращает список удалённых путей.
    """
    output_dir = Path(output_dir)
    previous = read_generated_index(output_dir)
    previous_owned = set(previous.get("owned") or ())

    output_dir.mkdir(parents=True, exist_ok=True)
    for artifact in artifacts.files:
        target = _safe_target(output_dir, artifact.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, artifact.content)

    stale = sorted(previous_owned - set(artifacts.paths()))
    removed: list[str] = []
    for relative in stale:
        target = _safe_target(output_dir, relative)
        if target.is_file():
            target.unlink()
            removed.append(relative)
    _remove_empty_dirs(output_dir)
    return tuple(removed)


def _atomic_write(target: Path, content: str) -> None:
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    tmp.replace(target)


def _remove_empty_dirs(output_dir: Path) -> None:
    for path in sorted(output_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()


def read_generated_index(output_dir: Path) -> dict[str, Any]:
    """Прочитать ``_generated.json``. Отсутствие файла — пустой набор."""
    path = Path(output_dir) / GENERATED_INDEX
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{path} повреждён: {exc}") from exc
    if not isinstance(data, dict):
        raise ArtifactError(f"{path}: ожидался объект")
    return data


# ------------------------------------------------------------------ проверка


def check_artifacts(output_dir: Path, artifacts: ArtifactSet) -> DriftReport:
    """Сравнить рабочее дерево с тем, что даёт генератор, ничего не меняя."""
    output_dir = Path(output_dir)
    previous = read_generated_index(output_dir)
    format_mismatch = None
    if previous:
        stored = previous.get("artifact_format")
        if stored != ARTIFACT_FORMAT_VERSION:
            format_mismatch = (
                f"на диске {stored!r}, библиотека генерирует {ARTIFACT_FORMAT_VERSION!r}"
            )

    missing: list[str] = []
    changed: list[str] = []
    for artifact in artifacts.files:
        target = output_dir / artifact.path
        if not target.is_file():
            missing.append(artifact.path)
            continue
        current = target.read_text(encoding="utf-8")
        if current != artifact.content:
            changed.append(artifact.path)

    expected = set(artifacts.paths())
    previous_owned = set(previous.get("owned") or ())
    extra = sorted(path for path in previous_owned - expected if (output_dir / path).is_file())

    return DriftReport(
        missing=tuple(sorted(missing)),
        changed=tuple(sorted(changed)),
        extra=tuple(extra),
        format_mismatch=format_mismatch,
    )


def raise_on_drift(report: DriftReport, *, command: str = "openapi-contracts update") -> None:
    """Превратить отчёт о расхождении в ошибку с подсказкой, что запустить."""
    if report.is_clean:
        return
    raise ArtifactDriftError(
        "generated-артефакты разошлись со спецификацией:\n"
        f"{report.describe()}\n"
        f"Запустите '{command}' и закоммитьте результат",
        report=report,
    )

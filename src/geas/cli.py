"""CLI ``geas``.

Команды:

===========  ==========================================================
``init``     создать минимальный manifest и waivers, ничего не затирая
``list``     показать источники, операции и их варианты (алиас ``inspect``)
``add``      транзакционно добавить операцию в explicit-allowlist
``update``   детерминированно перегенерировать артефакты
``check``    проверить рабочее дерево на contract drift, ничего не меняя
``diff``     показать семантический diff контрактов
===========  ==========================================================

Коды возврата стабильны и годятся для CI:

====  =============================================
0     успех, расхождений нет
1     ошибка контракта (drift, binding, waiver, ...)
2     ошибка использования CLI
====  =============================================
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .artifacts import (
    check_artifacts,
    read_generated_index,
    render_artifacts,
    write_artifacts,
)
from .contracts import build_contracts
from .dialects.base import detect_dialect
from .errors import ContractError, ManifestError
from .manifest import (
    MANIFEST_VERSION,
    Manifest,
    Selection,
    default_manifest_document,
    dump_manifest,
    load_manifest,
)
from .naming import python_path_from_key
from .normalization.refs import SpecRegistry
from .semantic_diff import diff_operations
from .waivers import empty_waivers_document, load_waivers

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_CONTRACT_ERROR = 1
EXIT_USAGE_ERROR = 2

DEFAULT_MANIFEST = "manifest.yaml"


class _UsageError(Exception):
    """Неверный аргумент CLI.

    Отдельный тип нужен, чтобы такие случаи давали код 2 (ошибка использования),
    а не 1: расхождение контракта и опечатка в команде — разные события для CI.
    """


def build_parser() -> argparse.ArgumentParser:
    """Собрать разбор аргументов."""
    parser = argparse.ArgumentParser(
        prog="geas",
        description="Генерация тестовых контрактов и operation-aware моков из OpenAPI",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-m",
        "--manifest",
        default=DEFAULT_MANIFEST,
        help=f"путь к manifest (по умолчанию {DEFAULT_MANIFEST})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="создать минимальный manifest и waivers")
    init.add_argument("--source", required=True, help="путь к файлу OpenAPI относительно manifest")
    init.add_argument("--directory", default="generated", help="каталог generated-артефактов")
    init.add_argument(
        "--package", required=True, help="Python-пакет, соответствующий каталогу артефактов"
    )
    init.add_argument("--force", action="store_true", help="перезаписать существующие файлы")

    for name in ("list", "inspect"):
        listing = sub.add_parser(name, help="показать источники и операции")
        listing.add_argument("--source", help="показать только один источник")
        listing.add_argument("--json", action="store_true", help="машиночитаемый вывод")

    add = sub.add_parser("add", help="добавить операцию в explicit-allowlist")
    add.add_argument("key", help="стабильный ключ операции, например 'ws2.addTicket'")
    add.add_argument("--source", required=True)
    add.add_argument("--operation-id")
    add.add_argument("--method")
    add.add_argument("--path")
    add.add_argument("--request-content-type")
    add.add_argument(
        "--response",
        action="append",
        default=[],
        metavar="STATUS[:CONTENT_TYPE]",
        help="закрепить вариант ответа; можно повторять",
    )
    add.add_argument(
        "--python-path",
        help="явный путь в generated namespace через точку, например 'ws2.add_ticket'",
    )
    add.add_argument("--no-d42", action="store_true", help="не генерировать d42 для операции")

    sub.add_parser("update", help="перегенерировать артефакты")
    sub.add_parser("check", help="проверить артефакты на drift")

    diff = sub.add_parser("diff", help="семантический diff контрактов")
    diff.add_argument("--json", action="store_true", help="машиночитаемый вывод")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return _cmd_init(args)
        if args.command in ("list", "inspect"):
            return _cmd_list(args)
        if args.command == "add":
            return _cmd_add(args)
        if args.command == "update":
            return _cmd_update(args)
        if args.command == "check":
            return _cmd_check(args)
        if args.command == "diff":
            return _cmd_diff(args)
    except _UsageError as error:
        print(f"ошибка использования: {error}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except ContractError as error:
        print(f"ошибка: {error}", file=sys.stderr)
        return EXIT_CONTRACT_ERROR
    parser.error(f"неизвестная команда {args.command!r}")  # argparse завершает процесс


# --------------------------------------------------------------------- init


def _cmd_init(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    waivers_path = manifest_path.parent / "waivers.yaml"
    existing = [path for path in (manifest_path, waivers_path) if path.exists()]
    if existing and not args.force:
        names = ", ".join(str(path) for path in existing)
        print(
            f"файлы уже существуют: {names}. Повторите с --force, если действительно "
            f"хотите их перезаписать",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR

    document = default_manifest_document(
        directory=args.directory, package=args.package, source_path=args.source
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    waivers_path.write_text(empty_waivers_document(), encoding="utf-8")

    print(f"создан {manifest_path}")
    print(f"создан {waivers_path}")
    print()
    print("Дальше:")
    print(f"  geas -m {manifest_path} list")
    print(f"  geas -m {manifest_path} add <ключ> --source main --operation-id <id>")
    print(f"  geas -m {manifest_path} update")
    print(f"  geas -m {manifest_path} check   # это и ставится в CI")
    return EXIT_OK


# --------------------------------------------------------------------- list


def _cmd_list(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    report = _inspect(manifest, only=args.source)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_OK
    for source in report["sources"]:
        print(f"источник {source['name']}: {source['path']} [{source['dialect']}]")
        print(f"  режим выбора: {source['selection']}, операций: {len(source['operations'])}")
        for operation in source["operations"]:
            mark = "*" if operation["selected"] else " "
            key = operation["key"] or "-"
            print(f"  {mark} {operation['method']:<7} {operation['path']}")
            print(f"      operationId={operation['operation_id'] or '-'}  ключ={key}")
            if operation["request_content_types"]:
                print(f"      request: {', '.join(operation['request_content_types'])}")
            if operation["responses"]:
                print(f"      responses: {', '.join(operation['responses'])}")
            for reason in operation["unsupported"]:
                print(f"      не поддержано: {reason}")
    print()
    print("* — операция выбрана manifest и попадает в generated-артефакты")
    return EXIT_OK


def _inspect(manifest: Manifest, *, only: str | None = None) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for source in sorted(manifest.sources, key=lambda item: item.name):
        if only is not None and source.name != only:
            continue
        registry = SpecRegistry(
            source_name=source.name,
            entry_path=manifest.source_path(source.name),
            root=manifest.source_root(source.name),
        )
        dialect = detect_dialect(registry.entry_document(), source=source.name)
        operations: list[dict[str, Any]] = []
        explicit_ids = {
            spec.operation_id: spec.key
            for spec in manifest.operations
            if spec.source == source.name and spec.operation_id
        }
        for raw in dialect.operations(registry, base_path=source.base_path):
            key: str | None = None
            if source.selection is Selection.ALL and raw.operation_id:
                key = f"{source.name}.{raw.operation_id}"
            elif raw.operation_id in explicit_ids:
                key = explicit_ids[raw.operation_id]
            unsupported = [
                f"request {body.content_type}: {body.unsupported_reason}"
                for body in raw.bodies
                if body.unsupported_reason
            ] + [
                f"response {item.status}:{item.content_type or '-'}: {item.unsupported_reason}"
                for item in raw.responses
                if item.unsupported_reason
            ]
            operations.append(
                {
                    "operation_id": raw.operation_id,
                    "method": raw.method,
                    "path": raw.path,
                    "key": key,
                    "selected": key is not None,
                    "request_content_types": sorted(
                        body.content_type for body in raw.bodies if not body.unsupported_reason
                    ),
                    "responses": sorted(
                        f"{item.status}:{item.content_type or '-'}"
                        for item in raw.responses
                        if not item.unsupported_reason
                    ),
                    "unsupported": unsupported,
                }
            )
        sources.append(
            {
                "name": source.name,
                "path": source.path,
                "dialect": dialect.name,
                "selection": source.selection.value,
                "operations": sorted(operations, key=lambda item: (item["path"], item["method"])),
            }
        )
    return {"manifest_version": MANIFEST_VERSION, "sources": sources}


# ---------------------------------------------------------------------- add


def _cmd_add(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    try:
        original_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"не удалось прочитать manifest {manifest_path}: {exc}") from exc
    try:
        document = yaml.safe_load(original_text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest {manifest_path} не разбирается: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestError(f"{manifest_path}: ожидался объект")

    entry: dict[str, Any] = {"source": args.source}
    if args.operation_id:
        entry["operation_id"] = args.operation_id
    if args.method:
        entry["method"] = args.method.upper()
    if args.path:
        entry["path"] = args.path
    if args.request_content_type:
        entry["request"] = {"content_type": args.request_content_type}
    if args.response:
        entry["responses"] = [_parse_response_selector(item) for item in args.response]
    if args.python_path:
        entry["python_path"] = args.python_path.split(".")
    if args.no_d42:
        entry["d42"] = False

    entry = _complete_add_entry(args, manifest_path=manifest_path, entry=entry)

    operations = dict(document.get("operations") or {})
    current_entry = operations.get(args.key)
    if isinstance(current_entry, dict) and _operation_binding_matches(
        current=current_entry, proposed=entry
    ):
        print(f"операция {args.key} уже добавлена с теми же параметрами — ничего не меняю")
        return EXIT_OK
    if current_entry is not None:
        print(
            f"операция {args.key} уже есть в manifest, но с другими параметрами.\n"
            f"  сейчас:    {json.dumps(operations[args.key], ensure_ascii=False, sort_keys=True)}\n"
            f"  предложено: {json.dumps(entry, ensure_ascii=False, sort_keys=True)}\n"
            f"Отредактируйте manifest вручную, если это осознанное изменение",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR

    operations[args.key] = entry
    candidate = dict(document)
    candidate["operations"] = operations

    # Транзакционность: операция полностью нормализуется и рендерится во временный
    # каталог до того, как manifest будет тронут. Если что-то не поддержано,
    # manifest останется байт-в-байт прежним.
    manifest = Manifest.from_dict(candidate, base_dir=manifest_path.parent)
    waivers = load_waivers(manifest.base_dir / manifest.waivers_path)
    waivers.validate(manifest, today=dt.date.today())
    result = build_contracts(manifest, waivers)
    artifacts = render_artifacts(manifest, result)
    with tempfile.TemporaryDirectory(prefix="geas-add-") as staging:
        write_artifacts(Path(staging), artifacts)

    manifest_path.write_text(dump_manifest(manifest), encoding="utf-8")
    print(f"операция {args.key} добавлена в {manifest_path}")
    print("Теперь запустите 'geas update', чтобы обновить артефакты")
    return EXIT_OK


def _operation_binding_matches(*, current: dict[str, Any], proposed: dict[str, Any]) -> bool:
    """Сравнить binding, не отбрасывая дополнительные политики текущей записи."""
    return all(current.get(key) == value for key, value in proposed.items())


def _complete_add_entry(
    args: argparse.Namespace, *, manifest_path: Path, entry: dict[str, Any]
) -> dict[str, Any]:
    """Дополнить запись стабильным binding, выведенным из выбранной операции."""
    manifest = load_manifest(manifest_path)
    report = _inspect(manifest, only=args.source)
    if not report["sources"]:
        raise _UsageError(f"в manifest нет источника {args.source!r}")

    operations = report["sources"][0]["operations"]
    candidates = [
        operation
        for operation in operations
        if (not args.operation_id or operation["operation_id"] == args.operation_id)
        and (not args.method or operation["method"] == args.method.upper())
        and (not args.path or operation["path"] == args.path)
    ]
    if not candidates:
        raise _UsageError(
            f"в источнике {args.source!r} нет операции с переданным operationId/method/path"
        )
    if len(candidates) > 1:
        routes = ", ".join(f"{item['method']} {item['path']}" for item in candidates)
        raise _UsageError(f"операция выбрана неоднозначно ({routes}); передайте --method и --path")

    operation = candidates[0]
    completed = dict(entry)
    if operation["operation_id"]:
        completed["operation_id"] = operation["operation_id"]
    completed["method"] = operation["method"]
    completed["path"] = operation["path"]

    request_types = operation["request_content_types"]
    if args.request_content_type:
        if args.request_content_type not in request_types:
            raise _UsageError(
                f"request content type {args.request_content_type!r} не объявлен операцией; "
                f"доступны: {request_types or '<нет тела>'}"
            )
    elif len(request_types) == 1:
        completed["request"] = {"content_type": request_types[0]}
    elif len(request_types) > 1:
        raise _UsageError(
            "у операции несколько JSON request content types; передайте --request-content-type"
        )

    if not args.response:
        response_variants = [_response_from_report(item) for item in operation["responses"]]
        successful = [
            item
            for item in response_variants
            if isinstance(item["status"], int) and 200 <= item["status"] < 300
        ]
        selectable = successful or response_variants
        if len(selectable) == 1:
            completed["responses"] = selectable
        elif len(selectable) > 1:
            variants = ", ".join(operation["responses"])
            raise _UsageError(
                f"у операции несколько вариантов ответа ({variants}); передайте --response"
            )
        else:
            raise _UsageError("у операции нет поддерживаемого варианта ответа")

    if not args.python_path:
        completed["python_path"] = list(python_path_from_key(args.key))
    return completed


def _response_from_report(raw: str) -> dict[str, Any]:
    """Преобразовать вариант из ``list --json`` обратно в manifest selector."""
    status, _, content_type = raw.partition(":")
    selector: dict[str, Any] = {
        "status": "default" if status == "default" else int(status),
    }
    if content_type != "-":
        selector["content_type"] = content_type
    return selector


def _parse_response_selector(raw: str) -> dict[str, Any]:
    """Разобрать селектор ответа ``STATUS[:CONTENT_TYPE]``.

    Нечисловой статус — опечатка в команде, а не расхождение контракта, поэтому
    здесь поднимается :class:`_UsageError`: иначе пользователь получал бы
    traceback вместо сообщения и код 1 вместо 2.
    """
    status, _, content_type = raw.partition(":")
    selector: dict[str, Any] = {}
    if status == "default":
        selector["status"] = "default"
    else:
        try:
            selector["status"] = int(status)
        except ValueError as exc:
            raise _UsageError(
                f"--response {raw!r}: статус должен быть целым числом или 'default'"
            ) from exc
    if content_type:
        selector["content_type"] = content_type
    return selector


# ------------------------------------------------------------------- update


def _load(args: argparse.Namespace) -> tuple[Manifest, Any]:
    manifest = load_manifest(args.manifest)
    waivers = load_waivers(manifest.base_dir / manifest.waivers_path)
    waivers.validate(manifest, today=dt.date.today())
    return manifest, waivers


def _cmd_update(args: argparse.Namespace) -> int:
    manifest, waivers = _load(args)
    result = build_contracts(manifest, waivers)
    artifacts = render_artifacts(manifest, result)
    removed = write_artifacts(manifest.output_dir(), artifacts)
    print(f"обновлено файлов: {len(artifacts.files)} в {manifest.output_dir()}")
    for path in removed:
        print(f"  удалён устаревший артефакт: {path}")
    for key, reason in artifacts.d42_disabled:
        print(f"  {key}: d42-схемы не сгенерированы — {reason}")
    for built in result.operations:
        for reason in built.unsupported:
            print(f"  {built.contract.key}: не поддержано — {reason}")
        if built.recursive:
            print(
                f"  {built.contract.key}: контракт рекурсивен, d42-схемы не генерируются "
                f"(JSON Schema и валидация работают)"
            )
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    manifest, waivers = _load(args)
    result = build_contracts(manifest, waivers)
    artifacts = render_artifacts(manifest, result)
    report = check_artifacts(manifest.output_dir(), artifacts)
    if report.is_clean:
        print(f"артефакты актуальны: {len(artifacts.files)} файл(ов)")
        return EXIT_OK
    print("generated-артефакты разошлись со спецификацией:", file=sys.stderr)
    print(report.describe(), file=sys.stderr)
    print(file=sys.stderr)
    print("Запустите 'geas update' и закоммитьте результат", file=sys.stderr)
    return EXIT_CONTRACT_ERROR


# --------------------------------------------------------------------- diff


def _cmd_diff(args: argparse.Namespace) -> int:
    manifest, waivers = _load(args)
    result = build_contracts(manifest, waivers)
    after = {built.contract.key: built.document for built in result.operations}

    output_dir = manifest.output_dir()
    index = read_generated_index(output_dir)
    before: dict[str, Any] = {}
    for key, item in (index.get("operations") or {}).items():
        path = output_dir / "contracts" / f"{item['slug']}.json"
        if path.is_file():
            before[key] = json.loads(path.read_text(encoding="utf-8"))

    diff = diff_operations(before, after)
    if args.json:
        payload = {
            "added": list(diff.added),
            "removed": list(diff.removed),
            "changed": [
                {
                    "key": item.key,
                    "semantic": [
                        {
                            "kind": change.kind.value,
                            "pointer": change.pointer,
                            "before": change.before,
                            "after": change.after,
                        }
                        for change in item.semantic
                    ],
                    "cosmetic": [
                        {"kind": change.kind.value, "pointer": change.pointer}
                        for change in item.cosmetic
                    ],
                }
                for item in diff.changed
            ],
            "is_semantic": diff.is_semantic,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_CONTRACT_ERROR if diff.is_semantic else EXIT_OK

    if diff.is_empty:
        print("контракты не изменились")
        return EXIT_OK
    for key in diff.added:
        print(f"новая операция: {key}")
    for key in diff.removed:
        print(f"операция исчезла: {key}")
    for item in diff.changed:
        print(f"{item.key}:")
        for change in item.semantic:
            print(f"  [контракт] {change.describe()}")
        for change in item.cosmetic:
            print(f"  [оформление] {change.describe()}")
    return EXIT_CONTRACT_ERROR if diff.is_semantic else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

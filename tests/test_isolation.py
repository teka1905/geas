"""Изоляция от опциональных зависимостей: ядро живёт без ``d42`` и без ``jj``.

Обещание библиотеки простое: установить её можно голой, и всё, что не требует
extra, обязано работать — импорт пакета, импорт generated-реестра, проверка
данных по JSON Schema, CLI. А то, что действительно требует extra, обязано
падать не ``ModuleNotFoundError`` где-то в кишках, а понятной ошибкой с точной
командой установки.

Проверять это ``monkeypatch``-ем нельзя: ``d42`` и ``jj`` стоят в dev-окружении и
могли попасть в ``sys.modules`` любым соседним тестом. Поэтому каждый случай
выполняется в чистом подпроцессе, где импорт этих модулей запрещён на уровне
``sys.meta_path`` (``support.run_isolated``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from support import Project, make_project, run_isolated, spec

#: Тело ответа 400 у deleteDocument — ErrorResponse из фикстуры basic/.
ERROR_BODY = '{"code": "not_found", "message": "документ не найден"}'


def build_project(root: Path) -> Project:
    """Собрать проект-потребитель с артефактами.

    Артефакты генерируются здесь, в обычном окружении: генерация d42-схем сама
    требует extra ``[d42]``. Подпроцесс потом только читает готовое.

    ``api.createDocument`` идёт с ``d42: false`` (тесту нужен только core), а
    ``api.deleteDocument`` — с включённым d42: на нём проверяется отказ
    ``d42_schema`` без extra.
    """
    project = make_project(root, package="isolated_contracts")
    project.write_spec("api/openapi.yaml", spec("basic", "openapi30.yaml"))
    project.write_manifest(
        sources={"main": {"path": "api/openapi.yaml", "selection": "explicit", "root": "api"}},
        operations={
            "api.createDocument": {
                "source": "main",
                "operation_id": "createDocument",
                "python_path": ["api", "create_document"],
                "d42": False,
            },
            "api.deleteDocument": {
                "source": "main",
                "operation_id": "deleteDocument",
                "python_path": ["api", "delete_document"],
            },
        },
    )
    project.update()
    return project


@pytest.fixture(scope="module")
def project_root(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Корень собранного проекта — его подставляют в ``sys.path`` подпроцесса."""
    return str(build_project(tmp_path_factory.mktemp("isolation")).root)


def run(code: str, *, block: tuple[str, ...]) -> subprocess.CompletedProcess[Any]:
    """Выполнить код в подпроцессе и потребовать успеха.

    Провал печатается целиком: разбирать чужой traceback по обрезанному хвосту
    невозможно.
    """
    result = run_isolated(code, block=block)
    assert result.returncode == 0, (
        f"подпроцесс упал с кодом {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result


def with_project(root: str, body: str) -> str:
    """Обернуть код так, чтобы generated-пакет проекта был импортируем."""
    return f"import sys\nsys.path.insert(0, {root!r})\n{body}"


def test_core_imports_without_optional_extras() -> None:
    """``import geas`` не тянет ни d42, ни jj.

    Версия сравнивается с версией самого пакета, а не с литералом: иначе каждый
    bump ронял бы тест изоляции, к которому номер версии отношения не имеет.
    """
    from geas import __version__

    result = run(
        "import sys\n"
        "import geas\n"
        "print(geas.__version__)\n"
        "print(sorted(name for name in sys.modules if name in ('d42', 'jj')))\n",
        block=("d42", "jj"),
    )

    lines = result.stdout.split()
    assert lines[0] == __version__
    assert result.stdout.strip().endswith("[]")


def test_generated_package_imports_without_optional_extras(project_root: str) -> None:
    """Generated-реестр импортируется и отдаёт ручки на голом ядре."""
    result = run(
        with_project(
            project_root,
            "import sys\n"
            "from isolated_contracts.generated import operations\n"
            "print(operations.api.create_document.key)\n"
            "print(sorted(name for name in sys.modules if name in ('d42', 'jj')))\n",
        ),
        block=("d42", "jj"),
    )

    assert "api.createDocument" in result.stdout
    assert result.stdout.strip().endswith("[]")


def test_validate_response_works_on_json_schema_alone(project_root: str) -> None:
    """Проверка ответа работает без extras — путь JSON Schema самодостаточен.

    Операция взята с включённым d42 намеренно: у варианта есть имя generated
    d42-схемы, но без установленного extra библиотека обязана тихо ограничиться
    JSON Schema, а не упасть на импорте.
    """
    result = run(
        with_project(
            project_root,
            "from isolated_contracts.generated import operations\n"
            "from geas.errors import ResponseContractError\n"
            "handle = operations.api.delete_document\n"
            f"variant = handle.validate_response({ERROR_BODY}, status=400)\n"
            "print('OK', variant.label(), variant.d42_export)\n"
            "try:\n"
            "    handle.validate_response({'code': 'unknown', 'message': 'x'}, status=400)\n"
            "except ResponseContractError as error:\n"
            "    print('REJECTED', error.json_pointer, error.validator)\n",
        ),
        block=("d42", "jj"),
    )

    assert "OK 400:application/json GeneratedErrorResponseSchema" in result.stdout
    assert "REJECTED /code enum" in result.stdout


def test_mock_without_jj_extra_names_the_install_command(project_root: str) -> None:
    """``handle.mock()`` без ``[jj]`` объясняет, что именно поставить."""
    result = run(
        with_project(
            project_root,
            "from isolated_contracts.generated import operations\n"
            "from geas.errors import MissingExtraError\n"
            "try:\n"
            "    operations.api.create_document.mock(response=None, status=200)\n"
            "except MissingExtraError as error:\n"
            "    print('EXTRA', error.extra)\n"
            "    print('MESSAGE', error)\n",
        ),
        block=("jj",),
    )

    assert "EXTRA jj" in result.stdout
    assert "geas[jj]" in result.stdout


def test_d42_schema_without_d42_extra_names_the_install_command(project_root: str) -> None:
    """``handle.d42_schema()`` без ``[d42]`` тоже называет команду установки."""
    result = run(
        with_project(
            project_root,
            "from isolated_contracts.generated import operations\n"
            "from geas.errors import MissingExtraError\n"
            "from geas.models import Direction\n"
            "handle = operations.api.delete_document\n"
            "export = handle.response(status=400).d42_export\n"
            "try:\n"
            "    handle.d42_schema(Direction.RESPONSE, export=export)\n"
            "except MissingExtraError as error:\n"
            "    print('EXTRA', error.extra)\n"
            "    print('MESSAGE', error)\n",
        ),
        block=("d42",),
    )

    assert "EXTRA d42" in result.stdout
    assert "geas[d42]" in result.stdout


def test_cli_help_runs_without_optional_extras() -> None:
    """CLI поднимается и печатает справку на голом ядре."""
    result = run(
        "from geas.cli import main\n"
        "try:\n"
        "    main(['--help'])\n"
        "except SystemExit as exit_code:\n"
        "    print('EXIT', exit_code.code)\n",
        block=("d42", "jj"),
    )

    assert "usage: geas" in result.stdout
    assert "EXIT 0" in result.stdout


def test_blocker_really_blocks() -> None:
    """Контроль самого стенда: без блокировки d42 импортируется, с ней — нет.

    Без этой проверки все тесты модуля могли бы «проходить» просто потому, что
    запрет не сработал.
    """
    allowed = run_isolated("import d42\nprint('IMPORTED')\n", block=())
    assert allowed.returncode == 0, allowed.stderr
    assert "IMPORTED" in allowed.stdout

    blocked = run_isolated("import d42\nprint('IMPORTED')\n", block=("d42",))
    assert blocked.returncode != 0
    assert "намеренно недоступен" in blocked.stderr

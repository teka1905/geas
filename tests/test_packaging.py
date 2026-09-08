"""Сборка дистрибутива и установка колеса в чистое окружение.

Тесты медленные (создают venv и ставят пакет), поэтому помечены ``slow`` и
исключаются из быстрого прогона: ``pytest -m "not slow"``. Пропускать их совсем
нельзя — именно они доказывают, что библиотека устанавливается, а не только
запускается из исходников, и что ядро работает **без** опциональных extras.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from support import REPO_ROOT

pytestmark = pytest.mark.slow


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, cwd=cwd, check=False)


@pytest.fixture(scope="module")
def distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Собрать wheel и sdist во временный каталог."""
    if shutil.which(sys.executable) is None:  # pragma: no cover - защита от странных окружений
        pytest.skip("нет интерпретатора для сборки")
    outdir = tmp_path_factory.mktemp("dist")
    result = _run(sys.executable, "-m", "build", "--outdir", str(outdir), str(REPO_ROOT))
    if result.returncode != 0:
        if "No module named build" in result.stderr:
            pytest.skip("модуль build не установлен")
        pytest.fail(f"сборка не удалась:\n{result.stdout}\n{result.stderr}")
    wheels = sorted(outdir.glob("*.whl"))
    sdists = sorted(outdir.glob("*.tar.gz"))
    assert wheels, f"wheel не собран: {sorted(p.name for p in outdir.iterdir())}"
    assert sdists, f"sdist не собран: {sorted(p.name for p in outdir.iterdir())}"
    return wheels[0], sdists[0]


def test_wheel_and_sdist_are_built(distributions: tuple[Path, Path]) -> None:
    """``python -m build`` даёт и колесо, и исходный дистрибутив."""
    wheel, sdist = distributions

    assert wheel.name.startswith("openapi_contract_fixtures-")
    assert sdist.name.startswith("openapi_contract_fixtures-")


def test_wheel_contains_py_typed(distributions: tuple[Path, Path]) -> None:
    """``py.typed`` обязан попасть в колесо, иначе типы не видны потребителю."""
    wheel, _ = distributions

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    assert "openapi_contracts/py.typed" in names
    assert "openapi_contracts/integrations/d42/converter.py" in names
    assert "openapi_contracts/integrations/jj/contract_mock.py" in names


def test_package_metadata_passes_twine(distributions: tuple[Path, Path]) -> None:
    """Метаданные пакета корректны с точки зрения twine."""
    wheel, sdist = distributions
    result = _run(sys.executable, "-m", "twine", "check", str(wheel), str(sdist))
    if result.returncode != 0 and "No module named twine" in result.stderr:
        pytest.skip("twine не установлен")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "PASSED" in result.stdout


def test_wheel_installs_into_a_clean_environment(
    distributions: tuple[Path, Path], tmp_path: Path
) -> None:
    """Колесо ставится в чистый venv, и ядро там работает без d42 и без JJ.

    Это главный тест файла: он проверяет ровно то обещание, из-за которого ядро
    вообще отделено от интеграций.
    """
    wheel, _ = distributions
    venv = tmp_path / "clean"
    assert _run(sys.executable, "-m", "venv", str(venv)).returncode == 0

    python = venv / "bin" / "python"
    if not python.exists():  # pragma: no cover - Windows
        python = venv / "Scripts" / "python.exe"

    install = _run(str(python), "-m", "pip", "install", "--no-input", "-q", str(wheel))
    assert install.returncode == 0, install.stderr

    probe = _run(
        str(python),
        "-c",
        (
            "import json, importlib.util as u, openapi_contracts as p;"
            "print(json.dumps({"
            "'version': p.__version__,"
            "'has_d42': u.find_spec('d42') is not None,"
            "'has_jj': u.find_spec('jj') is not None,"
            "'handle': hasattr(p, 'OperationHandle'),"
            "}))"
        ),
    )
    assert probe.returncode == 0, probe.stderr
    report = json.loads(probe.stdout)

    assert report["version"] == _declared_version()
    assert report["handle"] is True
    assert report["has_d42"] is False, "extra [d42] не должен ставиться сам"
    assert report["has_jj"] is False, "extra [jj] не должен ставиться сам"

    cli = venv / "bin" / "openapi-contracts"
    if not cli.exists():  # pragma: no cover - Windows
        cli = venv / "Scripts" / "openapi-contracts.exe"
    help_result = _run(str(cli), "--help")
    assert help_result.returncode == 0, help_result.stderr
    assert "openapi-contracts" in help_result.stdout

    version_result = _run(str(cli), "--version")
    assert version_result.returncode == 0
    assert _declared_version() in version_result.stdout


def test_missing_extra_error_names_the_install_command(
    distributions: tuple[Path, Path], tmp_path: Path
) -> None:
    """Без extra ``[jj]`` ``mock()`` объясняет, что именно поставить."""
    wheel, _ = distributions
    venv = tmp_path / "clean-extra"
    assert _run(sys.executable, "-m", "venv", str(venv)).returncode == 0
    python = venv / "bin" / "python"
    if not python.exists():  # pragma: no cover - Windows
        python = venv / "Scripts" / "python.exe"
    assert _run(str(python), "-m", "pip", "install", "--no-input", "-q", str(wheel)).returncode == 0

    probe = _run(
        str(python),
        "-c",
        (
            "from openapi_contracts.errors import MissingExtraError;"
            "e = MissingExtraError('jj', 'OperationHandle.mock()');"
            "print(str(e))"
        ),
    )

    assert probe.returncode == 0, probe.stderr
    assert "openapi-contract-fixtures[jj]" in probe.stdout


def _declared_version() -> str:
    """Версия из ``pyproject.toml`` — источник истины для дистрибутива.

    ``tomllib`` появился только в Python 3.11, а поддерживается и 3.10, поэтому
    есть запасной разбор регуляркой: нужна ровно одна строка ``version`` из
    секции ``[project]``.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib
    except ModuleNotFoundError:
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
        assert match is not None, "в pyproject.toml не найдена версия"
        return match.group(1)
    return str(tomllib.loads(text)["project"]["version"])

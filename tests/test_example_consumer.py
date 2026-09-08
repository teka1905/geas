"""Пример-потребитель проверяется как настоящий проект, а не как текст в README.

Две независимые проверки:

1. ``openapi-contracts -m examples/consumer/manifest.yaml check`` — закоммиченные
   артефакты примера актуальны. Если кто-то поправит спецификацию и забудет
   ``update``, здесь это и вскроется;
2. собственные тесты примера действительно проходят. Они гоняются подпроцессом:
   пример живёт в своём ``sys.path`` (пакет ``app_contracts``), а мок-сервер JJ
   поднимается на свободном порту loopback.

Сервер JJ поднимается прямо здесь (``python -m jj -H 127.0.0.1 -p <port>``) и
адресуется через ``JJ_REMOTE_MOCK_URL``. Если в наборе появится общий помощник
``tests/jj_server.py``, эту часть надо будет заменить на него.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager

import pytest

from support import REPO_ROOT, SRC_ROOT

#: Каталог примера-потребителя.
EXAMPLE_ROOT = REPO_ROOT / "examples" / "consumer"

#: Manifest примера.
EXAMPLE_MANIFEST = EXAMPLE_ROOT / "manifest.yaml"

#: Тесты примера.
EXAMPLE_TESTS = EXAMPLE_ROOT / "tests" / "test_documents.py"

#: Сколько ждать, пока мок-сервер начнёт принимать соединения.
STARTUP_TIMEOUT = 20.0


def _free_port() -> int:
    """Занять и сразу отпустить свободный порт на loopback."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_listening(port: int, process: subprocess.Popen[str]) -> None:
    """Дождаться, пока порт начнёт принимать соединения."""
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"мок-сервер JJ завершился с кодом {process.returncode}:\n{_drain(process)}"
            )
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=0.5)):
                return
        except OSError:
            time.sleep(0.05)
    process.terminate()
    raise RuntimeError(f"мок-сервер JJ не поднялся за {STARTUP_TIMEOUT} с:\n{_drain(process)}")


def _drain(process: subprocess.Popen[str]) -> str:
    """Забрать вывод завершившегося (или убиваемого) процесса, не подвисая."""
    try:
        output, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=5)
    return output or ""


@contextmanager
def jj_mock_server() -> Iterator[str]:
    """Поднять локальный мок-сервер JJ и отдать его URL."""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "jj", "-H", "127.0.0.1", "-p", str(port)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_until_listening(port, process)
        yield f"http://127.0.0.1:{port}"
    finally:
        if process.poll() is None:
            process.terminate()
        _drain(process)


def _run(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )


def test_example_exists() -> None:
    """Пример на месте, включая закоммиченные generated-артефакты."""
    assert EXAMPLE_MANIFEST.is_file()
    assert EXAMPLE_TESTS.is_file()
    generated = EXAMPLE_ROOT / "app_contracts" / "generated"
    assert (generated / "_generated.json").is_file()
    assert (generated / "operations.py").is_file()
    assert sorted(path.name for path in (generated / "contracts").glob("*.json")) == [
        "api__create_document.json",
        "api__list_documents.json",
    ]


def test_example_artifacts_are_current() -> None:
    """``check`` на примере чист: закоммиченные артефакты соответствуют спецификации."""
    result = _run(
        [sys.executable, "-m", "openapi_contracts.cli", "-m", str(EXAMPLE_MANIFEST), "check"],
        env={"PYTHONPATH": str(SRC_ROOT)},
    )
    assert result.returncode == 0, (
        "артефакты примера разошлись со спецификацией; запустите "
        f"'openapi-contracts -m {EXAMPLE_MANIFEST} update'\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "артефакты актуальны" in result.stdout


def test_example_tests_pass() -> None:
    """Собственные тесты примера проходят на живом мок-сервере JJ."""
    pytest.importorskip("jj", reason="тестам примера нужен extra [jj]")
    pytest.importorskip("d42", reason="тестам примера нужен extra [d42]")
    pytest.importorskip("aiohttp", reason="примеру нужен HTTP-клиент")

    with jj_mock_server() as url:
        result = _run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(EXAMPLE_TESTS),
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            env={
                "JJ_REMOTE_MOCK_URL": url,
                # Пример импортирует и app_contracts, и саму библиотеку из src.
                "PYTHONPATH": os.pathsep.join([str(SRC_ROOT), str(EXAMPLE_ROOT)]),
            },
        )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "3 passed" in result.stdout


def test_example_uses_only_public_entry_points() -> None:
    """Пример не лезет во внутренности интеграций.

    Это не стилистика: всё, что импортируется из ``integrations.<x>.<модуль>``,
    становится де-факто публичным API и связывает руки при рефакторинге. Пример —
    образец для копирования, поэтому он обязан держаться публичных точек входа.
    """
    for path in sorted(EXAMPLE_ROOT.rglob("*.py")):
        if "generated" in path.relative_to(EXAMPLE_ROOT).parts:
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            assert "openapi_contracts.integrations.jj" not in stripped, (
                f"{path}: интеграция с JJ подключается через OperationHandle.mock(), "
                f"а не импортом её модулей"
            )
            if "openapi_contracts.integrations.d42" in stripped:
                assert stripped.startswith("from openapi_contracts.integrations.d42 import"), (
                    f"{path}: d42-интеграция импортируется только пакетом целиком"
                )


def test_generated_package_is_owned_by_generator() -> None:
    """Каждый файл в ``generated/`` перечислен генератором как owned."""
    generated = EXAMPLE_ROOT / "app_contracts" / "generated"
    index = json.loads((generated / "_generated.json").read_text(encoding="utf-8"))
    owned = set(index["owned"])
    on_disk = {
        path.relative_to(generated).as_posix()
        for path in generated.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert on_disk == owned

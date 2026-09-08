"""Настоящий удалённый JJ-мок-сервер на loopback — для тестов JJ-интеграции.

Модуль поднимает ``python -m jj -H 127.0.0.1 -p <port>`` отдельным процессом и
подсовывает клиенту его адрес через переменную окружения ``JJ_REMOTE_MOCK_URL``:
именно так JJ выбирает, куда регистрировать моки и откуда забирать историю.

Почему настоящий сервер, а не заглушка: ``ContractMock`` проверяет перехваченные
запросы по данным, которые JJ отдаёт в истории (метод, путь, ``segments``,
повторяющиеся query-параметры, сырое тело). Подделать их — значит проверять
собственные представления о JJ, а не поведение интеграции.

Loopback разрешён политикой сети из ``tests/conftest.py`` явно, поэтому тесты
остаются офлайновыми: наружу ни один пакет не уходит.

Использование::

    from jj_server import jj_server  # noqa: F401 - fixture

    async def test_something(jj_server):
        ...

Если сервер поднять не удалось (нет ``jj``, порт не отдали, процесс умер), тесты
**пропускаются**, а не падают: недоступное окружение — это не дефект библиотеки.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

__all__ = ["JJ_URL_ENV", "JJServer", "jj_server", "start_jj_server"]

#: Переменная окружения, по которой клиент JJ выбирает удалённый мок-сервер.
JJ_URL_ENV = "JJ_REMOTE_MOCK_URL"

#: Сколько ждать, пока сервер начнёт принимать соединения.
STARTUP_TIMEOUT = 20.0

#: Пауза между попытками достучаться до сервера.
POLL_INTERVAL = 0.05

#: Сколько ждать корректного завершения процесса перед kill.
SHUTDOWN_TIMEOUT = 5.0

#: Хост всегда loopback: политика сети в conftest не пропустит ничего другого.
HOST = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class JJServer:
    """Запущенный мок-сервер: адрес, процесс и файл с его логом."""

    host: str
    port: int
    process: subprocess.Popen[bytes]
    log_path: Path

    @property
    def url(self) -> str:
        """Базовый URL сервера — то же, что попадает в ``JJ_REMOTE_MOCK_URL``."""
        return f"http://{self.host}:{self.port}"

    def log(self) -> str:
        """Содержимое лога процесса — попадает в сообщение о пропуске теста."""
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _free_port(host: str = HOST) -> int:
    """Занять свободный порт и сразу отпустить его.

    Гонка здесь возможна в принципе, но локально и на CI она пренебрежима, а
    альтернатива — передавать сокет в чужой процесс — того не стоит.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _accepts_connections(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """Принимает ли кто-нибудь соединения на этом адресе."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def start_jj_server() -> JJServer:
    """Поднять сервер и дождаться, пока он начнёт отвечать.

    :raises RuntimeError: сервер не запустился — вызывающий сам решает,
        пропустить тесты или упасть.
    """
    try:
        import jj  # noqa: F401
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError(f"jj не установлен: {exc}") from exc

    port = _free_port()
    handle, log_name = tempfile.mkstemp(prefix="jj-server-", suffix=".log")
    log_path = Path(log_name)
    process = subprocess.Popen(
        [sys.executable, "-m", "jj", "-H", HOST, "-p", str(port)],
        stdout=handle,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    os.close(handle)

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = log_path.read_text(encoding="utf-8", errors="replace")
            _cleanup(process, log_path)
            raise RuntimeError(
                f"процесс jj завершился с кодом {process.returncode}. Вывод:\n{output}"
            )
        if _accepts_connections(HOST, port):
            return JJServer(host=HOST, port=port, process=process, log_path=log_path)
        time.sleep(POLL_INTERVAL)

    output = log_path.read_text(encoding="utf-8", errors="replace")
    _cleanup(process, log_path)
    raise RuntimeError(
        f"jj не начал слушать {HOST}:{port} за {STARTUP_TIMEOUT} с. Вывод:\n{output}"
    )


def _cleanup(process: subprocess.Popen[bytes], log_path: Path) -> None:
    """Остановить процесс и убрать его лог."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:  # pragma: no cover - редкий путь
            process.kill()
            process.wait(timeout=SHUTDOWN_TIMEOUT)
    log_path.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def jj_server() -> Iterator[JJServer]:
    """Сессионный мок-сервер JJ с прописанным ``JJ_REMOTE_MOCK_URL``.

    Сервер один на всю сессию: старт процесса стоит заметно дороже, чем
    регистрация мока, а изоляция тестов обеспечивается тем, что каждый
    ``ContractMock`` создаётся disposable и снимается на выходе из блока.
    """
    try:
        server = start_jj_server()
    except RuntimeError as exc:
        pytest.skip(f"локальный JJ-мок-сервер недоступен: {exc}")

    previous = os.environ.get(JJ_URL_ENV)
    os.environ[JJ_URL_ENV] = server.url
    try:
        yield server
    finally:
        if previous is None:
            os.environ.pop(JJ_URL_ENV, None)
        else:
            os.environ[JJ_URL_ENV] = previous
        _cleanup(server.process, server.log_path)

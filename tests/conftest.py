"""Общие фикстуры набора тестов: данные, изоляция от сети, временный проект.

Модуль сознательно не импортирует ничего из ``geas`` на уровне
модуля. Conftest должен собираться даже тогда, когда часть пакета ещё не
дописана: иначе ошибка импорта одного модуля превращается в ошибку сбора всех
тестов и прячет настоящую причину падения. Всё, что нужно от самого пакета,
тесты импортируют сами.

Ключевое здесь — автоиспользуемая фикстура :func:`no_outbound_network`. Тесты
работают только на локальных данных, поэтому попытка выйти в сеть — это не
«медленный тест», а дефект: где-то не сработал мок. Такая попытка падает сразу
и с объяснением политики.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

__all__ = ["FIXTURES", "SPECS", "TmpProject"]

#: Корень тестовых данных.
FIXTURES = Path(__file__).parent / "fixtures"

#: Каталог OpenAPI-спецификаций внутри тестовых данных.
SPECS = FIXTURES / "specs"

#: Расширения, которые перебираются, если имя спецификации задано без суффикса.
SPEC_SUFFIXES = (".yaml", ".yml", ".json")

#: Относительное имя каталога спецификаций — для сообщений об ошибках
#: (абсолютные пути в выводе недетерминированны и бесполезны в CI).
_SPECS_LABEL = "tests/fixtures/specs"

_NETWORK_POLICY = (
    "тесты выполняются без доступа в сеть. Все спецификации, схемы и ответы "
    "берутся из tests/fixtures, внешние сервисы не опрашиваются. Разрешены "
    "только AF_UNIX и loopback (127.0.0.0/8, ::1) — их использует локальный "
    "HTTP-сервер моков. Если сюда попал реальный хост, значит не сработал мок: "
    "почините мок, а не политику."
)


@dataclass(frozen=True)
class TmpProject:
    """Изолированный каталог проекта во временной директории.

    Содержимое manifest и waivers сознательно не задаётся: форматы проверяются
    самими тестами, а фикстура даёт только каркас — корень, договорённые пути к
    двум файлам и запись текста внутрь корня.
    """

    root: Path

    @property
    def manifest_path(self) -> Path:
        """Договорной путь к manifest. Файл не создаётся — его пишет тест."""
        return self.root / "contracts.yaml"

    @property
    def waivers_path(self) -> Path:
        """Договорной путь к waivers. Файл не создаётся — его пишет тест."""
        return self.root / "waivers.yaml"

    def path(self, *parts: str) -> Path:
        """Путь внутри проекта, собранный из сегментов."""
        return self.root.joinpath(*parts)

    def write(self, path: str | Path, text: str) -> Path:
        """Записать ``text`` в файл проекта и вернуть его путь.

        Относительный путь разрешается от корня проекта, недостающие каталоги
        создаются. Абсолютный путь вне корня запрещён: временный проект на то и
        временный, чтобы тест не мог случайно задеть рабочее дерево.
        """
        target = Path(path)
        if not target.is_absolute():
            target = self.root / target
        resolved = target.resolve()
        root = self.root.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"путь {path!r} выходит за пределы временного проекта — "
                "используйте путь относительно его корня"
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(text, encoding="utf-8")
        return resolved


def _is_loopback_host(host: object) -> bool:
    """Является ли хост локальной петлёй."""
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    # Пустой хост — это INADDR_ANY, наружу такое соединение не уходит.
    if host in {"", "localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    # У IPv6-адреса может быть zone id: '::1%lo0'.
    candidate = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # Доменное имя резолвить нельзя — резолв сам по себе поход в сеть.
        return False


def _is_allowed_address(family: object, address: object) -> bool:
    """Разрешено ли соединение по этому семейству и адресу."""
    unix_family = getattr(socket, "AF_UNIX", None)
    if unix_family is not None and family == unix_family:
        return True
    if isinstance(address, (str, bytes, Path)):
        # Строковый адрес вне AF_INET* — это путь к сокету, а не сеть.
        return True
    if isinstance(address, tuple) and address:
        return _is_loopback_host(address[0])
    return False


def _denied(address: object) -> str:
    return f"Исходящее сетевое соединение запрещено: {address!r}. {_NETWORK_POLICY}"


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запретить любые исходящие соединения, кроме AF_UNIX и loopback.

    Патчатся обе точки входа — ``socket.socket.connect`` и
    ``socket.create_connection``: вторая идёт через первую, но собственная
    проверка даёт понятное сообщение до попытки резолва имени.
    """
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if not _is_allowed_address(self.family, address):
            raise RuntimeError(_denied(address))
        return original_connect(self, address)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_allowed_address(socket.AF_INET, address):
            raise RuntimeError(_denied(address))
        return original_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)


@pytest.fixture(scope="session")
def spec_path() -> Callable[[str], Path]:
    """Фабрика путей к спецификациям из ``tests/fixtures/specs``.

    Имя можно указывать с расширением (``petstore.yaml``) или без него — тогда
    перебираются ``.yaml``, ``.yml``, ``.json`` в этом порядке. Отсутствующая
    спецификация — это ошибка теста, а не пустой путь, поэтому фабрика падает
    сразу и перечисляет доступные файлы.
    """

    def resolve(name: str) -> Path:
        candidate = SPECS / name
        variants = (
            [candidate]
            if candidate.suffix
            else [candidate.with_name(candidate.name + suffix) for suffix in SPEC_SUFFIXES]
        )
        for variant in variants:
            if variant.is_file():
                return variant
        available = (
            sorted(item.name for item in SPECS.iterdir() if item.is_file())
            if SPECS.is_dir()
            else []
        )
        raise FileNotFoundError(
            f"спецификация {name!r} не найдена в {_SPECS_LABEL}/. Доступны: {available}"
        )

    return resolve


@pytest.fixture
def tmp_project(tmp_path: Path) -> TmpProject:
    """Пустой изолированный каталог проекта с помощником записи файлов."""
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    return TmpProject(root=root)

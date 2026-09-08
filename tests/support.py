"""Общие помощники тестов.

Здесь собрано всё, что нужно, чтобы поднять изолированный проект-потребитель во
временном каталоге: свой manifest, свой waivers, свой каталог артефактов и
импорт generated-пакета без загрязнения ``sys.path`` соседних тестов.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import textwrap
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FIXTURES = Path(__file__).parent / "fixtures"
SPECS = FIXTURES / "specs"

#: Корень репозитория — нужен, чтобы подпроцессы видели ``src``.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def spec(*parts: str) -> Path:
    """Путь к синтетической спецификации из ``tests/fixtures/specs``."""
    path = SPECS.joinpath(*parts)
    if not path.exists():
        available = sorted(
            item.relative_to(SPECS).as_posix() for item in SPECS.rglob("*") if item.is_file()
        )
        raise FileNotFoundError(f"нет фикстуры {'/'.join(parts)!r}. Доступны: {available}")
    return path


@dataclass(slots=True)
class Project:
    """Изолированный проект-потребитель во временном каталоге."""

    root: Path
    package: str = "demo_contracts"
    _manifest_document: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ пути

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.yaml"

    @property
    def waivers_path(self) -> Path:
        return self.root / "waivers.yaml"

    @property
    def output_dir(self) -> Path:
        return self.root / self.package / "generated"

    # ------------------------------------------------------------- настройка

    def write_spec(self, relative: str, source: Path | str) -> Path:
        """Скопировать фикстуру-спецификацию внутрь проекта."""
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = Path(source).read_text(encoding="utf-8") if isinstance(source, Path) else source
        target.write_text(text, encoding="utf-8")
        return target

    def write(self, relative: str, text: str) -> Path:
        """Записать произвольный файл внутрь проекта."""
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")
        return target

    def write_manifest(
        self,
        *,
        sources: Mapping[str, Any],
        operations: Mapping[str, Any] | None = None,
        policies: Mapping[str, Any] | None = None,
    ) -> Path:
        """Собрать и записать manifest."""
        document: dict[str, Any] = {
            "version": 1,
            "output": {
                "directory": f"{self.package}/generated",
                "package": f"{self.package}.generated",
            },
            "waivers": "waivers.yaml",
            "sources": dict(sources),
            "operations": dict(operations or {}),
        }
        if policies:
            document["policies"] = dict(policies)
        self._manifest_document = document
        self.manifest_path.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return self.manifest_path

    def write_waivers(self, waivers: list[Mapping[str, Any]]) -> Path:
        """Записать ``waivers.yaml``."""
        self.waivers_path.write_text(
            yaml.safe_dump(
                {"version": 1, "waivers": [dict(item) for item in waivers]},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return self.waivers_path

    def manifest_bytes(self) -> bytes:
        """Байты manifest — для проверки транзакционности ``add``."""
        return self.manifest_path.read_bytes()

    # -------------------------------------------------------------- действия

    def load(self) -> Any:
        """Загрузить manifest объектом."""
        from openapi_contracts.manifest import load_manifest

        return load_manifest(self.manifest_path)

    def waivers(self) -> Any:
        """Загрузить набор waiver'ов."""
        from openapi_contracts.waivers import load_waivers

        return load_waivers(self.waivers_path)

    def build(self) -> Any:
        """Собрать контракты."""
        from openapi_contracts.contracts import build_contracts

        return build_contracts(self.load(), self.waivers())

    def render(self) -> Any:
        """Отрендерить набор артефактов, ничего не записывая."""
        from openapi_contracts.artifacts import render_artifacts

        manifest = self.load()
        return render_artifacts(manifest, self.build())

    def update(self) -> Any:
        """Записать артефакты на диск."""
        from openapi_contracts.artifacts import write_artifacts

        manifest = self.load()
        artifacts = self.render()
        write_artifacts(manifest.output_dir(), artifacts)
        return artifacts

    def check(self) -> Any:
        """Проверить артефакты на drift."""
        from openapi_contracts.artifacts import check_artifacts

        manifest = self.load()
        return check_artifacts(manifest.output_dir(), self.render())

    def cli(self, *args: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
        """Запустить CLI в подпроцессе — так проверяются коды возврата."""
        import os

        environment = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
        if env:
            environment.update(env)
        return subprocess.run(
            [sys.executable, "-m", "openapi_contracts.cli", "-m", str(self.manifest_path), *args],
            capture_output=True,
            text=True,
            cwd=self.root,
            env=environment,
        )

    def contract_document(self, slug: str) -> dict[str, Any]:
        """Прочитать сгенерированный документ контракта."""
        return json.loads(
            (self.output_dir / "contracts" / f"{slug}.json").read_text(encoding="utf-8")
        )

    def generated_index(self) -> dict[str, Any]:
        """Прочитать ``_generated.json``."""
        return json.loads((self.output_dir / "_generated.json").read_text(encoding="utf-8"))

    @contextmanager
    def importable(self) -> Iterator[Any]:
        """Импортировать generated-пакет и убрать его из ``sys.modules`` на выходе."""
        (self.root / self.package / "__init__.py").write_text("", encoding="utf-8")
        sys.path.insert(0, str(self.root))
        prefix = f"{self.package}."
        try:
            module = importlib.import_module(f"{self.package}.generated")
            yield module
        finally:
            sys.path.remove(str(self.root))
            for name in [
                key for key in sys.modules if key == self.package or key.startswith(prefix)
            ]:
                del sys.modules[name]


def make_project(root: Path, *, package: str = "demo_contracts") -> Project:
    """Создать пустой проект во временном каталоге."""
    root.mkdir(parents=True, exist_ok=True)
    (root / package).mkdir(parents=True, exist_ok=True)
    (root / package / "__init__.py").write_text("", encoding="utf-8")
    project = Project(root=root, package=package)
    project.write_waivers([])
    return project


def run_isolated(code: str, *, block: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    """Выполнить код в чистом подпроцессе, запретив импорт перечисленных модулей.

    Так проверяется, что ядро и generated-реестр импортируются без ``d42`` и без
    ``jj``: обычного ``monkeypatch`` мало, потому что модули уже могли попасть в
    ``sys.modules`` соседним тестом.
    """
    import os

    blocker = textwrap.dedent(
        f"""
        import sys

        _BLOCKED = {block!r}


        class _Blocker:
            def find_module(self, name, path=None):
                return self.find_spec(name, path)

            def find_spec(self, name, path=None, target=None):
                root = name.split(".")[0]
                if root in _BLOCKED:
                    raise ImportError(f"модуль {{root!r}} намеренно недоступен в этом тесте")
                return None


        sys.meta_path.insert(0, _Blocker())
        """
    )
    return subprocess.run(
        [sys.executable, "-c", blocker + "\n" + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
    )

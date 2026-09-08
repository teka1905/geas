"""Запуск CLI командой ``python -m openapi_contracts``."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover — выполняется отдельным процессом
    raise SystemExit(main())

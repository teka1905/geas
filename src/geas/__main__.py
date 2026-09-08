"""Запуск CLI командой ``python -m geas``."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover — выполняется отдельным процессом
    raise SystemExit(main())

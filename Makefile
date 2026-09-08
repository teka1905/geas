# Точки входа для разработки. Рецепты рассчитаны на POSIX sh.

SHELL := /bin/sh

# Интерпретатор для создания venv; venv-инструменты вызываются по явным путям,
# чтобы не зависеть от активированного окружения.
PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

# Одноразовое окружение для проверки собранного wheel.
WHEEL_VENV ?= /tmp/geas-wheel-smoke
PKG := geas

.DEFAULT_GOAL := help

.PHONY: help venv install lint format typecheck test check build wheel-smoke clean

help: ## Показать список целей
	@printf 'Цели:\n'
	@awk -F ':.*## ' '/^[a-zA-Z0-9_-]+:.*## /{printf "  %-13s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

venv: ## Создать виртуальное окружение (по умолчанию .venv)
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)

install: venv ## Установить пакет editable вместе с dev-зависимостями
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e '.[dev]'

lint: ## Проверить стиль: ruff check + ruff format --check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Отформатировать и автопочинить: ruff format + ruff check --fix
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

typecheck: ## Проверить типы (mypy strict, конфигурация в pyproject.toml)
	$(BIN)/mypy

test: ## Прогнать тесты (сеть не требуется и запрещена)
	$(BIN)/pytest -q

check: lint typecheck test ## Полная проверка перед коммитом

build: ## Собрать sdist и wheel в чистый dist/
	rm -rf dist build
	$(BIN)/python -m build

wheel-smoke: build ## Проверить собранный wheel в одноразовом venv
	rm -rf $(WHEEL_VENV)
	$(PYTHON) -m venv $(WHEEL_VENV)
	$(WHEEL_VENV)/bin/python -m pip install --quiet --upgrade pip
	$(WHEEL_VENV)/bin/python -m pip install --quiet dist/*.whl
	$(WHEEL_VENV)/bin/python -c 'import geas'
	$(WHEEL_VENV)/bin/geas --help > /dev/null
	$(WHEEL_VENV)/bin/python -m geas --help > /dev/null
	@$(WHEEL_VENV)/bin/python -c 'from importlib.metadata import version; print(version("$(PKG)"))'
	rm -rf $(WHEEL_VENV)

clean: ## Удалить артефакты сборки, кэши и __pycache__
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache
	rm -rf ./*.egg-info src/*.egg-info
	find . -path ./$(VENV) -prune -o -type d -name '__pycache__' -exec rm -rf {} +

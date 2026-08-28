# Development tasks for ora-okf.
#
# PY points at the interpreter used for every target. It defaults to the
# project virtual environment on Windows; override it anywhere else:
#
#     make test PY=.venv/bin/python
#     make test PY=python3

PY ?= .venv/Scripts/python.exe
SRC := src/ora_okf
TESTS := tests

.PHONY: help venv install test test-unit test-integration coverage lint fmt fmt-check typecheck mdlint check clean

help:
	@echo "Targets:"
	@echo "  venv             create the virtual environment"
	@echo "  install          install the package with dev extras (editable)"
	@echo "  test             run the whole test suite"
	@echo "  test-unit        run unit tests only (no database needed)"
	@echo "  test-integration run integration tests (needs a live Oracle)"
	@echo "  coverage         run tests with a coverage report"
	@echo "  lint             ruff check"
	@echo "  fmt              ruff format"
	@echo "  fmt-check        ruff format --check"
	@echo "  typecheck        mypy"
	@echo "  mdlint           markdownlint-cli2 over the repository Markdown"
	@echo "  check            lint + fmt-check + typecheck + test"
	@echo "  clean            remove caches and build artifacts"

venv:
	uv venv

install:
	uv pip install -e ".[dev]"

test:
	$(PY) -m pytest $(TESTS)

test-unit:
	$(PY) -m pytest $(TESTS)/unit

test-integration:
	$(PY) -m pytest $(TESTS)/integration -m integration

coverage:
	$(PY) -m pytest --cov=ora_okf --cov-report=term-missing $(TESTS)

lint:
	$(PY) -m ruff check $(SRC) $(TESTS)

fmt:
	$(PY) -m ruff format $(SRC) $(TESTS)

fmt-check:
	$(PY) -m ruff format --check $(SRC) $(TESTS)

typecheck:
	$(PY) -m mypy $(SRC)

mdlint:
	markdownlint-cli2 "**/*.md"

check: lint fmt-check typecheck test

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +

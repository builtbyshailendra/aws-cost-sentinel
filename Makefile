# Works with GNU make on Linux, macOS and Git Bash on Windows.
ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python.exe
else
PY := .venv/bin/python
endif

CSV ?= examples/cost_explorer_by_service.csv
FORMAT ?= markdown

.PHONY: help venv install test cov lint format run report checks docker-build clean

help:
	@echo "venv install test cov lint format run report checks docker-build clean"

venv:
	uv venv .venv --python 3.12

install: venv
	uv pip install --python $(PY) -e ".[dev]"

test:
	$(PY) -m pytest -q

cov:
	$(PY) -m pytest -q --cov=cost_sentinel --cov-report=term-missing

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

# Live scan: make run PROFILE=dev REGION=eu-west-1
run:
	$(PY) -m cost_sentinel scan $(if $(PROFILE),--profile $(PROFILE)) $(if $(REGION),--region $(REGION)) --format $(FORMAT)

# Offline: make report CSV=path/to/export.csv FORMAT=html
report:
	$(PY) -m cost_sentinel report --csv $(CSV) --format $(FORMAT)

checks:
	$(PY) -m cost_sentinel checks

docker-build:
	docker build -t aws-cost-sentinel:local .

clean:
	rm -rf .pytest_cache .ruff_cache .coverage coverage*.xml htmlcov build dist

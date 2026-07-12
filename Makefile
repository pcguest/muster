# Development targets. `make lint` is the hardening gate: style, strict
# types, security static analysis and dependency audit — all must pass.

PY ?= .venv/bin

.PHONY: lint test bench constraints

lint:
	$(PY)/ruff check src tests scripts
	$(PY)/mypy
	$(PY)/bandit -r src -q
	$(PY)/pip-audit --skip-editable

test:
	$(PY)/python -m pytest -q

bench:
	$(PY)/python scripts/bench.py

# Re-pin every dependency (with hashes) after changing pyproject.toml.
# Install a verified environment with:
#   pip install -r constraints.txt --require-hashes && pip install -e . --no-deps
constraints:
	uv pip compile pyproject.toml --extra postgres --extra keyring \
	  --generate-hashes -o constraints.txt

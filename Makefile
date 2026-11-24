.PHONY: check test fmt
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src
	uv run pytest -q
test:
	uv run pytest -q
fmt:
	uv run ruff format . && uv run ruff check . --fix

eval:
	uv run python scripts/eval_mock.py --repeat 3
build:
	uv build

# everything: lint, types, tests
check:
    uv run ruff check --fix
    uv run ruff format .
    uv run ty check


.PHONY: lint test check format

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	TESTING=1 pytest tests/ -v

check: lint test

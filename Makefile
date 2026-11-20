.PHONY: run test lint fmt format

run:
	crawlnet run quotes --export xlsx

test:
	pytest -q

lint:
	ruff check crawlnet tests

fmt format:
	ruff format crawlnet tests
	ruff check --fix crawlnet tests

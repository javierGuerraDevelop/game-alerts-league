.PHONY: fmt lint test build validate clean

fmt:
	ruff format src tests

lint:
	ruff check src tests && ruff format --check src tests

test:
	pytest -q

build:
	sam build

validate:
	sam validate --lint

clean:
	rm -rf .aws-sam .pytest_cache .ruff_cache

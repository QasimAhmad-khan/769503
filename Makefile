.PHONY: install test demo faults evaluate soak readiness all
install:
	python -m pip install -e '.[test]'
test:
	python -m pytest -q
demo:
	python -m cqc demo
faults:
	python -m cqc faults
evaluate:
	python -m cqc evaluate
soak:
	python -m cqc soak --days 14
readiness:
	-python -m cqc readiness
all: test demo faults evaluate

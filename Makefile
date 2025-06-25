# The dependencies need --allow-unsafe because sphinx and gunicorn depend on
# setuptools, which is normally not allowed to appear in a hashed dependency
# file.
.PHONY: update-deps
update-deps:
	python -m pip install --upgrade pip-tools pip setuptools
	pip-compile --upgrade --resolver=backtracking --build-isolation --allow-unsafe --generate-hashes --output-file requirements/main.txt requirements/main.in
	pip-compile --upgrade --resolver=backtracking --build-isolation --allow-unsafe --generate-hashes --output-file requirements/dev.txt requirements/dev.in

# npm dependencies have to be installed for pre-commit eslint to work.
.PHONY: init
init:
	python -m pip install --editable .
	python -m pip install --upgrade -r requirements/main.txt -r requirements/dev.txt
	rm -rf .tox
	pip install --upgrade tox pre-commit
	pre-commit install

.PHONY: update
update: update-deps init

.PHONY: docs
docs:
	tox -e docs

.PHONY: run
run:
	tox -e run

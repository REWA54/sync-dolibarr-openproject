# Raccourcis de développement. Les mêmes commandes tournent dans la CI (.github/workflows/ci.yml).
# Prérequis : uv (https://docs.astral.sh/uv/) et, pour « securite » et « image », Docker.

PY := .venv/bin
GITLEAKS := ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f

.PHONY: aide installer verifier securite image verrous

aide: ## cette aide
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

installer: ## environnement .venv avec les dépendances figées et vérifiées par empreinte
	uv venv --allow-existing --python 3.13
	uv pip install --require-hashes -r requirements-dev.txt -r requirements-build.txt
	uv pip install --no-deps --no-build-isolation -e .

verifier: ## lint, format, typage strict, tests et couverture (exigés par la CI)
	$(PY)/ruff check src tests
	$(PY)/ruff format --check src tests
	$(PY)/mypy
	$(PY)/pytest --cov

securite: ## analyse statique, failles connues des dépendances, secrets dans l'historique
	$(PY)/bandit -q -c pyproject.toml -r src
	for f in requirements.txt requirements-build.txt requirements-dev.txt; do \
	  $(PY)/pip-audit --strict --require-hashes --disable-pip --progress-spinner off -r $$f || exit 1; \
	done
	docker run --rm -v "$(CURDIR):/depot:ro" $(GITLEAKS) git /depot --redact --no-banner

image: ## image locale (amd64) puis test de fumée en conteneur durci
	docker build --platform linux/amd64 -t dolop:local .
	DOCKER_DEFAULT_PLATFORM=linux/amd64 scripts/test-fumee.sh dolop:local

verrous: ## régénère les verrous après un changement de pyproject.toml (versions en place gardées)
	uv pip compile pyproject.toml --python-version 3.13 --python-platform x86_64-manylinux_2_28 --generate-hashes -o requirements.txt
	uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-dev.txt

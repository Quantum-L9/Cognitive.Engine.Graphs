# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: graph
# layer: [build]
# tags: [L9_TEMPLATE, build, commands]
# owner: platform
# status: active
# --- /L9_META ---
# ─────────────────────────────────────────────────────────────
# L9 Graph Cognitive Engine — repository Make implementation
# ─────────────────────────────────────────────────────────────
#
# REPOSITORY-OWNED. The root Makefile is the Core-owned make-v1 facade
# (Quantum-L9/l9-ci-core tools/l9_repo/facades/make-v1.mk), reproduced
# byte-for-byte and validated byte-for-byte by Core's repository verification.
# Never edit the Makefile; it owns help/setup/validate/check/test and includes
# this file. Do not redefine any of those five targets here.
#
#   make setup     repo-setup     $(VENV) with runtime + dev + CI requirements
#   make validate  repo-validate  action refs, contract wiring + scan, payload
#                                 contract compiler, C-18 L9_META headers
#   make check     repo-check     ruff check, ruff format --check, mypy engine/,
#                                 C-009 cypher-lint (non-mutating)
#   make test      repo-test      unit suite: pytest -m unit (no Docker, no Neo4j)
#
# Core runs setup -> validate -> check -> test in that order on a plain runner,
# so none of the four leaves may need Docker or Neo4j. The pre-V2 meanings of
# the two facade names that this file used to define live on under new names:
#
#   make test-docker   the old `make test` (full suite inside the api container)
#   make check-fix     the old `make check` (autofix lint + types + unit tests)
#
# Every other target below is unchanged.

# The four leaves run through one interpreter: $(VENV)/bin/python once
# `make setup` has created it, otherwise the python3 on PATH (an already
# provisioned environment). Override with `make <phase> VENV=/path/to/venv`.
VENV ?= .venv
REPO_PY := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)

.PHONY: repo-setup repo-validate repo-check repo-test
.PHONY: dev dev-build dev-down dev-logs dev-restart health
.PHONY: test-docker test-unit test-integration seed shell neo4j-shell cypher-lint
.PHONY: local-dbs local-api local-api-legacy local-api-sdk

# ── Core Repository Execution V2 phases (make-v1 ABI) ──────

repo-setup:	## Create $(VENV) if absent; install requirements.txt + requirements-dev.txt + requirements-ci.txt
	@test -x "$(VENV)/bin/python" || python3 -m venv "$(VENV)"
	@"$(VENV)/bin/python" -m pip install --disable-pip-version-check --quiet \
		-r requirements.txt -r requirements-dev.txt -r requirements-ci.txt

repo-validate:	## Action refs, contract files + scan, payload contract compiler, C-18 L9_META headers
	@echo "── Action references ──"
	@$(REPO_PY) tools/check_action_refs.py
	@echo "── Contract files present + wired ──"
	@$(REPO_PY) tools/verify_contracts.py
	@echo "── Contract violation scan ──"
	@$(REPO_PY) tools/contract_scanner.py
	@echo "── Payload contract compiler validator ──"
	@PYTHONPATH="$${PYTHONPATH:-}:." $(REPO_PY) tools/payload_contract_compiler.py --stdout-only
	@echo "── L9_META headers (C-18) ──"
	@$(REPO_PY) tools/l9_meta_injector.py check

repo-check:	## Non-mutating static gate: ruff check, ruff format --check, mypy engine/, cypher-lint (C-009)
	@echo "── Ruff lint ──"
	@$(REPO_PY) -m ruff check .
	@echo "── Ruff format (check only) ──"
	@$(REPO_PY) -m ruff format --check .
	@echo "── Type check ──"
	@$(REPO_PY) -m mypy engine/
	@echo "── Cypher lint (C-009) ──"
	@$(REPO_PY) tools/cypher_lint.py

repo-test:	## Unit suite (pytest -m unit): no Docker, no Neo4j
	@PYTHONPATH="$${PYTHONPATH:-}:." $(REPO_PY) -m pytest tests/ -m unit --tb=short -q

# ── Governance ─────────────────────────────────────────────

.PHONY: start

start:	## Run the full governance session-start pipeline against this repo
	@$(MAKE) -C "$(HOME)/.cursor-governance" start WS="$(CURDIR)"

# ── Docker Compose ─────────────────────────────────────────

dev:	## Start all services (detached)
	docker compose up -d

dev-build:	## Rebuild API image and start
	docker compose up -d --build

dev-down:	## Stop all services
	docker compose down

dev-logs:	## Tail all logs
	docker compose logs -f

dev-restart:	## Restart API only (fast iteration)
	docker compose restart api

# ── Health & Status ────────────────────────────────────────

health:	## Check all service health
	@echo "── API ──"
	@curl -sf http://localhost:8000/v1/health | python -m json.tool || echo "API: DOWN"
	@echo "── Neo4j ──"
	@docker exec l9-graph-neo4j cypher-shell -u neo4j -p l9-dev-password "RETURN 'ok'" 2>/dev/null || echo "Neo4j: DOWN"

# ── Testing ────────────────────────────────────────────────
# `make test` is the facade phase (repo-test above, unit suite, no Docker).

test-docker:	## Run full test suite inside the api container (the pre-V2 `make test`)
	docker compose exec api python -m pytest tests/ -v --tb=short

test-unit:	## Unit tests only (no Neo4j needed)
	docker compose exec api python -m pytest tests/unit/ -v --tb=short

test-integration:	## Integration tests (needs Neo4j)
	docker compose exec api python -m pytest tests/integration/ -v --tb=short

# ── Data Seeding ───────────────────────────────────────────

seed:	## Seed PlasticOS domain data into Neo4j
	docker compose exec api python -m engine.scripts.seed

# ── Shell Access ───────────────────────────────────────────

shell:	## Python shell inside API container
	docker compose exec api python

neo4j-shell:	## Cypher shell into Neo4j
	docker exec -it l9-graph-neo4j cypher-shell -u neo4j -p l9-dev-password

# ── Local Dev (API outside Docker, DBs in Docker) ─────────

local-dbs:	## Start only Neo4j + Postgres
	docker compose up -d neo4j postgres

local-api:	## Run API locally against Dockerized DBs (SDK chassis; alias of local-api-sdk)
	$(MAKE) local-api-sdk

local-api-legacy:	## Run the legacy dict chassis locally (permitted in L9_ENV=dev|local|test)
	NEO4J_URI=bolt://localhost:7687 \
	NEO4J_PASSWORD=l9-dev-password \
	LOG_LEVEL=debug \
	L9_LIFECYCLE_HOOK=engine.boot:GraphLifecycle \
	L9_CHASSIS=legacy \
	uvicorn chassis.entrypoint:create_app --factory --reload --port 8000

local-api-sdk:	## Run API locally on the SDK chassis (L9_CHASSIS=sdk)
	NEO4J_URI=bolt://localhost:7687 \
	NEO4J_PASSWORD=l9-dev-password \
	LOG_LEVEL=debug \
	L9_LIFECYCLE_HOOK=engine.boot:GraphLifecycle \
	L9_CHASSIS=sdk \
	L9_ENVIRONMENT=local \
	L9_SERVICE_NAME=graph-engine \
	HOST=0.0.0.0 \
	L9_ENFORCE_GATE_ONLY_INGRESS=false \
	L9_REQUIRE_SIGNATURE=false \
	L9_MAX_ATTACHMENTS=0 \
	L9_MAX_ATTACHMENT_SIZE_BYTES=0 \
	L9_ALLOWED_ACTIONS="$$(python3 -c 'from engine.handlers import ACTION_HANDLERS; print(",".join(a for a in ACTION_HANDLERS if a != "enrich"))')" \
	uvicorn chassis.entrypoint:create_app --factory --reload --port 8000

# ── Production ─────────────────────────────────────────────

.PHONY: prod prod-build prod-down prod-logs

prod:	## Start production stack
	docker compose -f docker-compose.prod.yml up -d

prod-build:	## Rebuild + start production stack
	docker compose -f docker-compose.prod.yml up -d --build

prod-down:	## Stop production stack
	docker compose -f docker-compose.prod.yml down

prod-logs:	## Tail production logs
	docker compose -f docker-compose.prod.yml logs -f

# ── VPS Deployment ─────────────────────────────────────────
# GitHub SSOT + env sync + rebuild + healthcheck, native (no wrapper script).
# Config comes from .env.vps at repo root (copy templates/.env.vps.template),
# or override inline: `make deploy VPS_HOST=1.2.3.4`.

.PHONY: deploy deploy-no-rebuild deploy-core deploy-services
.PHONY: deploy-push deploy-pull deploy-sync-env deploy-rebuild deploy-health
.PHONY: guard-vps-host guard-branch

-include .env.vps

VPS_HOST       ?=
VPS_USER       ?= root
VPS_REPO       ?= /opt/ceg
DEPLOY_BRANCH  ?= main
COMPOSE_FILE   ?= docker-compose.prod.yml
CORE_SERVICES  ?= api
ALLOW_NON_MAIN ?= false
SSH_OPTS       := -o BatchMode=yes -o StrictHostKeyChecking=accept-new
SSH_TARGET     := $(VPS_USER)@$(VPS_HOST)

guard-vps-host:	## Fail fast if VPS_HOST is not configured
	@test -n "$(VPS_HOST)" || (echo "❌ Set VPS_HOST (in .env.vps, or: make deploy VPS_HOST=1.2.3.4)"; exit 1)

guard-branch:	## Refuse to deploy from a non-$(DEPLOY_BRANCH) branch unless ALLOW_NON_MAIN=true
	@branch=$$(git rev-parse --abbrev-ref HEAD); \
	if [ "$$branch" != "$(DEPLOY_BRANCH)" ] && [ "$(ALLOW_NON_MAIN)" != "true" ]; then \
		echo "❌ Refusing deploy from '$$branch'. Expected '$(DEPLOY_BRANCH)' (or set ALLOW_NON_MAIN=true)."; \
		exit 1; \
	fi

deploy: guard-vps-host deploy-push deploy-pull deploy-sync-env deploy-rebuild deploy-health	## Full VPS deploy: push -> VPS git reset -> env sync -> full rebuild -> healthcheck

deploy-no-rebuild: guard-vps-host deploy-push deploy-pull deploy-sync-env deploy-health	## Deploy without rebuilding containers (git sync + env sync only)

deploy-core: guard-vps-host deploy-push deploy-pull deploy-sync-env	## Deploy + rebuild only $(CORE_SERVICES)
	$(MAKE) deploy-services SERVICES="$(CORE_SERVICES)"
	$(MAKE) deploy-health

deploy-push: guard-branch	## Stage, commit (if needed), and push the current branch to origin
	git add -A
	@git diff --cached --quiet && echo " = Nothing staged; skipping commit" || git commit --no-verify -m "deploy: $$(date +'%Y-%m-%d %H:%M:%S')"
	git push --no-verify origin HEAD

deploy-pull: guard-vps-host	## Hard-reset the VPS repo to origin/$(DEPLOY_BRANCH) (VPS mirrors GitHub exactly)
	ssh $(SSH_OPTS) $(SSH_TARGET) "cd $(VPS_REPO) && git fetch origin $(DEPLOY_BRANCH) && git reset --hard origin/$(DEPLOY_BRANCH) && git clean -fd"

deploy-sync-env: guard-vps-host	## Sync local .env.vps -> VPS .env (backs up remote first, verifies via sha256)
	@test -f .env.vps || (echo "❌ Missing .env.vps at repo root — copy templates/.env.vps.template and fill in real values."; exit 1)
	ssh $(SSH_OPTS) $(SSH_TARGET) "cd $(VPS_REPO) && (test -f .env && cp -a .env .env.bak.$$(date +%Y%m%d_%H%M%S) || true)"
	ssh $(SSH_OPTS) $(SSH_TARGET) "cat > $(VPS_REPO)/.env && chmod 600 $(VPS_REPO)/.env" < .env.vps
	@local_hash=$$(shasum -a 256 .env.vps | awk '{print $$1}'); \
	remote_hash=$$(ssh $(SSH_OPTS) $(SSH_TARGET) "shasum -a 256 $(VPS_REPO)/.env" | awk '{print $$1}'); \
	if [ "$$local_hash" != "$$remote_hash" ]; then echo "❌ Env sync mismatch ($$local_hash != $$remote_hash)"; exit 1; fi; \
	echo "✅ Env synced (sha256 match)"

deploy-rebuild: guard-vps-host	## Full rebuild on VPS: down -> build -> up -d (set NO_CACHE=1 for a clean build)
	ssh $(SSH_OPTS) $(SSH_TARGET) "cd $(VPS_REPO) && docker compose -f $(COMPOSE_FILE) down --remove-orphans"
	ssh $(SSH_OPTS) $(SSH_TARGET) "cd $(VPS_REPO) && docker compose -f $(COMPOSE_FILE) build $(if $(NO_CACHE),--no-cache,)"
	ssh $(SSH_OPTS) $(SSH_TARGET) "cd $(VPS_REPO) && docker compose -f $(COMPOSE_FILE) up -d --force-recreate --remove-orphans"

deploy-services: guard-vps-host	## Rebuild ONLY $(SERVICES) on VPS, e.g. make deploy-services SERVICES="api"
	@test -n "$(SERVICES)" || (echo "❌ Set SERVICES=\"svc1 svc2\""; exit 1)
	ssh $(SSH_OPTS) $(SSH_TARGET) "cd $(VPS_REPO) && docker compose -f $(COMPOSE_FILE) stop $(SERVICES)"
	ssh $(SSH_OPTS) $(SSH_TARGET) "cd $(VPS_REPO) && docker compose -f $(COMPOSE_FILE) build $(SERVICES)"
	ssh $(SSH_OPTS) $(SSH_TARGET) "cd $(VPS_REPO) && docker compose -f $(COMPOSE_FILE) up -d --force-recreate $(SERVICES)"

deploy-health: guard-vps-host	## Remote healthcheck over SSH (VPS ports may be firewalled externally, so check via localhost on the box)
	@echo "⏳ Waiting 15s for services to settle..."
	@sleep 15
	@echo "── API ──"
	@ssh $(SSH_OPTS) $(SSH_TARGET) "curl -sf http://localhost:8000/v1/health && echo" || echo "API: DOWN"
	@echo "── Neo4j ──"
	@ssh $(SSH_OPTS) $(SSH_TARGET) "curl -sf http://localhost:7474 >/dev/null" && echo "Neo4j: UP" || echo "Neo4j: DOWN"

# ── Cleanup ────────────────────────────────────────────────

clean:	## Remove volumes + containers
	docker compose down -v --remove-orphans

# ── Quality Gates (local, no Docker) ───────────────────────

.PHONY: lint lint-fix typecheck check-fix

lint:	## Ruff lint + format check (no mutation) + MyPy — matches CI's blocking gate
	ruff check .
	ruff format --check .
	mypy engine/

cypher-lint:	## C-009: scan generated Cypher for injection vectors
	python3 tools/cypher_lint.py

lint-fix:	## Autofix: ruff check --fix + ruff format . (run this when `make lint` fails)
	ruff check . --fix
	ruff format .

typecheck:	## MyPy type checking on engine/
	mypy engine/

# `make check` is the facade phase (repo-check above, non-mutating). This is
# the pre-V2 `make check`, unchanged except for its name.
check-fix:	## Full local quality gate (autofix lint + types + unit tests; the pre-V2 `make check`)
	@echo "── Lint (autofix) ──"
	@ruff check . --fix
	@ruff format .
	@echo "── Type Check ──"
	@mypy engine/
	@echo "── Unit Tests ──"
	@PYTHONPATH="$${PYTHONPATH}:." python3 -m pytest tests/ -m "unit" --tb=short -q
	@echo "── All checks passed ──"

# ── Agent Verification Gate ────────────────────────────────
# The single command an agent runs to prove a change is complete.
# Mirrors CI's blocking jobs (.github/workflows/contracts.yml) exactly, so a
# green `agent-check` means a green CI. Non-mutating by design — it verifies,
# it does not autofix. Run `make lint-fix` first if formatting fails.

.PHONY: agent-check agent-check-unit contracts-report

contracts-report:	## Contract-to-verification coverage table (scanner rules, tests, docs)
	@python3 tools/contract_report.py

agent-check:	## Agent completion gate: CI's blocking set + audit harness, run locally
	@echo "── [1/8] Action references ──"
	@python3 tools/check_action_refs.py
	@echo "── [2/8] Contract files present + wired ──"
	@python3 tools/verify_contracts.py
	@echo "── [3/8] Contract violation scan ──"
	@python3 tools/contract_scanner.py
	@echo "── [4/8] Payload contract compiler validator ──"
	@PYTHONPATH="$${PYTHONPATH}:." python3 tools/payload_contract_compiler.py --stdout-only
	@echo "── [5/8] Lint + format ──"
	@ruff check .
	@ruff format --check .
	@echo "── [6/8] Type check ──"
	@mypy engine/ --config-file=pyproject.toml --ignore-missing-imports --exclude chassis
	@echo "── [7/8] Tests ──"
	@PYTHONPATH="$${PYTHONPATH}:." python3 -m pytest tests/ --tb=short -q
	@echo "── [8/8] Contract verification coverage ──"
	@python3 tools/contract_report.py
	@echo ""
	@echo "── Audit harness ──"
	@python3 tools/audit_harness.py
	@echo ""
	@echo "✅ agent-check passed — CI's blocking gates should be green"

agent-check-unit:	## Local agent gate (skips Docker integration/perf): CI's blocking set + audit harness, run locally
	@echo "── [1/8] Action references ──"
	@python3 tools/check_action_refs.py
	@echo "── [2/8] Contract files present + wired ──"
	@python3 tools/verify_contracts.py
	@echo "── [3/8] Contract violation scan ──"
	@python3 tools/contract_scanner.py
	@echo "── [4/8] Payload contract compiler validator ──"
	@PYTHONPATH="$${PYTHONPATH}:." python3 tools/payload_contract_compiler.py --stdout-only
	@echo "── [5/8] Lint + format ──"
	@ruff check .
	@ruff format --check .
	@echo "── [6/8] Type check ──"
	@mypy engine/ --config-file=pyproject.toml --ignore-missing-imports --exclude chassis
	@echo "── [7/8] Tests ──"
	@PYTHONPATH="$${PYTHONPATH}:." python3 -m pytest tests/ -m "unit" --tb=short -q
	@echo "── [8/8] Contract verification coverage ──"
	@python3 tools/contract_report.py
	@echo ""
	@echo "── Audit harness ──"
	@python3 tools/audit_harness.py
	@echo ""
	@echo "✅ agent-check-unit passed — CI's blocking gates should be green"


# ── L9_TEMPLATE Audit Harness ─────────────────────────────

.PHONY: harness harness-strict audit audit-strict coverage

harness:                   ## Run full audit harness (recommended)
	python3 tools/audit_harness.py
	@echo "Reports in artifacts/"

harness-strict:            ## Audit harness + fail on MISSING spec features
	python3 tools/audit_harness.py --strict

harness-json:              ## Audit harness with JSON output (for CI)
	python3 tools/audit_harness.py --json

audit:                     ## Run architecture audit only (legacy)
	python3 tools/audit_engine.py
	@echo "Report: artifacts/audit_report.md"

audit-strict:              ## Architecture audit + spec coverage strict (legacy)
	python3 tools/audit_engine.py
	python3 tools/spec_extract.py --fail-on MISSING

coverage:                  ## Spec coverage matrix only
	python3 tools/spec_extract.py --fail-on NONE

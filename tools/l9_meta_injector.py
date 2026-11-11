#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 1
origin: l9-template
engine: graph
layer: [meta]
tags: [L9_TEMPLATE, meta, injector]
owner: platform
status: active
--- /L9_META ---
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# =============================================================================
# CONFIGURATION
# =============================================================================

ENGINE_ID = "graph"  # Override via --engine flag
L9_SCHEMA_VERSION = 1

# =============================================================================
# FILE REGISTRY — Single source of truth for every file's meta values
# =============================================================================


@dataclass
class FileMeta:
    path: str
    origin: str  # l9-template | engine-specific | domain-specific
    layer: list[str]
    tags: list[str]
    owner: str  # platform | engine-team | domain-team | <handle>
    status: str = "active"


FILE_REGISTRY: list[FileMeta] = [
    # =========================================================================
    # TEMPLATE FILES  (origin: l9-template, owner: platform)
    # =========================================================================
    # --- Agent Rules ---
    FileMeta(".cursorrules", "l9-template", ["agent-rules"], ["L9_TEMPLATE", "agent-rules", "cursor"], "platform"),
    FileMeta("CLAUDE.md", "l9-template", ["agent-rules"], ["L9_TEMPLATE", "agent-rules", "claude"], "platform"),
    FileMeta(
        ".github/copilot-instructions.md",
        "l9-template",
        ["agent-rules"],
        ["L9_TEMPLATE", "agent-rules", "copilot"],
        "platform",
    ),
    # --- Audit Tools ---
    FileMeta("tools/audit_engine.py", "l9-template", ["audit"], ["L9_TEMPLATE", "audit", "compliance"], "platform"),
    FileMeta("tools/audit_rules.yaml", "l9-template", ["audit"], ["L9_TEMPLATE", "audit-rules"], "platform"),
    FileMeta("tools/spec_extract.py", "l9-template", ["audit"], ["L9_TEMPLATE", "audit", "spec-coverage"], "platform"),
    FileMeta(
        "tools/l9_template_manifest.yaml", "l9-template", ["meta"], ["L9_TEMPLATE", "meta", "manifest"], "platform"
    ),
    FileMeta("tools/l9_meta_injector.py", "l9-template", ["meta"], ["L9_TEMPLATE", "meta", "injector"], "platform"),
    # --- CI/CD Workflows ---
    FileMeta(".github/workflows/audit.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "audit"], "platform"),
    FileMeta(".github/workflows/ci.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "pipeline"], "platform"),
    FileMeta(
        ".github/workflows/compliance.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "compliance"], "platform"
    ),
    FileMeta(".github/workflows/docker-build.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "docker"], "platform"),
    FileMeta(".github/workflows/k8s-deploy.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "deploy"], "platform"),
    FileMeta(
        ".github/workflows/supply-chain.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "security"], "platform"
    ),
    FileMeta(".github/workflows/codeql.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "security"], "platform"),
    FileMeta(
        ".github/workflows/pr-review-enforcement.yml",
        "l9-template",
        ["ci", "governance"],
        ["L9_TEMPLATE", "ci", "governance"],
        "platform",
    ),
    FileMeta(
        ".github/workflows/terminology-guard.yml",
        "l9-template",
        ["ci"],
        ["L9_TEMPLATE", "ci", "terminology"],
        "platform",
    ),
    FileMeta(
        ".github/workflows/release-drafter.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "release"], "platform"
    ),
    FileMeta(
        ".github/workflows/docs-code-sync.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "docs-sync"], "platform"
    ),
    FileMeta(
        ".github/workflows/refactoring-validation.yml",
        "l9-template",
        ["ci"],
        ["L9_TEMPLATE", "ci", "refactoring"],
        "platform",
    ),
    FileMeta(
        ".github/workflows/dev-layer-gmp.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "dev-layer"], "platform"
    ),
    # --- Governance ---
    FileMeta(
        ".github/pr_review_config.yaml",
        "l9-template",
        ["governance"],
        ["L9_TEMPLATE", "governance", "pr-review"],
        "platform",
    ),
    FileMeta(
        ".github/scripts/setup-repo-vars.sh",
        "l9-template",
        ["bootstrap"],
        ["L9_TEMPLATE", "bootstrap", "setup"],
        "platform",
    ),
    FileMeta(".pre-commit-config.yaml", "l9-template", ["governance"], ["L9_TEMPLATE", "precommit"], "platform"),
    FileMeta(".coderabbit.yaml", "l9-template", ["governance"], ["L9_TEMPLATE", "pr-review"], "platform"),
    # --- Config / Bootstrap ---
    FileMeta(".env.template", "l9-template", ["config"], ["L9_TEMPLATE", "config", "env"], "platform"),
    FileMeta(".github/env.template", "l9-template", ["config"], ["L9_TEMPLATE", "config", "env"], "platform"),
    # --- Docker / Build ---
    FileMeta("Dockerfile", "l9-template", ["docker"], ["L9_TEMPLATE", "docker", "dev"], "platform"),
    FileMeta("Dockerfile.prod", "l9-template", ["docker"], ["L9_TEMPLATE", "docker", "prod"], "platform"),
    FileMeta("docker-compose.yml", "l9-template", ["docker"], ["L9_TEMPLATE", "docker", "dev"], "platform"),
    FileMeta("docker-compose.prod.yml", "l9-template", ["docker"], ["L9_TEMPLATE", "docker", "prod"], "platform"),
    # entrypoint.sh at root doesn't exist - use scripts/entrypoint.sh or chassis/entrypoint.sh
    # The root Makefile is the Core make-v1 facade and is EXCLUDED (see
    # EXCLUDED_PATTERNS); the repository recipes and this header moved to Repo.mk.
    FileMeta("Repo.mk", "l9-template", ["build"], ["L9_TEMPLATE", "build", "commands"], "platform"),
    # --- Telemetry ---
    FileMeta(
        "telemetry/dashboards/dashboards-docker-compose.monitoring.yml",
        "l9-template",
        ["telemetry"],
        ["L9_TEMPLATE", "telemetry", "docker"],
        "platform",
    ),
    FileMeta(
        "telemetry/dashboards/dashboards-grafana-overview.json",
        "l9-template",
        ["telemetry"],
        ["L9_TEMPLATE", "telemetry", "grafana"],
        "platform",
    ),
    FileMeta(
        "telemetry/dashboards/dashboards-grafana-api.json",
        "l9-template",
        ["telemetry"],
        ["L9_TEMPLATE", "telemetry", "grafana"],
        "platform",
    ),
    FileMeta(
        "telemetry/dashboards/dashboards-grafana-neo4j.json",
        "l9-template",
        ["telemetry"],
        ["L9_TEMPLATE", "telemetry", "grafana"],
        "platform",
    ),
    FileMeta(
        "telemetry/dashboards/dashboards-grafana-provisioning-dashboards.yml",
        "l9-template",
        ["telemetry"],
        ["L9_TEMPLATE", "telemetry", "grafana"],
        "platform",
    ),
    FileMeta(
        "telemetry/dashboards/dashboards-grafana-provisioning-datasources.yml",
        "l9-template",
        ["telemetry"],
        ["L9_TEMPLATE", "telemetry", "grafana"],
        "platform",
    ),
    FileMeta(
        "telemetry/dashboards/dashboards-prometheus.yml",
        "l9-template",
        ["telemetry"],
        ["L9_TEMPLATE", "telemetry", "prometheus"],
        "platform",
    ),
    # --- Scripts ---
    FileMeta("scripts/scripts-build.sh", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "build"], "platform"),
    FileMeta("scripts/scripts-dev.sh", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "dev"], "platform"),
    FileMeta("scripts/scripts-health.sh", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "health"], "platform"),
    FileMeta(
        "scripts/scripts-migrate.sh", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "migration"], "platform"
    ),
    FileMeta("scripts/scripts-seed.sh", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "seed"], "platform"),
    FileMeta("scripts/scripts-setup.sh", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "setup"], "platform"),
    FileMeta("scripts/scripts-test.sh", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "test"], "platform"),
    FileMeta(
        "scripts/scripts-gds-trigger.sh", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "gds"], "platform"
    ),
    FileMeta("scripts/entrypoint.sh", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "entrypoint"], "platform"),
    FileMeta("scripts/audit.sh", "l9-template", ["scripts", "audit"], ["L9_TEMPLATE", "scripts", "audit"], "platform"),
    FileMeta("scripts/README.md", "l9-template", ["scripts"], ["L9_TEMPLATE", "scripts", "docs"], "platform"),
    # --- Test Harness ---
    FileMeta("tests/conftest.py", "l9-template", ["test"], ["L9_TEMPLATE", "test", "fixtures"], "platform"),
    FileMeta("tests/README.md", "l9-template", ["test"], ["L9_TEMPLATE", "test", "docs"], "platform"),
    # --- Tests: domain-neutrality verification inputs (Stage 2) ---
    FileMeta(
        "tests/fixtures/domain_contracts.py",
        "engine-specific",
        ["test"],
        ["test", "fixtures", "domain-neutrality", "payload-contracts"],
        "engine-team",
    ),
    FileMeta(
        "tests/fixtures/domains/orbital-logistics/spec.yaml",
        "engine-specific",
        ["test"],
        ["test", "fixtures", "domain-neutrality", "synthetic"],
        "engine-team",
    ),
    FileMeta(
        "tests/fixtures/domains/cheese-aging/spec.yaml",
        "engine-specific",
        ["test"],
        ["test", "fixtures", "domain-neutrality", "synthetic"],
        "engine-team",
    ),
    # --- Tests: Compliance ---
    FileMeta(
        "tests/compliance/test_audit.py", "engine-specific", ["test"], ["test", "compliance", "audit"], "engine-team"
    ),
    FileMeta(
        "tests/compliance/test_hipaa.py", "engine-specific", ["test"], ["test", "compliance", "hipaa"], "engine-team"
    ),
    FileMeta(
        "tests/compliance/test_prohibited_factors.py",
        "engine-specific",
        ["test"],
        ["test", "compliance", "prohibited-factors"],
        "engine-team",
    ),
    # --- Tests: Integration ---
    FileMeta(
        "tests/integration/test_multi_tenant.py",
        "engine-specific",
        ["test"],
        ["test", "integration", "multi-tenant"],
        "engine-team",
    ),
    FileMeta(
        "tests/integration/test_null_semantics.py",
        "engine-specific",
        ["test"],
        ["test", "integration", "null-semantics"],
        "engine-team",
    ),
    # --- Tests: Performance ---
    FileMeta(
        "tests/performance/test_query_latency.py",
        "engine-specific",
        ["test"],
        ["test", "performance", "latency"],
        "engine-team",
    ),
    FileMeta(
        "tests/performance/test_sync_throughput.py",
        "engine-specific",
        ["test"],
        ["test", "performance", "throughput"],
        "engine-team",
    ),
    # --- Tests: Root Level ---
    FileMeta("tests/test_compliance_engine.py", "engine-specific", ["test"], ["test", "compliance"], "engine-team"),
    FileMeta("tests/test_config_loader.py", "engine-specific", ["test"], ["test", "config"], "engine-team"),
    FileMeta("tests/test_enrich.py", "engine-specific", ["test"], ["test", "enrich"], "engine-team"),
    FileMeta("tests/test_handlers.py", "engine-specific", ["test"], ["test", "handlers"], "engine-team"),
    FileMeta("tests/test_scoring_extended.py", "engine-specific", ["test"], ["test", "scoring"], "engine-team"),
    # --- Tests: Unit ---
    FileMeta(
        "tests/unit/test_api_error_handling.py", "engine-specific", ["test"], ["test", "unit", "api"], "engine-team"
    ),
    FileMeta("tests/unit/test_chassis_app.py", "engine-specific", ["test"], ["test", "unit", "chassis"], "engine-team"),
    FileMeta(
        "tests/unit/test_chassis_contract.py", "engine-specific", ["test"], ["test", "unit", "chassis"], "engine-team"
    ),
    FileMeta("tests/unit/test_config.py", "engine-specific", ["test"], ["test", "unit", "config"], "engine-team"),
    FileMeta(
        "tests/unit/test_config_schema.py", "engine-specific", ["test"], ["test", "unit", "config"], "engine-team"
    ),
    FileMeta(
        "tests/unit/test_domain_pack_loader.py", "engine-specific", ["test"], ["test", "unit", "config"], "engine-team"
    ),
    FileMeta(
        "tests/unit/test_gates_all_types.py", "engine-specific", ["test"], ["test", "unit", "gates"], "engine-team"
    ),
    FileMeta("tests/unit/test_gds_scheduler.py", "engine-specific", ["test"], ["test", "unit", "gds"], "engine-team"),
    FileMeta(
        "tests/unit/test_domain_neutrality_acceptance.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "domain-neutrality", "acceptance"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_handlers_enrich_health.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "handlers"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_handlers_extended.py", "engine-specific", ["test"], ["test", "unit", "handlers"], "engine-team"
    ),
    FileMeta(
        "tests/unit/test_capability_tenant_binding.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "capability", "auth"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_feedback_convergence_closure.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "feedback"],
        "engine-team",
    ),
    FileMeta("tests/unit/test_kge_beam_search.py", "engine-specific", ["test"], ["test", "unit", "kge"], "engine-team"),
    FileMeta(
        "tests/unit/test_kge_compound_e3d.py", "engine-specific", ["test"], ["test", "unit", "kge"], "engine-team"
    ),
    FileMeta("tests/unit/test_kge_ensemble.py", "engine-specific", ["test"], ["test", "unit", "kge"], "engine-team"),
    FileMeta(
        "tests/unit/test_kge_transformations.py", "engine-specific", ["test"], ["test", "unit", "kge"], "engine-team"
    ),
    FileMeta(
        "tests/unit/test_packet_envelope.py", "engine-specific", ["test"], ["test", "unit", "packet"], "engine-team"
    ),
    FileMeta("tests/unit/test_safe_eval.py", "engine-specific", ["test"], ["test", "unit", "utils"], "engine-team"),
    FileMeta("tests/unit/test_scoring.py", "engine-specific", ["test"], ["test", "unit", "scoring"], "engine-team"),
    FileMeta(
        "tests/unit/test_scoring_admission_default_off.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "config", "admission"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_scoring_security.py", "engine-specific", ["test"], ["test", "unit", "scoring"], "engine-team"
    ),
    FileMeta("tests/unit/test_settings.py", "engine-specific", ["test"], ["test", "unit", "config"], "engine-team"),
    FileMeta("tests/unit/test_sync.py", "engine-specific", ["test"], ["test", "unit", "sync"], "engine-team"),
    FileMeta("tests/unit/test_traversal.py", "engine-specific", ["test"], ["test", "unit", "traversal"], "engine-team"),
    FileMeta("tests/unit/test_units.py", "engine-specific", ["test"], ["test", "unit", "units"], "engine-team"),
    # =========================================================================
    # ENGINE-SPECIFIC FILES  (origin: engine-specific, owner: engine-team)
    # =========================================================================
    FileMeta("engine/__init__.py", "engine-specific", ["config"], ["engine-core"], "engine-team"),
    FileMeta("engine/handlers.py", "engine-specific", ["config"], ["chassis-bridge", "handlers"], "engine-team"),
    FileMeta("engine/config/__init__.py", "engine-specific", ["config"], ["config"], "engine-team"),
    FileMeta("engine/config/loader.py", "engine-specific", ["config"], ["config", "domain-loader"], "engine-team"),
    FileMeta(
        "engine/config/schema.py", "engine-specific", ["config"], ["config", "pydantic", "domain-spec"], "engine-team"
    ),
    FileMeta("engine/config/settings.py", "engine-specific", ["config"], ["config", "settings"], "engine-team"),
    FileMeta("engine/config/units.py", "engine-specific", ["config"], ["config", "units"], "engine-team"),
    FileMeta("engine/gates/__init__.py", "engine-specific", ["config"], ["gates"], "engine-team"),
    FileMeta("engine/gates/compiler.py", "engine-specific", ["config"], ["gates", "compiler", "cypher"], "engine-team"),
    FileMeta(
        "engine/gates/null_semantics.py", "engine-specific", ["config"], ["gates", "null-semantics"], "engine-team"
    ),
    FileMeta("engine/gates/registry.py", "engine-specific", ["config"], ["gates", "registry"], "engine-team"),
    FileMeta("engine/gates/types/__init__.py", "engine-specific", ["config"], ["gates", "types"], "engine-team"),
    FileMeta(
        "engine/gates/types/all_gates.py",
        "engine-specific",
        ["config"],
        ["gates", "types", "implementation"],
        "engine-team",
    ),
    FileMeta("engine/scoring/__init__.py", "engine-specific", ["config"], ["scoring"], "engine-team"),
    FileMeta(
        "engine/scoring/assembler.py", "engine-specific", ["config"], ["scoring", "assembler", "cypher"], "engine-team"
    ),
    FileMeta("engine/traversal/__init__.py", "engine-specific", ["config"], ["traversal"], "engine-team"),
    FileMeta(
        "engine/traversal/assembler.py",
        "engine-specific",
        ["config"],
        ["traversal", "assembler", "cypher"],
        "engine-team",
    ),
    FileMeta("engine/traversal/resolver.py", "engine-specific", ["config"], ["traversal", "resolver"], "engine-team"),
    FileMeta("engine/sync/__init__.py", "engine-specific", ["config"], ["sync"], "engine-team"),
    FileMeta("engine/sync/generator.py", "engine-specific", ["config"], ["sync", "cypher", "merge"], "engine-team"),
    FileMeta("engine/gds/__init__.py", "engine-specific", ["config"], ["gds"], "engine-team"),
    FileMeta("engine/gds/scheduler.py", "engine-specific", ["config"], ["gds", "scheduler", "louvain"], "engine-team"),
    FileMeta("engine/graph/__init__.py", "engine-specific", ["config"], ["graph", "driver"], "engine-team"),
    FileMeta("engine/graph/driver.py", "engine-specific", ["config"], ["graph", "driver", "neo4j"], "engine-team"),
    FileMeta("engine/compliance/__init__.py", "engine-specific", ["config"], ["compliance"], "engine-team"),
    FileMeta("engine/compliance/audit.py", "engine-specific", ["config"], ["compliance", "audit"], "engine-team"),
    FileMeta("engine/compliance/pii.py", "engine-specific", ["config"], ["compliance", "pii"], "engine-team"),
    FileMeta(
        "engine/compliance/prohibited_factors.py",
        "engine-specific",
        ["config"],
        ["compliance", "prohibited-factors"],
        "engine-team",
    ),
    FileMeta("engine/packet/__init__.py", "engine-specific", ["config"], ["packet"], "engine-team"),
    FileMeta(
        "engine/packet/chassis_contract.py", "engine-specific", ["config"], ["packet", "chassis-bridge"], "engine-team"
    ),
    FileMeta("engine/utils/__init__.py", "engine-specific", ["config"], ["utils"], "engine-team"),
    FileMeta("engine/utils/safe_eval.py", "engine-specific", ["config"], ["utils", "safe-eval"], "engine-team"),
    FileMeta(
        "engine/utils/security.py", "engine-specific", ["config"], ["utils", "security", "sanitize"], "engine-team"
    ),
    FileMeta("engine/boot.py", "engine-specific", ["config"], ["engine", "boot", "lifecycle"], "engine-team"),
    # --- Engine KGE (Knowledge Graph Embeddings) ---
    FileMeta("engine/kge/__init__.py", "engine-specific", ["kge"], ["kge", "embeddings"], "engine-team"),
    FileMeta(
        "engine/kge/beam_search.py",
        "engine-specific",
        ["kge"],
        ["kge", "beam-search", "link-prediction"],
        "engine-team",
    ),
    FileMeta("engine/kge/compound_e3d.py", "engine-specific", ["kge"], ["kge", "compound-e3d", "model"], "engine-team"),
    FileMeta("engine/kge/ensemble.py", "engine-specific", ["kge"], ["kge", "ensemble", "aggregation"], "engine-team"),
    FileMeta("engine/kge/transformations.py", "engine-specific", ["kge"], ["kge", "transformations"], "engine-team"),
    # --- Engine Compliance (additional) ---
    FileMeta("engine/compliance/engine.py", "engine-specific", ["compliance"], ["compliance", "engine"], "engine-team"),
    # --- Engine Security ---
    FileMeta(
        "engine/security/P2_9_llm_schemas.py",
        "engine-specific",
        ["security"],
        ["security", "llm", "schemas"],
        "engine-team",
    ),
    FileMeta(
        "engine/security/5_prompt_injection_semgrep.yaml",
        "engine-specific",
        ["security"],
        ["security", "semgrep", "prompt-injection"],
        "engine-team",
    ),
    # --- Tools (additional) ---
    FileMeta("tools/audit_dispatch.py", "l9-template", ["audit"], ["L9_TEMPLATE", "audit", "dispatch"], "platform"),
    FileMeta("tools/contract_scanner.py", "l9-template", ["audit"], ["L9_TEMPLATE", "audit", "contracts"], "platform"),
    FileMeta("tools/verify_contracts.py", "l9-template", ["audit"], ["L9_TEMPLATE", "audit", "verify"], "platform"),
    FileMeta("tools/auditors/__init__.py", "l9-template", ["audit"], ["L9_TEMPLATE", "auditors"], "platform"),
    FileMeta("tools/auditors/base.py", "l9-template", ["audit"], ["L9_TEMPLATE", "auditors", "base"], "platform"),
    FileMeta(
        "tools/auditors/api_regression.py", "l9-template", ["audit"], ["L9_TEMPLATE", "auditors", "api"], "platform"
    ),
    FileMeta(
        "tools/auditors/log_safety.py", "l9-template", ["audit"], ["L9_TEMPLATE", "auditors", "logging"], "platform"
    ),
    FileMeta(
        "tools/auditors/query_performance.py",
        "l9-template",
        ["audit"],
        ["L9_TEMPLATE", "auditors", "performance"],
        "platform",
    ),
    FileMeta(
        "tools/auditors/test_quality.py",
        "l9-template",
        ["audit"],
        ["L9_TEMPLATE", "auditors", "test-quality"],
        "platform",
    ),
    # --- Tools Shell Scripts ---
    FileMeta("tools/dev/dev_up.sh", "l9-template", ["scripts", "dev"], ["L9_TEMPLATE", "dev"], "platform"),
    FileMeta(
        "tools/hooks/install_hooks.sh", "l9-template", ["scripts", "hooks"], ["L9_TEMPLATE", "git-hooks"], "platform"
    ),
    FileMeta(
        "tools/infra/check_env.sh",
        "l9-template",
        ["scripts", "infra"],
        ["L9_TEMPLATE", "infra", "env-check"],
        "platform",
    ),
    FileMeta(
        "tools/infra/deep_mri.sh",
        "l9-template",
        ["scripts", "infra"],
        ["L9_TEMPLATE", "infra", "diagnostics"],
        "platform",
    ),
    FileMeta(
        "tools/infra/docker_validate.sh",
        "l9-template",
        ["scripts", "infra"],
        ["L9_TEMPLATE", "infra", "docker"],
        "platform",
    ),
    FileMeta(
        "tools/infra/precommit_smoke.sh",
        "l9-template",
        ["scripts", "infra"],
        ["L9_TEMPLATE", "infra", "precommit"],
        "platform",
    ),
    FileMeta(
        "tools/infra/test_everything.sh",
        "l9-template",
        ["scripts", "infra"],
        ["L9_TEMPLATE", "infra", "test"],
        "platform",
    ),
    # --- Core Markdown Docs (root) ---
    FileMeta("README.md", "engine-specific", ["docs"], ["readme", "overview"], "engine-team"),
    FileMeta("CHANGELOG.md", "engine-specific", ["docs"], ["changelog"], "engine-team"),
    FileMeta("ROADMAP.md", "engine-specific", ["docs"], ["roadmap", "planning"], "engine-team"),
    FileMeta("TODO.md", "engine-specific", ["docs"], ["todo", "planning"], "engine-team"),
    FileMeta("Readme-Requirements.md", "engine-specific", ["docs"], ["requirements"], "engine-team"),
    FileMeta("workflow_state.md", "engine-specific", ["docs"], ["workflow", "state"], "engine-team"),
    FileMeta(".gitleaks.toml", "l9-template", ["security"], ["L9_TEMPLATE", "gitleaks", "secrets"], "platform"),
    # --- docs/ folder ---
    FileMeta("docs/ARCHITECTURE.md", "engine-specific", ["docs"], ["architecture", "design"], "engine-team"),
    FileMeta("docs/ACTION ITEMS.md", "engine-specific", ["docs"], ["action-items"], "engine-team"),
    FileMeta("docs/Audit Harness-Explained.md", "engine-specific", ["docs"], ["audit", "harness"], "engine-team"),
    FileMeta("docs/What the Audit Harness Does.md", "engine-specific", ["docs"], ["audit", "harness"], "engine-team"),
    FileMeta("docs/GRAPH-architecture.md", "engine-specific", ["docs"], ["architecture", "graph"], "engine-team"),
    FileMeta(
        "docs/L9_Contract_Enforcement_System.md",
        "engine-specific",
        ["docs"],
        ["contracts", "enforcement"],
        "engine-team",
    ),
    FileMeta(
        "docs/archive/plasticos/PlasticOS Graph Cognitive Engine.yaml",
        "engine-specific",
        ["docs"],
        ["plasticos", "spec"],
        "engine-team",
    ),
    FileMeta(
        "docs/archive/plasticos/plasticos_domain_spec_v0.3.0.yaml",
        "domain-specific",
        ["docs"],
        ["plasticos", "spec", "v0.3"],
        "domain-team",
    ),
    FileMeta(
        "docs/archive/plasticos/plasticos_domain_spec_v0.4.yaml",
        "domain-specific",
        ["docs"],
        ["plasticos", "spec", "v0.4"],
        "domain-team",
    ),
    # L9_Platform_Architecture.md and L9_AI_Constellation_Infrastructure_Reference.md
    # are referenced in CLAUDE.md but live in the L9 platform repo, not here
    # --- tools/research (spec-coverage input, consumed by tools/spec_extract.py) ---
    FileMeta(
        "tools/research/cognitive-engine-revenue-patterns.md",
        "engine-specific",
        ["audit"],
        ["research", "revenue", "spec-coverage"],
        "engine-team",
    ),
    FileMeta(
        "tools/research/top3_graph_pattern_analysis.json",
        "engine-specific",
        ["audit"],
        ["research", "analysis"],
        "engine-team",
    ),
    FileMeta(
        "tools/research/top5_leverage_patterns_detailed.json",
        "engine-specific",
        ["audit"],
        ["research", "analysis", "spec-coverage"],
        "engine-team",
    ),
    FileMeta(
        "tools/research/universal_graph_schema.json",
        "engine-specific",
        ["audit"],
        ["research", "schema"],
        "engine-team",
    ),
    # --- tools/auditors/remediation (audit fix runbooks, wired via Auditor.remediation_doc) ---
    FileMeta(
        "tools/auditors/remediation/api_regression.md",
        "engine-specific",
        ["tools"],
        ["remediation", "api"],
        "engine-team",
    ),
    FileMeta(
        "tools/auditors/remediation/audit_finding.md",
        "engine-specific",
        ["tools"],
        ["remediation", "audit"],
        "engine-team",
    ),
    FileMeta(
        "tools/auditors/remediation/log_safety.md",
        "engine-specific",
        ["tools"],
        ["remediation", "logging"],
        "engine-team",
    ),
    FileMeta(
        "tools/auditors/remediation/query_performance.md",
        "engine-specific",
        ["tools"],
        ["remediation", "performance"],
        "engine-team",
    ),
    FileMeta(
        "tools/auditors/remediation/test_quality.md",
        "engine-specific",
        ["tools"],
        ["remediation", "testing"],
        "engine-team",
    ),
    # --- docs/misc ---
    FileMeta(
        "docs/archive/plasticos/plasticos_domain_spec_changes.md",
        "domain-specific",
        ["docs"],
        ["plasticos", "changelog"],
        "domain-team",
    ),
    # --- docs/contracts ---
    FileMeta(
        "docs/contracts/FIELD_NAMES.md", "l9-template", ["docs", "contracts"], ["L9_TEMPLATE", "contracts"], "platform"
    ),
    FileMeta(
        "docs/contracts/METHOD_SIGNATURES.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/CYPHER_SAFETY.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts", "cypher"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/BANNED_PATTERNS.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/ERROR_HANDLING.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/HANDLER_PAYLOADS.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/PYDANTIC_YAML_MAPPING.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/DEPENDENCY_INJECTION.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/TEST_PATTERNS.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/RETURN_VALUES.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/PACKET_ENVELOPE_FIELDS.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts", "packet"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/DELEGATION_PROTOCOL.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/PACKET_TYPE_REGISTRY.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts", "packet"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/DOMAIN_SPEC_VERSIONING.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/FEEDBACK_LOOPS.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/NODE_REGISTRATION.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/ENV_VARS.md", "l9-template", ["docs", "contracts"], ["L9_TEMPLATE", "contracts"], "platform"
    ),
    FileMeta(
        "docs/contracts/OBSERVABILITY.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/MEMORY_SUBSTRATE_ACCESS.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    FileMeta(
        "docs/contracts/SHARED_MODELS.md",
        "l9-template",
        ["docs", "contracts"],
        ["L9_TEMPLATE", "contracts"],
        "platform",
    ),
    # --- engine/packet ---
    FileMeta(
        "engine/packet/README-Packet Envelope.md", "engine-specific", ["docs"], ["packet", "envelope"], "engine-team"
    ),
    # --- reports ---
    FileMeta(
        "reports/GAP-ANALYSIS-Audit-Harness-and-Spec-Coverage.md",
        "engine-specific",
        ["reports"],
        ["gap-analysis"],
        "engine-team",
    ),
    FileMeta(
        "reports/GMP-Report-130-Contract-Enforcement-System.md",
        "engine-specific",
        ["reports"],
        ["gmp-report"],
        "engine-team",
    ),
    # --- templates ---
    FileMeta(
        "templates/styleguide.template.md", "l9-template", ["templates"], ["L9_TEMPLATE", "styleguide"], "platform"
    ),
    # --- Additional YAML configs ---
    FileMeta(".github/workflows/ci-quality.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "quality"], "platform"),
    FileMeta(".github/workflows/contracts.yml", "l9-template", ["ci"], ["L9_TEMPLATE", "ci", "contracts"], "platform"),
    FileMeta(
        ".semgrep/semgrep-rules.yaml", "l9-template", ["security"], ["L9_TEMPLATE", "semgrep", "rules"], "platform"
    ),
    FileMeta(
        ".semgrep/fixtures/semgrep-rules.py",
        "engine-specific",
        ["test"],
        ["semgrep", "rules", "fixtures"],
        "platform",
    ),
    FileMeta("domains/_template.yaml", "domain-specific", ["config"], ["domains", "template"], "domain-team"),
    FileMeta(
        "docs/archive/plasticos/plasticos_domain_spec.yaml",
        "domain-specific",
        ["config"],
        ["domains", "plasticos"],
        "domain-team",
    ),
    FileMeta("domains/plasticos/spec.yaml", "domain-specific", ["config"], ["domains", "plasticos"], "domain-team"),
    FileMeta("chassis/__init__.py", "chassis", ["api"], ["chassis"], "platform-team"),
    FileMeta("chassis/actions.py", "chassis", ["api"], ["chassis", "actions"], "platform-team"),
    FileMeta("chassis/audit.py", "chassis", ["api"], ["chassis", "audit"], "platform-team"),
    FileMeta("chassis/chassis_app.py", "chassis", ["api"], ["chassis", "fastapi", "single-ingress"], "platform-team"),
    FileMeta("chassis/config.py", "chassis", ["api", "config"], ["chassis", "settings"], "platform-team"),
    FileMeta("chassis/errors.py", "chassis", ["api"], ["chassis", "errors", "exceptions"], "platform-team"),
    FileMeta("chassis/health.py", "chassis", ["api"], ["chassis", "health", "readiness"], "platform-team"),
    FileMeta("chassis/middleware.py", "chassis", ["api"], ["chassis", "middleware", "tenant"], "platform-team"),
    FileMeta("chassis/pii.py", "chassis", ["api", "compliance"], ["chassis", "pii", "redaction"], "platform-team"),
    # --- Chassis Auth Submodule ---
    FileMeta("chassis/auth/auth.py", "chassis", ["api", "auth"], ["chassis", "auth", "middleware"], "platform-team"),
    FileMeta(
        "chassis/auth/generate_l9_api_key.py",
        "chassis",
        ["api", "auth"],
        ["chassis", "auth", "keygen"],
        "platform-team",
    ),
    FileMeta(
        "chassis/auth/settings.py",
        "chassis",
        ["api", "auth", "config"],
        ["chassis", "auth", "settings"],
        "platform-team",
    ),
    FileMeta(
        "tests/unit/test_chassis_auth_middleware.py",
        "chassis",
        ["api", "auth", "test"],
        ["chassis", "auth", "test"],
        "platform-team",
    ),
    FileMeta("pyproject.toml", "engine-specific", ["build", "config"], ["build", "dependencies", "uv"], "engine-team"),
    FileMeta(
        "graph-cognitive-engine-spec-v1.1.0.yaml", "engine-specific", ["config"], ["spec", "engine-spec"], "engine-team"
    ),
    # =========================================================================
    # DOMAIN-SPECIFIC FILES  (origin: domain-specific, owner: domain-team)
    # =========================================================================
    FileMeta("domains/README.md", "domain-specific", ["config"], ["domains", "docs"], "domain-team"),
    FileMeta("domains/TESTING_GUIDE.md", "domain-specific", ["test"], ["domains", "testing", "guide"], "domain-team"),
    # =========================================================================
    # RE-HEADERED FROM SCHEMA v2 (RU-131, DEC-49 option B: v1 is the only schema)
    # Values are the file's v2 header values plus the v1 `owner` field;
    # docs/FEATURE_GATES.md keeps the values of the v1 block it already carried.
    # =========================================================================
    FileMeta("AGENTS.md", "engine-specific", ["agent-rules"], ["governance"], "engine-team"),
    FileMeta("agents/cursor/cursor_workflow_kernel.yaml", "l9-template", ["agent-rules"], ["governance"], "platform"),
    FileMeta("chassis/entrypoint.py", "chassis", ["api"], ["platform", "chassis"], "platform-team"),
    FileMeta("chassis/handler_registration.py", "chassis", ["api"], ["platform", "chassis"], "platform-team"),
    FileMeta("chassis/node_app.py", "chassis", ["api"], ["platform", "chassis"], "platform-team"),
    FileMeta("contracts/contract_18.yaml", "l9-template", ["contracts"], ["governance", "compliance"], "platform"),
    FileMeta(
        "docs/FEATURE_GATES.md", "engine-specific", ["docs"], ["feature-gates", "activation", "runbook"], "engine-team"
    ),
    FileMeta(
        "docs/remediation/CEG_CORRECTNESS_0d84ec8_LEDGER.md",
        "engine-specific",
        ["docs"],
        ["remediation", "audit", "ledger"],
        "engine-team",
    ),
    FileMeta(
        "docs/remediation/VERIFICATION.md",
        "engine-specific",
        ["docs"],
        ["remediation", "audit", "verification"],
        "engine-team",
    ),
    FileMeta(
        "tests/contracts/test_chassis_parity.py", "engine-specific", ["test"], ["platform", "chassis"], "engine-team"
    ),
    FileMeta("tests/contracts/test_contracts.py", "l9-template", ["test"], ["governance", "compliance"], "platform"),
    FileMeta(
        "tests/integration/test_outcomes_causal_integrity.py",
        "engine-specific",
        ["test"],
        ["causal", "outcomes", "integration"],
        "engine-team",
    ),
    FileMeta("tests/unit/test_node_app.py", "engine-specific", ["test"], ["platform", "chassis"], "engine-team"),
    FileMeta(
        "tests/unit/test_outcomes_causal_rejection.py",
        "engine-specific",
        ["test"],
        ["causal", "outcomes", "handlers"],
        "engine-team",
    ),
    # --- Native domain supply chain: documentation, publication, and proofs ---
    FileMeta(
        "docs/contracts/NATIVE_AUTHORING_VALIDATION.md",
        "engine-specific",
        ["docs", "contracts"],
        ["contracts", "native-authoring", "validation"],
        "engine-team",
    ),
    FileMeta(
        "docs/runbooks/domain-pack-publish.md",
        "engine-specific",
        ["docs", "runbooks"],
        ["domain-pack", "native-publication", "runbook"],
        "engine-team",
    ),
    FileMeta(
        "tools/publish_domain_pack.py",
        "engine-specific",
        ["tools"],
        ["domain-pack", "native-publication", "receipt"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_native_domain_supply_chain.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "native-domain", "supply-chain"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_native_receipt_cli.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "native-domain", "receipt", "cli"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_native_strict_compatibility.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "native-authoring", "predecessor-parity"],
        "engine-team",
    ),
    FileMeta(
        "tests/fixtures/native_authoring_predecessor_f8d933.json",
        "engine-specific",
        ["test"],
        ["test", "fixtures", "native-authoring", "predecessor-parity"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_native_runbook_cli.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "native-domain", "runbook", "cli"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_native_domain_publication.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "native-domain", "publication"],
        "engine-team",
    ),
    FileMeta(
        "tests/unit/test_native_domain_integrity.py",
        "engine-specific",
        ["test"],
        ["test", "unit", "native-domain", "integrity"],
        "engine-team",
    ),
]


# =============================================================================
# FORMATTERS — One per filetype family
# =============================================================================


def _yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def _meta_field_lines(meta: FileMeta, engine: str) -> list[str]:
    """The seven header fields, in contract order (docs/contracts/L9_META_HEADERS.md)."""
    return [
        f"l9_schema: {L9_SCHEMA_VERSION}",
        f"origin: {meta.origin}",
        f"engine: {engine}",
        f"layer: {_yaml_list(meta.layer)}",
        f"tags: {_yaml_list(meta.tags)}",
        f"owner: {meta.owner}",
        f"status: {meta.status}",
    ]


def format_comment_block(meta: FileMeta, engine: str, prefix: str = "# ") -> str:
    """Format L9_META as comment block for YAML, shell, Makefile, Dockerfile, etc."""
    lines = [
        f"{prefix}--- L9_META ---",
        *(f"{prefix}{field}" for field in _meta_field_lines(meta, engine)),
        f"{prefix}--- /L9_META ---",
    ]
    return "\n".join(lines)


def format_html_comment(meta: FileMeta, engine: str) -> str:
    """Format L9_META as HTML comment for Markdown files."""
    lines = [
        "<!-- L9_META",
        f"l9_schema: {L9_SCHEMA_VERSION}",
        f"origin: {meta.origin}",
        f"engine: {engine}",
        f"layer: {_yaml_list(meta.layer)}",
        f"tags: {_yaml_list(meta.tags)}",
        f"owner: {meta.owner}",
        f"status: {meta.status}",
        "/L9_META -->",
    ]
    return "\n".join(lines)


def format_python_docstring_block(meta: FileMeta, engine: str) -> str:
    """Format L9_META for Python docstring insertion."""
    lines = [
        "--- L9_META ---",
        *_meta_field_lines(meta, engine),
        "--- /L9_META ---",
    ]
    return "\n".join(lines)


def format_json_meta(meta: FileMeta, engine: str) -> dict[str, Any]:
    return {
        "l9_schema": L9_SCHEMA_VERSION,
        "origin": meta.origin,
        "engine": engine,
        "layer": meta.layer,
        "tags": meta.tags,
        "owner": meta.owner,
        "status": meta.status,
    }


def format_toml_block(meta: FileMeta, engine: str) -> str:
    layer_str = ", ".join(f'"{l}"' for l in meta.layer)
    tags_str = ", ".join(f'"{t}"' for t in meta.tags)
    lines = [
        "",
        "[tool.l9_meta]",
        f"l9_schema = {L9_SCHEMA_VERSION}",
        f'origin = "{meta.origin}"',
        f'engine = "{engine}"',
        f"layer = [{layer_str}]",
        f"tags = [{tags_str}]",
        f'owner = "{meta.owner}"',
        f'status = "{meta.status}"',
    ]
    return "\n".join(lines)


# =============================================================================
# STRIP EXISTING — Regex patterns for idempotent re-injection
# =============================================================================

# Matches comment-style L9_META blocks (YAML, shell, Makefile, Dockerfile, CODEOWNERS)
# Example:
#   # --- L9_META ---
#   # l9_schema: 1
#   # ...
#   # --- /L9_META ---
RE_COMMENT_META = re.compile(
    r"^[ \t]*#[ \t]*---[ \t]*L9_META[ \t]*---.*?#[ \t]*---[ \t]*/L9_META[ \t]*---[ \t]*\n?",
    re.MULTILINE | re.DOTALL,
)

# Matches HTML comment L9_META blocks (Markdown)
# Example:
#   <!-- L9_META
#   l9_schema: 1
#   ...
#   /L9_META -->
RE_HTML_META = re.compile(
    r"<!-- L9_META.*?/L9_META -->[ \t]*\n?",
    re.DOTALL,
)

# Matches L9_META inside a Python module docstring body. Applied to the body of
# the module docstring only, never to the rest of the file (code may contain the
# delimiters in string literals, as this module's own formatters do).
# Example:
#   """
#   --- L9_META ---
#   l9_schema: 1
#   ...
#   --- /L9_META ---
#
#   Module description.
#   """
RE_PY_DOCSTRING_META = re.compile(
    r"---[ \t]*L9_META[ \t]*---.*?---[ \t]*/L9_META[ \t]*---[ \t]*\n?",
    re.DOTALL,
)

# Matches broken/uncommented L9_META blocks in YAML files (missing # prefix)
# These need to be stripped before injecting the correct comment-style block
RE_BROKEN_YAML_META = re.compile(
    r"^---[ \t]*L9_META[ \t]*---.*?---[ \t]*/L9_META[ \t]*---[ \t]*\n?",
    re.MULTILINE | re.DOTALL,
)


# =============================================================================
# FILETYPE DETECTION
# =============================================================================


def _detect_filetype(path: str) -> str:
    p = Path(path)
    name = p.name.lower()
    suffix = p.suffix.lower()

    if name == "codeowners":
        return "plain-comment"
    if name in ("makefile",):
        return "comment"
    if name.startswith("dockerfile"):
        return "comment"
    if suffix in (".yaml", ".yml"):
        return "yaml"
    if suffix == ".py":
        return "python"
    if suffix == ".md":
        return "markdown"
    if suffix == ".sh":
        return "shell"
    if suffix == ".json":
        return "json"
    if suffix == ".toml":
        return "toml"
    if ".template" in name:
        return "comment"
    return "comment"


# =============================================================================
# MARKDOWN CODE FENCES — examples of a header are not a header
# =============================================================================

_RE_FENCE = re.compile(r"^[ \t]*(```|~~~)", re.MULTILINE)


def _fenced_spans(content: str) -> list[tuple[int, int]]:
    """(start, end) offsets of every fenced code block; an unclosed fence runs to EOF."""
    marks = list(_RE_FENCE.finditer(content))
    opens, closes = marks[0::2], marks[1::2]
    ends = [m.end() for m in closes] + [len(content)] * (len(opens) - len(closes))
    return [(o.start(), end) for o, end in zip(opens, ends, strict=True)]


def _sub_unfenced(pattern: re.Pattern[str], content: str) -> str:
    """Remove every match of *pattern* that does not start inside a code fence."""
    spans = _fenced_spans(content)
    return pattern.sub(
        lambda m: m.group(0) if any(start <= m.start() < end for start, end in spans) else "",
        content,
    )


# =============================================================================
# INJECTION — Filetype-aware header injection
# =============================================================================


# A triple-quoted docstring literal: optional string prefix, quotes, body, quotes.
_RE_TRIPLE_QUOTED = re.compile(r"^([rRuU]?)(\"{3}|'{3})(.*)\2$", re.DOTALL)


def _line_offsets(content: str) -> list[int]:
    """Character offset of the start of each line (index 0 is line 1)."""
    offsets = [0]
    for line in content.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _node_char_span(content: str, node: ast.expr) -> tuple[int, int]:
    """(start, end) character offsets of *node*; ast columns are UTF-8 byte offsets."""
    lines = content.splitlines(keepends=True)
    offsets = _line_offsets(content)
    end_lineno = node.end_lineno if node.end_lineno is not None else node.lineno
    end_col = node.end_col_offset if node.end_col_offset is not None else 0

    def _char(lineno: int, byte_col: int) -> int:
        line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
        return offsets[lineno - 1] + len(line.encode("utf-8")[:byte_col].decode("utf-8"))

    return _char(node.lineno, node.col_offset), _char(end_lineno, end_col)


def _prologue_end(content: str) -> int:
    """Offset just after the last line of the leading comment prologue.

    The prologue is the run of blank and ``#`` lines before the first statement
    (shebang, encoding declaration, licence). Blank lines that follow the last
    comment stay after the insertion point, so spacing before code is kept.
    """
    pos = end = 0
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            break
        pos += len(line)
        if stripped:
            end = pos
    return end


def _inject_python_meta(content: str, meta: FileMeta, engine: str) -> str:
    """Inject L9_META into the module docstring of a Python file.

    Only the module docstring (the module's first statement, as ``ast`` defines
    it) is ever rewritten. Everything before it (shebang, encoding line, licence
    comments) and everything after it is kept byte-for-byte. Without a module
    docstring, a new one is inserted after the comment prologue.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        msg = f"cannot parse Python source ({exc.msg} at line {exc.lineno}); refusing to inject"
        raise ValueError(msg) from exc

    meta_block = format_python_docstring_block(meta, engine)
    first = tree.body[0] if tree.body else None
    docstring_node = (
        first.value
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
        else None
    )

    if docstring_node is None:
        pos = _prologue_end(content)
        # A comment-style header mistakenly placed in the prologue is replaced by
        # the docstring header; code after the prologue is never searched.
        prologue = RE_COMMENT_META.sub("", content[:pos])
        if prologue and not prologue.endswith("\n"):
            prologue += "\n"
        return prologue + f'"""\n{meta_block}\n"""\n' + content[pos:]

    start, end = _node_char_span(content, docstring_node)
    literal = content[start:end]
    match = _RE_TRIPLE_QUOTED.match(literal)
    if match is None:
        msg = "module docstring is not a single triple-quoted literal; refusing to rewrite it"
        raise ValueError(msg)
    prefix, quote, body = match.group(1), match.group(2), match.group(3)

    # Strip any existing L9_META from the docstring body only.
    body_stripped = RE_PY_DOCSTRING_META.sub("", body).lstrip("\n")
    if body_stripped:
        new_ds = f"{prefix}{quote}\n{meta_block}\n\n{body_stripped}{quote}"
    else:
        new_ds = f"{prefix}{quote}\n{meta_block}\n{quote}"
    return RE_COMMENT_META.sub("", content[:start]) + new_ds + content[end:]


def _inject_comment_meta(content: str, meta: FileMeta, engine: str) -> str:
    """Inject L9_META using comment-style for shell, YAML, etc."""
    content = RE_COMMENT_META.sub("", content)
    block = format_comment_block(meta, engine)
    if content.startswith("#!"):
        nl = content.index("\n")
        return content[: nl + 1] + block + "\n" + content[nl + 1 :]
    return block + "\n" + content


def _json_header_is_current(data: dict[str, Any], expected: dict[str, Any]) -> bool:
    """True when ``_l9_meta`` is the first key and matches *expected* in schema order."""
    current = data.get("_l9_meta")
    return (
        next(iter(data), None) == "_l9_meta"
        and isinstance(current, dict)
        and list(current.items()) == list(expected.items())
    )


def _inject_json_meta(content: str, meta: FileMeta, engine: str) -> str:
    """Put ``_l9_meta`` first in a JSON object; leave non-objects and correct files as is."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    # Only inject _l9_meta into objects, not arrays (arrays would break structure)
    if not isinstance(data, dict):
        return content
    expected = format_json_meta(meta, engine)
    if _json_header_is_current(data, expected):
        # Layout belongs to the JSON formatter (Biome, biome.json), so a file
        # whose header is already correct is left byte-for-byte as is.
        return content
    data.pop("_l9_meta", None)
    ordered = {"_l9_meta": expected, **data}
    return json.dumps(ordered, indent=2) + "\n"


def inject_meta(content: str, meta: FileMeta, engine: str) -> str:
    """Inject L9_META header into file content based on filetype."""
    ftype = _detect_filetype(meta.path)

    # --- JSON ---
    if ftype == "json":
        return _inject_json_meta(content, meta, engine)

    # --- TOML ---
    if ftype == "toml":
        block = format_toml_block(meta, engine)
        cleaned = re.sub(r"\n?\[tool\.l9_meta\].*?(?=\n\[|\Z)", "", content, flags=re.DOTALL).rstrip() + "\n"
        return cleaned + block + "\n"

    # --- Markdown ---
    if ftype == "markdown":
        content = _sub_unfenced(RE_HTML_META, content)
        block = format_html_comment(meta, engine)
        return block + "\n\n" + content.lstrip("\n")

    # --- Python ---
    if ftype == "python":
        return _inject_python_meta(content, meta, engine)

    # --- Shell ---
    if ftype == "shell":
        content = RE_COMMENT_META.sub("", content)
        block = format_comment_block(meta, engine)
        if content.startswith("#!"):
            nl = content.index("\n")
            return content[: nl + 1] + block + "\n" + content[nl + 1 :]
        return block + "\n" + content

    # --- YAML ---
    if ftype == "yaml":
        # Strip both comment-style and broken (uncommented) meta blocks
        content = RE_COMMENT_META.sub("", content)
        content = RE_BROKEN_YAML_META.sub("", content)
        block = format_comment_block(meta, engine)
        # Handle YAML document separator
        content_stripped = content.lstrip()
        if content_stripped.startswith("---"):
            # Find the first --- that's a YAML doc separator (not part of meta)
            idx = content.index("---")
            return block + "\n" + content[idx:]
        return block + "\n" + content.lstrip()

    # --- Fallback: comment-style ---
    content = RE_COMMENT_META.sub("", content)
    block = format_comment_block(meta, engine)
    if content.startswith("#!"):
        nl = content.index("\n")
        return content[: nl + 1] + block + "\n" + content[nl + 1 :]
    return block + "\n" + content


# =============================================================================
# VERIFICATION — read the header a file carries (for files not in FILE_REGISTRY)
# =============================================================================

META_FIELDS = ("l9_schema", "origin", "engine", "layer", "tags", "owner", "status")


def _header_lines(block: str) -> list[str]:
    """Field lines between the delimiters of a matched header block."""
    return [ln for ln in block.strip().splitlines()[1:-1] if ln.strip()]


def _parse_list(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return None
    inner = value[1:-1].strip()
    return [item.strip() for item in inner.split(",")] if inner else []


def _raw_header(content: str, path: str) -> dict[str, Any] | None:
    """Field mapping of the header *content* carries, or None when it has none."""
    ftype = _detect_filetype(path)
    if ftype == "json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None
        meta = data.get("_l9_meta") if isinstance(data, dict) else None
        return dict(meta) if isinstance(meta, dict) else None
    if ftype == "toml":
        try:
            table = tomllib.loads(content).get("tool", {}).get("l9_meta")
        except tomllib.TOMLDecodeError:
            return None
        return dict(table) if isinstance(table, dict) else None

    if ftype == "markdown":
        spans = _fenced_spans(content)
        match = next(
            (m for m in RE_HTML_META.finditer(content) if not any(a <= m.start() < b for a, b in spans)),
            None,
        )
        lines = _header_lines(match.group(0)) if match else None
    elif ftype == "python":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return None
        docstring = ast.get_docstring(tree, clean=False)
        match = RE_PY_DOCSTRING_META.search(docstring) if docstring else None
        lines = _header_lines(match.group(0)) if match else None
    else:
        match = RE_COMMENT_META.search(content)
        lines = [re.sub(r"^[ \t]*#[ \t]?", "", ln) for ln in _header_lines(match.group(0))] if match else None
    if lines is None:
        return None
    fields: dict[str, Any] = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if not sep:
            return {"__malformed__": line.strip()}
        fields[key.strip()] = value.strip()
    return fields


def read_header(content: str, path: str, engine: str) -> FileMeta | str | None:
    """The header of an unregistered file as FileMeta; a reason string if it is not v1; None if absent."""
    fields = _raw_header(content, path)
    if fields is None:
        return None
    if tuple(fields) != META_FIELDS:
        return f"fields {list(fields)} != schema v{L9_SCHEMA_VERSION} {list(META_FIELDS)}"
    if str(fields["l9_schema"]) != str(L9_SCHEMA_VERSION):
        return f"l9_schema {fields['l9_schema']} != {L9_SCHEMA_VERSION}"
    if fields["engine"] != engine:
        return f"engine {fields['engine']!r} != {engine!r}"
    layer, tags = _parse_list(fields["layer"]), _parse_list(fields["tags"])
    if layer is None or tags is None:
        return "layer and tags must be [a, b] lists"
    scalars = [fields[k] for k in ("origin", "owner", "status")]
    if not all(isinstance(v, str) and v for v in scalars):
        return "origin, owner and status must be non-empty strings"
    origin, owner, status = scalars
    return FileMeta(path, origin, layer, tags, owner, status)


# =============================================================================
# DISCOVERY — the eligible file set is the git index, not a hand list
# =============================================================================

# contracts/contract_18.yaml `scope.paths`, verbatim (a unit test keeps the two
# in lockstep). A pattern without "/" matches the basename at any depth.
SCOPE_PATTERNS: tuple[str, ...] = (
    "**/*.py",
    "**/*.yaml",
    "**/*.yml",
    "**/*.md",
    "**/*.json",
    "**/*.toml",
    "**/*.sh",
    "Dockerfile*",
    "Makefile",
)

# Tracked paths that are never eligible, with the reason. Keep this short: a
# file that merely lacks a header belongs in HEADERLESS_BASELINE instead.
EXCLUDED_PATTERNS: dict[str, str] = {
    "artifacts/**": (
        "generated: rewritten on every run by tools/audit_engine.py (write_report), "
        "tools/spec_extract.py and tools/audit_harness.py, which emit no header"
    ),
    "reports/end-session/**": (
        "generated: session and governance handoff reports written on every publish by "
        "the governance post-publish hook, which emits no header"
    ),
    "chassis/Dockerfile.chassis": (
        'protected infrastructure file (Dockerfile*); its header declares engine: "*" for the '
        "engine-agnostic chassis image, which this repo's engine-scoped v1 check cannot express"
    ),
    "Makefile": (
        "Core-owned make-v1 facade (Quantum-L9/l9-ci-core tools/l9_repo/facades/make-v1.mk): "
        "Core's repository verification compares it byte-for-byte with the template, so a header "
        "fails the contract; the repository recipes and their header live in Repo.mk. Only the "
        "root Makefile is tracked; a pattern without '/' would also match a nested Makefile"
    ),
    ".l9/repo-workflow.json": (
        "Core repository execution contract (l9.repo-execution/v2): Core rejects any key besides "
        "schema, facade and required_phases, so the JSON header's _l9_meta key fails the contract"
    ),
    ".github/CODEOWNERS": (
        "org-managed seed (Quantum-L9/.github policies/CODEOWNERS, ADR-0019 managed seed paths): the "
        "org seed restores it byte-for-byte, so a header here would be stripped on the next seed"
    ),
    ".github/dependabot.yml": (
        "org-managed render (Quantum-L9/.github ops/render-dependabot.js, ADR-0019): the org seed "
        "restores it byte-for-byte to the render, which carries its own provenance comment, so a "
        "header here would be stripped on the next seed"
    ),
}

# Headerless files that are still eligible: when one does carry a header, the
# header is verified like any other.
# Tracked files that had no header when the gate became able to fail (RU-131).
# This is a ratchet, not a scope rule: a new file is never in it, so it must be
# headered. To retire an entry, register the file in FILE_REGISTRY, run
# --apply and delete the line. An entry whose file is gone is ignored.
HEADERLESS_BASELINE: frozenset[str] = frozenset(
    {
        ".claude/agents/code-reviewer.md",
        ".claude/agents/domain-validator.md",
        ".claude/rules/capability-registry.md",
        ".claude/rules/contracts.md",
        ".claude/rules/feature-flags.md",
        ".claude/rules/gates-scoring.md",
        ".claude/rules/learnings.md",
        ".claude/rules/routing.md",
        ".claude/rules/subsystems.md",
        ".claude/rules/system-state.md",
        ".claude/rules/testing.md",
        ".claude/settings.json",
        ".claude/skills/action-handler-development/SKILL.md",
        ".claude/skills/contract-check/SKILL.md",
        ".claude/skills/domain-spec-authoring/SKILL.md",
        ".claude/skills/gate-development/SKILL.md",
        ".claude/skills/pr-workflow/SKILL.md",
        ".claude/skills/scoring-development/SKILL.md",
        ".devcontainer/devcontainer.json",
        ".github/ISSUE_TEMPLATE/1-bug.yml",
        ".github/ISSUE_TEMPLATE/2-feature.yml",
        ".github/ISSUE_TEMPLATE/3-task.yml",
        ".github/ISSUE_TEMPLATE/4-incident.yml",
        ".github/ISSUE_TEMPLATE/ci-failure.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/gov-violation.yml",
        ".github/ISSUE_TEMPLATE/seed-ci-failure.yml",
        ".github/PULL_REQUEST_TEMPLATE/agent.md",
        ".github/PULL_REQUEST_TEMPLATE/release.md",
        ".github/governance/execution-profiles.yaml",
        ".github/governance/promotion-policy.yaml",
        ".github/governance/provider-requiredness.yaml",
        ".github/governance/quality-thresholds.yaml",
        ".github/governance/rule-modes.yaml",
        ".github/governance/semgrep-finding-policy.yaml",
        ".github/governance/semgrep-identity-map.yaml",
        ".github/governance/semgrep-policy.yaml",
        ".github/governance/waivers.yaml",
        ".github/pull_request_template.md",
        ".github/workflows/coderabbit-notify.yml",
        ".github/workflows/governance.yml",
        ".github/workflows/l9-lint-test-node.yml",
        ".github/workflows/l9-lint-test.yml",
        ".l9/baselines/packet-envelope.yml",
        ".l9/ci.json",
        ".vscode/extensions.json",
        "ARCHITECTURE.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "DEFERRED.md",
        "GUARDRAILS.md",
        "INTELLIGENCE_ARCHITECTURE.md",
        "INTELLIGENCE_BRIEF.md",
        "SECURITY.md",
        "TESTING.md",
        "agents/cursor/GMP-v2.0-Perplex-Py-Scripts/script.py",
        "agents/cursor/GMP-v2.0-Perplex-Py-Scripts/script_1.py",
        "agents/cursor/GMP-v2.0-Perplex-Py-Scripts/script_2.py",
        "agents/cursor/GMP-v2.0-Perplex-Py-Scripts/script_3.py",
        "agents/cursor/GMP-v2.0-Perplex-Py-Scripts/script_4.py",
        "agents/cursor/README.md",
        "agents/cursor/cursor_client.py",
        "agents/cursor/cursor_memory_client.py",
        "agents/cursor/cursor_memory_kernel.py",
        "agents/cursor/cursor_memory_kernel.yaml",
        "agents/cursor/cursor_neo4j_query.py",
        "agents/cursor/cursor_retrieval_kernel.py",
        "agents/cursor/cursor_session_hooks.py",
        "agents/cursor/cursor_system_prompt.md",
        "agents/cursor/docs/CURSOR-L9-INTEGRATION.md",
        "agents/cursor/docs/CURSOR-MEMORY-CLIENT.md",
        "agents/cursor/docs/PRODUCTION-SPEED-PACK.md",
        "agents/cursor/gmp_meta_learning.py",
        "agents/cursor/gmp_protocol/README.md",
        "agents/cursor/gmp_protocol/cursor-gmp-canonical.md",
        "agents/cursor/gmp_protocol/cursor-gmp-runbook.md",
        "agents/cursor/gmp_protocol/cursor-gmp-template-quick-reference.md",
        "agents/cursor/gmp_protocol/cursor-gmp-template.md",
        "agents/cursor/gmp_protocol/cursor-phase-0-planning.md",
        "agents/cursor/gmp_protocol/cursor-phase-1-baseline.md",
        "agents/cursor/gmp_protocol/cursor-phase-2-implementation.md",
        "agents/cursor/gmp_protocol/cursor-phase-3-enforcement.md",
        "agents/cursor/gmp_protocol/cursor-phase-4-validation.md",
        "agents/cursor/gmp_protocol/cursor-phase-5-recursion.md",
        "agents/cursor/gmp_protocol/cursor-phase-6-finalization.md",
        "agents/cursor/gmp_protocol/cursor-phase-x-gmp-audit.md",
        "agents/cursor/gmp_protocol/gmp-contract.yaml",
        "agents/cursor/gmp_protocol/gmp-report-contract.yaml",
        "agents/cursor/gmp_protocol/gmp-report-template.md",
        "agents/cursor/gmp_protocol/phase0-scope-template.yaml",
        "agents/cursor/governance-reference.md",
        "agents/cursor/ingest_lessons.py",
        "agents/cursor/integrations/cursor_executor.py",
        "agents/cursor/integrations/cursor_gateway.py",
        "agents/cursor/integrations/cursor_langgraph.py",
        "agents/cursor/perplexity_research_queries/2026-01-20-langgraph-checkpoint-postgres.md",
        "agents/cursor/perplexity_research_results/01-15-2026 - memory-substrate-stages/PHASE-0-TODO-STAGE-4-BELIEF-REVISION.md",
        "agents/cursor/perplexity_research_results/01-15-2026 - memory-substrate-stages/stage4_belief_revision_system.md",
        "agents/cursor/perplexity_research_results/01-15-2026 - memory-substrate-stages/stage6_multi_agent_consensus.md",
        "agents/cursor/perplexity_research_results/01-16-2026 - predictive-memory-warming/README.md",
        "agents/cursor/perplexity_research_results/01-16-2026 - predictive-memory-warming/perplexity-deep-research-output.md",
        "agents/cursor/perplexity_research_results/01-16-2026 - predictive-memory-warming/stage5_predictive_memory_warming.md",
        "agents/cursor/perplexity_research_results/01-18-2026 - readme-generation-gmp100/GMP-100-README-GENERATION-TRACKER.md",
        "agents/cursor/perplexity_research_results/01-20-2026-langgraph-checkpoint-postgres/ANALYZE-EVALUATE-REPORT.md",
        "agents/cursor/perplexity_research_results/01-20-2026-langgraph-checkpoint-postgres/PHASE-0-TODO-PLAN.md",
        "agents/cursor/perplexity_research_results/01-20-2026-langgraph-checkpoint-postgres/langgraph-checkpoint-postgres-best-practices.md",
        "agents/cursor/perplexity_research_results/README-SOP.md",
        "agents/cursor/perplexity_research_results/README.md",
        "agents/cursor/prompts/action_prompts/CEG PR Review.md",
        "agents/cursor/prompts/gmp_examples/GMP-Action-Add-Learning-API-Routes.md",
        "agents/cursor/prompts/gmp_examples/GMP-Action-Create-Learning-Tests.md",
        "agents/cursor/prompts/gmp_examples/GMP-Action-Wire-Learning-Engine.md",
        "agents/cursor/prompts/gmp_examples/README.md",
        "agents/cursor/prompts/perplexity_research_prompts/Prompt Example - Perplexity Research Agent.md",
        "agents/cursor/scripts/cursor_check_mistakes.py",
        "agents/cursor/superprompt.md",
        "biome.json",
        "contracts/README.md",
        "contracts/contract_01.yaml",
        "contracts/contract_02.yaml",
        "contracts/contract_03.yaml",
        "contracts/contract_04.yaml",
        "contracts/contract_05.yaml",
        "contracts/contract_06.yaml",
        "contracts/contract_07.yaml",
        "contracts/contract_08.yaml",
        "contracts/contract_09.yaml",
        "contracts/contract_10.yaml",
        "contracts/contract_11.yaml",
        "contracts/contract_12.yaml",
        "contracts/contract_13.yaml",
        "contracts/contract_14.yaml",
        "contracts/contract_15.yaml",
        "contracts/contract_16.yaml",
        "contracts/contract_17.yaml",
        "contracts/contract_19.yaml",
        "contracts/contract_20.yaml",
        "contracts/contract_21.yaml",
        "contracts/contract_22.yaml",
        "contracts/contract_23.yaml",
        "contracts/contract_24.yaml",
        "contracts/match_request.json",
        "contracts/match_response.json",
        "contracts/payloads/README.md",
        "contracts/payloads/canonical-projection.schema.yaml",
        "contracts/payloads/common.schema.yaml",
        "contracts/payloads/examples/canonical-projection.json",
        "contracts/payloads/examples/improvement-proposal.json",
        "contracts/payloads/examples/match-request.json",
        "contracts/payloads/examples/match-response.json",
        "contracts/payloads/examples/outcome-feedback.json",
        "contracts/payloads/examples/sync-projection-tombstone.json",
        "contracts/payloads/examples/sync-projection.json",
        "contracts/payloads/improvement-proposal.schema.yaml",
        "contracts/payloads/match-request.schema.yaml",
        "contracts/payloads/match-response.schema.yaml",
        "contracts/payloads/negative_examples/canonical-projection-transport-field.json",
        "contracts/payloads/negative_examples/improvement-proposal-direct-mutation.json",
        "contracts/payloads/negative_examples/match-request-transport-field.json",
        "contracts/payloads/negative_examples/match-response-ineligible-ranked.json",
        "contracts/payloads/negative_examples/outcome-feedback-unknown-type.json",
        "contracts/payloads/negative_examples/sync-projection-unknown-operation.json",
        "contracts/payloads/outcome-feedback.schema.yaml",
        "contracts/payloads/sync-projection.schema.yaml",
        "docs/AUDIT_HARNESS.md",
        "docs/CI_CONSTELLATION_BOUNDARY.md",
        "docs/Commands.md",
        "docs/SEL4_UPGRADES.md",
        "docs/SEL4_UPGRADES_EXECUTIVE.md",
        "docs/adr/ADR-103-ceg-plasticos-spec-authority.md",
        "docs/adr/ADR-104-first-match-direction.md",
        "docs/adr/ADR-105-second-match-direction.md",
        "docs/adr/ADR-106-detach-match-improvement-payloads.md",
        "docs/adr/ADR-107-harvest-ceg-gates-features-outcomes.md",
        "docs/adr/ADR-108-ceg-projection-outcome-schemas.md",
        "docs/adr/ADR-109-ceg-contract-compiler-validator.md",
        "docs/adr/ADR-110-ceg-shadow-comparison-outputs.md",
        "docs/adr/ADR-111-ceg-outcome-replay.md",
        "docs/adr/ADR-112-ceg-domain-neutral-core.md",
        "docs/adr/ADR-DEC-001-candidate-identity.md",
        "docs/archive/plasticos/ARCHIVE_MAP.md",
        "docs/audits/2026-08-23-gap-fix-artifact-convergence/GAP_FIX_ARTIFACT_INVENTORY.yaml",
        "docs/audits/2026-08-23-gap-fix-artifact-convergence/GAP_FIX_REACHABILITY_CLASSIFICATION.yaml",
        "docs/audits/2026-08-23-gap-fix-artifact-convergence/IMPLEMENTATION_FILETREE.yaml",
        "docs/audits/2026-10-11-ceg-domain-neutrality-stage2/STAGE2_FINDING_CLOSURE.yaml",
        "docs/contracts/README.md",
        "docs/contracts/VERSIONING.md",
        "docs/contracts/_templates/api-endpoint.template.yaml",
        "docs/contracts/_templates/data-model.template.json",
        "docs/contracts/_templates/tool-schema.template.json",
        "docs/contracts/agents/tool-schemas/_index.yaml",
        "docs/contracts/api/openapi.yaml",
        "docs/contracts/config/env-contract.yaml",
        "docs/contracts/data/graph-schema.yaml",
        "docs/contracts/data/models/outcome-record.schema.json",
        "docs/contracts/data/models/packet-envelope.schema.json",
        "docs/contracts/dependencies/neo4j.yaml",
        "docs/contracts/events/asyncapi.yaml",
        "docs/github-ruleset-diagnostics.md",
        "docs/runbooks/CEG_OUTCOME_REPLAY.md",
        "docs/runbooks/CEG_SHADOW_COMPARISON.md",
        "engine/security/llm.py",
        "memory-bank/activeContext.md",
        "memory-bank/progress.md",
        "memory-bank/tasks.md",
        "memory-bank/tech-debt.md",
        "reports/GMP-134-lane-h5-one-effective-feedback-predicate.md",
        "reports/GMP-134-lane-id-cid-tenant-identity-grammar-on-p.md",
        "reports/GMP-Report-132-Fix-Residual-PreExisting-Issues.md",
        "reports/GMP-Report-133-Gate-SDK-Integration.md",
        "reports/hoprag_ceg_enhancement_program.md",
        "scripts/validate_sdk_pin.py",
        "tests/architecture/test_seam_gate_only.py",
        "tests/contracts/_constants.py",
        "tests/contracts/conftest.py",
        "tests/contracts/test_asyncapi_contract.py",
        "tests/contracts/test_auditor_wiring.py",
        "tests/contracts/test_contract_registry.py",
        "tests/contracts/test_data_models.py",
        "tests/contracts/test_dependency_contracts.py",
        "tests/contracts/test_env_contract.py",
        "tests/contracts/test_known_gaps.py",
        "tests/contracts/test_openapi_lint.py",
        "tests/contracts/test_packet_envelope.py",
        "tests/contracts/test_research_wiring.py",
        "tests/contracts/test_schema_json_schemas.py",
        "tests/contracts/test_templates.py",
        "tests/contracts/test_tool_schemas.py",
        "tests/e2e/constellation_odoo/FINAL_ODOO_E2E_REPORT.md",
        "tests/e2e/constellation_odoo/README.md",
        "tests/e2e/constellation_odoo/compose.eie-deterministic.yml",
        "tests/e2e/constellation_odoo/compose.odoo.yml",
        "tests/e2e/constellation_odoo/compose.sdk-participation.yml",
        "tests/e2e/constellation_odoo/results/SUMMARY.md",
        "tests/e2e/constellation_odoo/scripts/assert_odoo_evidence.py",
        "tests/e2e/constellation_odoo/scripts/build_images_odoo.sh",
        "tests/e2e/constellation_odoo/scripts/gen_env_odoo.sh",
        "tests/e2e/constellation_odoo/scripts/odoo_driver.py",
        "tests/e2e/constellation_odoo/scripts/redact_odoo.py",
        "tests/e2e/constellation_odoo/scripts/run_odoo_e2e.sh",
        "tests/e2e/constellation_odoo/scripts/sdk_participation_verdict.py",
        "tests/fixtures/benchmark_data.py",
        "tests/fixtures/payloads/match_request.json",
        "tests/fixtures/payloads/outcomes_request.json",
        "tests/fixtures/payloads/sync_request.json",
        "tests/gap_fixes/test_gap3_inference_registry.py",
        "tests/gap_fixes/test_gap9_inference_authority.py",
        "tests/integration/test_admin_handler.py",
        "tests/integration/test_causal_serializer_depth.py",
        "tests/integration/test_causal_temporal_decay.py",
        "tests/integration/test_clamp_nan.py",
        "tests/integration/test_dimension_weight_roundtrip.py",
        "tests/integration/test_gate_client.py",
        "tests/integration/test_gate_registration.py",
        "tests/integration/test_graph_driver.py",
        "tests/integration/test_handlers.py",
        "tests/integration/test_idea_portfolio_hydration.py",
        "tests/integration/test_match_handler.py",
        "tests/integration/test_outcomes_handler.py",
        "tests/integration/test_packet_bridge.py",
        "tests/integration/test_pricealignment_integer_operands.py",
        "tests/integration/test_score_propagator_integration.py",
        "tests/integration/test_sync_handler.py",
        "tests/invariants/test_compliance.py",
        "tests/invariants/test_configuration.py",
        "tests/invariants/test_module_reachability.py",
        "tests/invariants/test_resilience.py",
        "tests/invariants/test_trust_boundary.py",
        "tests/property/test_gates_property.py",
        "tests/property/test_scoring_property.py",
        "tests/scoring/test_benchmark.py",
        "tests/security/test_compliance_security.py",
        "tests/security/test_injection.py",
        "tests/test_algorithmic_upgrades.py",
        "tests/test_boot_and_registry.py",
        "tests/test_health_check.py",
        "tests/test_pareto_wiring.py",
        "tests/unit/test_arbitration.py",
        "tests/unit/test_audit_engine_cache.py",
        "tests/unit/test_audit_harness_parallel.py",
        "tests/unit/test_belief_propagation.py",
        "tests/unit/test_compliance_checker.py",
        "tests/unit/test_cypher_lint.py",
        "tests/unit/test_cypher_utils.py",
        "tests/unit/test_detached_payload_contracts.py",
        "tests/unit/test_domain_database_provisioning.py",
        "tests/unit/test_domain_loader.py",
        "tests/unit/test_domain_pack_shape.py",
        "tests/unit/test_first_match_direction.py",
        "tests/unit/test_gate_compiler.py",
        "tests/unit/test_gate_egress.py",
        "tests/unit/test_graph_inference_egress.py",
        "tests/unit/test_idea_portfolio.py",
        "tests/unit/test_outcome_replay.py",
        "tests/unit/test_outcomes.py",
        "tests/unit/test_packet_bridge.py",
        "tests/unit/test_parameter_resolver.py",
        "tests/unit/test_pareto.py",
        "tests/unit/test_pareto_ensemble.py",
        "tests/unit/test_payload_contract_compiler.py",
        "tests/unit/test_plasticos_harvest.py",
        "tests/unit/test_projection_outcome_schemas.py",
        "tests/unit/test_protocol_bodies.py",
        "tests/unit/test_sanitize.py",
        "tests/unit/test_score_propagator.py",
        "tests/unit/test_scoring_assembler.py",
        "tests/unit/test_second_match_direction.py",
        "tests/unit/test_shadow_comparison.py",
        "tests/unit/test_sync_generator.py",
        "tests/unit/test_sync_projection.py",
        "tests/unit/test_traversal_assembler.py",
        "tests/unit/test_validate_sdk_pin.py",
        "tools/audit_harness.py",
        "tools/check_action_refs.py",
        "tools/check_deprecated_imports.py",
        "tools/cypher_lint.py",
        "tools/hydrate_idea_portfolio.py",
        "tools/outcome_replay.py",
        "tools/packet_envelope_gate.py",
        "tools/shadow_comparison.py",
        "tools/validate_domain.py",
    }
)

HEADERLESS_ALLOWED_PATTERNS: dict[str, str] = {
    "**/__init__.py": "docs/contracts/L9_META_HEADERS.md (Exemptions)",
}


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    """gitignore-style match: "**" spans directories, a slash-free pattern matches the basename."""
    target = path if "/" in pattern else path.rsplit("/", 1)[-1]
    return _glob_regex(pattern).match(target) is not None


def in_scope(path: str) -> bool:
    return any(glob_match(path, pat) for pat in SCOPE_PATTERNS)


def is_excluded(path: str) -> bool:
    return any(glob_match(path, pat) for pat in EXCLUDED_PATTERNS)


def headerless_allowed(path: str) -> bool:
    return path in HEADERLESS_BASELINE or any(glob_match(path, pat) for pat in HEADERLESS_ALLOWED_PATTERNS)


def tracked_files(root: Path) -> list[str]:
    """Paths in the git index of the repository whose top level is *root*.

    Raises RuntimeError when *root* is not a repository top level or git fails:
    an undeterminable eligible set must fail the check, never shrink it.
    """
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True, check=False
        )
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
            msg = f"{root} is not the top level of a git repository"
            raise RuntimeError(msg)
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False)
    except OSError as exc:
        msg = f"git is not available ({exc})"
        raise RuntimeError(msg) from exc
    if listed.returncode != 0:
        msg = f"git ls-files failed: {listed.stderr.decode(errors='replace').strip()}"
        raise RuntimeError(msg)
    return sorted(p for p in listed.stdout.decode("utf-8").split("\0") if p)


# =============================================================================
# MAIN
# =============================================================================


FAILING_STATUSES = ("PENDING", "MISSING", "NOHEADER", "INVALID", "ERROR")


def main(argv: list[str] | None = None) -> int:
    """Verify (default, or ``check``) or ``--apply`` L9_META headers; return the exit status.

    Eligible files are every FILE_REGISTRY entry plus every git-tracked file
    matching SCOPE_PATTERNS, minus EXCLUDED_PATTERNS. Registered files must
    carry exactly the registered header. Unregistered files must carry a
    canonical schema-v1 header, unless HEADERLESS_ALLOWED_PATTERNS or
    HEADERLESS_BASELINE allow them to have none.

    Exit 1 when any file is PENDING (would change), MISSING (registered but
    absent), NOHEADER, INVALID (not schema v1) or ERROR, or when the eligible
    set cannot be determined. With --apply, PENDING files are rewritten and do
    not count as failures.
    """
    parser = argparse.ArgumentParser(description="Verify or inject L9_META headers (contract C-18)")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["check"],
        help="check: verify only and exit 1 on any finding (same as the default dry-run)",
    )
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--engine", default=ENGINE_ID, help=f"Engine ID (default: {ENGINE_ID})")
    parser.add_argument("--root", default=".", help="Repo root directory")
    args = parser.parse_args(argv)
    if args.command == "check" and args.apply:
        parser.error("check verifies only; it cannot be combined with --apply")

    root = Path(args.root).resolve()
    engine = args.engine
    counts = dict.fromkeys(("UPDATED", "SKIP", "EXEMPT", *FAILING_STATUSES), 0)

    try:
        tracked = tracked_files(root)
    except RuntimeError as exc:
        print(f"  ERROR    cannot determine the eligible file set: {exc}")
        return 1

    registry = {fm.path: fm for fm in FILE_REGISTRY}
    eligible = sorted(set(registry) | {p for p in tracked if in_scope(p) and not is_excluded(p)})

    for rel in eligible:
        fpath = root / rel
        fm = registry.get(rel)
        if not fpath.is_file():
            print(f"  MISSING  {rel}")
            counts["MISSING"] += 1
            continue
        try:
            content = fpath.read_text(encoding="utf-8")
            if fm is None:
                found = read_header(content, rel, engine)
                if found is None:
                    if headerless_allowed(rel):
                        counts["EXEMPT"] += 1
                    else:
                        print(f"  NOHEADER {rel}  (tracked, no L9_META header; register it and run --apply)")
                        counts["NOHEADER"] += 1
                    continue
                if isinstance(found, str):
                    print(f"  INVALID  {rel}  ({found})")
                    counts["INVALID"] += 1
                    continue
                fm = found
            new_content = inject_meta(content, fm, engine)
            if new_content == content:
                print(f"  SKIP     {rel}  (already has correct meta)")
                counts["SKIP"] += 1
                continue
            if args.apply:
                fpath.write_text(new_content, encoding="utf-8")
                print(f"  UPDATED  {rel}")
                counts["UPDATED"] += 1
            else:
                print(f"  PENDING  {rel}  (dry-run)")
                counts["PENDING"] += 1
        except Exception as e:
            print(f"  ERROR    {rel}  ({e})")
            counts["ERROR"] += 1

    failures = sum(counts[k] for k in FAILING_STATUSES)
    print(f"\n{'=' * 60}")
    print(f"  Engine:    {engine}")
    for key in ("UPDATED", "SKIP", "EXEMPT", *FAILING_STATUSES):
        print(f"  {key.capitalize() + ':':<10} {counts[key]}")
    print(f"  Eligible:  {len(eligible)}")
    if failures:
        print(f"\n  >>> FAIL: {failures} file(s) violate C-18 (see docs/contracts/L9_META_HEADERS.md).")
        if counts["PENDING"]:
            print("  >>> Run with --apply to write the PENDING headers.")
    else:
        print("\n  >>> PASS")
    print(f"{'=' * 60}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

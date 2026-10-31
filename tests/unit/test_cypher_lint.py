"""Unit tests — tools/cypher_lint.py, the C-009 scanner.

The scanner is contract enforcement, so these tests pin both directions:
every unsafe shape it must catch, and every validated shape it must pass —
including the two cases the 2026-09-20 audit found the keyword-gated
predecessor wrong on (F280-2): a raw ``NOT EXISTS((query)-[:{edge}]->(c))``
fragment with none of the old trigger keywords, and a module-constant label
in a multi-line ``MERGE``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.cypher_lint import Finding, scan_source, scan_tree

pytestmark = pytest.mark.unit


def _blocking(source: str) -> list[Finding]:
    return [f for f in scan_source(source, rel_path="engine/x.py") if f.blocking]


def _kinds(source: str) -> list[str]:
    return [f.kind.split(" —")[0] for f in _blocking(source)]


# ── must catch ──────────────────────────────────────────────────────────────


def test_quoted_value_interpolation_fails() -> None:
    src = "def compile(status):\n    return f\"SET n.status = '{status}'\"\n"
    assert _kinds(src) == ["quoted value interpolation"]


def test_double_quoted_value_interpolation_fails() -> None:
    src = "def compile(v):\n    cypher = f'MATCH (n) WHERE n.x = \"{v}\" RETURN n'\n    return cypher\n"
    assert _kinds(src) == ["quoted value interpolation"]


def test_limit_and_skip_interpolation_fail() -> None:
    # The keyword is interpolated so this fixture stays out of tools/contract_scanner.py SEC-004's line regex.
    kw = "LIMIT"
    src = f"def q(n, k):\n    return f'MATCH (n) RETURN n SKIP {{k}} {kw} {{n}}'\n"
    assert _kinds(src) == ["LIMIT / SKIP bound interpolated", "LIMIT / SKIP bound interpolated"]


def test_raw_spec_edge_type_in_exists_pattern_without_trigger_keywords_fails() -> None:
    """F280-2 false negative: no MATCH/WHERE/… token, yet a live label interpolation."""
    src = (
        "class ExclusionGate:\n"
        "    def compile(self):\n"
        "        edge = self.spec.edgetype\n"
        '        from_node = self.spec.fromnode or "query"\n'
        '        to_node = self.spec.tonode or "candidate"\n'
        '        return f"NOT EXISTS(({from_node})-[:{edge}]->({to_node}))"\n'
    )
    findings = _blocking(src)
    assert [f.expression for f in findings] == ["from_node", "edge", "to_node"]
    assert findings[1].kind.startswith("label / relationship type is not validated")
    assert findings[0].kind.startswith("raw domain-spec value")


def test_unvalidated_label_position_fails() -> None:
    src = "def q(label):\n    return f'MATCH (n:{label}) RETURN n'\n"
    assert _kinds(src) == ["label / relationship type is not validated (sanitize_label)"]


def test_unvalidated_property_position_fails() -> None:
    src = "def q(prop):\n    return f'MATCH (n) WHERE n.{prop} IS NULL RETURN n'\n"
    assert _kinds(src) == ["property name is not validated (sanitize_label)"]


def test_unvalidated_parameter_name_fails() -> None:
    src = "def compile(gate):\n    return f'candidate.x = ${gate.queryparam}'\n"
    assert _kinds(src) == ["parameter name is not validated"]


def test_raw_spec_value_in_bare_position_inside_compile_fails() -> None:
    """A fragment with no Cypher keyword at all is still scanned inside a compiler."""
    src = "def compile(self):\n    op = self.spec.operator\n    return f'{self._prop_ref()} {op} $x'\n"
    assert _kinds(src) == ["raw domain-spec value interpolated into Cypher"]


def test_raw_metadata_get_fails() -> None:
    src = "def _compile_x(dim):\n    v = dim.metadata.get('success_value', 'won')\n    return f'WHERE o.t = {v}'\n"
    assert _kinds(src) == ["raw domain-spec value interpolated into Cypher"]


def test_multiline_fstring_is_analysed_as_one_unit() -> None:
    src = 'def q(label):\n    return f"""\n    MATCH (n:{label})\n    RETURN n\n    """\n'
    findings = _blocking(src)
    assert len(findings) == 1
    assert findings[0].line_no == 3


def test_unvalidated_backquoted_identifier_fails() -> None:
    src = "async def _provision(self, name):\n    cypher = f'CREATE DATABASE `{name}` IF NOT EXISTS WAIT'\n"
    assert _kinds(src) == ["back-quoted identifier is not validated (sanitize_database_name / sanitize_label)"]


def test_fstring_flowing_to_execute_query_is_scanned_without_keywords() -> None:
    src = "async def run(driver, x):\n    await driver.execute_query(f\"'{x}'\", database='neo4j')\n"
    assert _kinds(src) == ["quoted value interpolation"]


# ── must pass ───────────────────────────────────────────────────────────────


def test_sanitize_label_call_passes() -> None:
    src = (
        "from engine.utils.security import sanitize_label\n"
        "def q(label):\n"
        "    return f'MATCH (n:{sanitize_label(label)}) RETURN n'\n"
    )
    assert _blocking(src) == []


def test_module_constant_label_in_multiline_merge_passes() -> None:
    """F280-2 false positive: the shape that failed current main's idea_portfolio.py."""
    src = (
        'STATE_LABEL = "IdeaPortfolioHydrationState"\n'
        'STATE_ID_PROPERTY = "state_id"\n'
        '_LOCK_STATE_CYPHER = f"""MERGE (state:{STATE_LABEL} {{{STATE_ID_PROPERTY}: $state_id}})\n'
        "ON CREATE SET state.tenant = $tenant\n"
        'RETURN state"""\n'
    )
    assert _blocking(src) == []


def test_name_assigned_from_sanitizer_passes() -> None:
    src = "def q(self):\n    prop = sanitize_label(self.spec.candidateprop)\n    return f'candidate.{prop} >= $min'\n"
    assert _blocking(src) == []


def test_self_attribute_assigned_from_sanitizer_passes() -> None:
    src = (
        "class X:\n"
        "    def __init__(self, spec):\n"
        "        self._label = sanitize_label(spec.label)\n"
        "    def q(self):\n"
        "        return f'MATCH (n:{self._label}) RETURN n'\n"
    )
    assert _blocking(src) == []


def test_same_module_helper_returning_validated_value_passes() -> None:
    src = (
        "class S:\n"
        "    def _get_candidate_label(self, job):\n"
        "        return sanitize_label(job.label)\n"
        "    def q(self, job):\n"
        "        node_label = self._get_candidate_label(job)\n"
        "        return f'MATCH (f:{node_label}) RETURN f'\n"
    )
    assert _blocking(src) == []


def test_parameter_name_built_from_sanitized_parts_passes() -> None:
    src = (
        "def _compile(dim):\n"
        "    safe_dim = sanitize_label(dim.name)\n"
        "    key = f'pref_success_{safe_dim}'\n"
        "    return f'WHERE o.t = ${key}'\n"
    )
    assert _blocking(src) == []


def test_join_over_sanitized_comprehension_passes() -> None:
    src = (
        "def q(self):\n"
        "    safe_types = [sanitize_label(t) for t in self._spec.edge_types]\n"
        "    edge_pattern = '|'.join(safe_types)\n"
        "    depth = int(self._spec.chain_depth_limit)\n"
        "    return f'MATCH p = (a)-[:{edge_pattern}*1..{depth}]->(b) RETURN p'\n"
    )
    assert _blocking(src) == []


def test_numeric_cast_and_allow_list_lookup_pass() -> None:
    src = (
        "_OPERATORS = {'>=': '>=', '<=': '<='}\n"
        "def compile(gate):\n"
        "    op = _OPERATORS[gate.operator or '>=']\n"
        "    days = int(gate.maxagedays or 1)\n"
        "    return f'candidate.x {op} $y AND candidate.d >= datetime() - duration({{days: {days}}})'\n"
    )
    assert _blocking(src) == []


def test_parameterized_values_pass() -> None:
    src = (
        "def compile(self):\n"
        "    ref = self._bind_param('key_0', self.spec.mapping)\n"
        "    return f'CASE WHEN $query.x = {ref} THEN candidate.y IN $vals ELSE false END'\n"
    )
    assert _blocking(src) == []


def test_diagnostic_fstrings_are_not_cypher() -> None:
    src = (
        "def check(self, gate, name):\n"
        "    if not gate.queryparam:\n"
        "        raise ValueError(f\"Gate '{gate.name}': queryparam required\")\n"
        "    msg = f\"Gate '{gate.name}' MATCH failed\"\n"
        '    logger.warning(f"Unknown gate type: {gate.type}, WHERE is it")\n'
        "    warnings.append(f\"Gate '{gate.name}': parameter '${gate.queryparam}' missing\")\n"
        "    return {'status': 'skipped', 'reason': f'unknown algorithm: {name}'}\n"
    )
    assert _blocking(src) == []


def test_fstring_passed_to_sanitizer_is_validated_by_the_call() -> None:
    src = "def _x(dim):\n    prop_name = sanitize_label(f'_prior_{dim.name}')\n    return f'candidate.{prop_name}'\n"
    assert _blocking(src) == []


def test_non_cypher_fstrings_outside_compilers_are_ignored() -> None:
    src = (
        "def cache_key(tenant, entity_id, digest):\n"
        "    return f'ceg:enrich:{tenant}:{entity_id}:{digest[:16]}'\n"
        "def path(prefix, key):\n"
        "    return f'{prefix}.{key}'\n"
    )
    assert _blocking(src) == []


# ── facts are lexically scoped (Codex P1 / Copilot on PR #285) ──────────────


def test_sanitizer_binding_in_one_function_does_not_certify_another() -> None:
    src = (
        "def safe(value):\n"
        "    label = sanitize_label(value)\n"
        "    return f'MATCH (n:{label}) RETURN n'\n"
        "def unsafe(label):\n"
        "    return f'MATCH (n:{label}) RETURN n'\n"
    )
    findings = _blocking(src)
    assert [(f.line_no, f.expression) for f in findings] == [(5, "label")]


def test_binding_in_an_enclosing_scope_is_visible_to_nested_functions() -> None:
    src = (
        "LABEL = sanitize_label(spec.label)\n"
        "def outer(x):\n"
        "    prop = sanitize_label(x)\n"
        "    def inner():\n"
        "        return f'MATCH (n:{LABEL}) WHERE n.{prop} IS NULL RETURN n'\n"
        "    return inner()\n"
    )
    assert _blocking(src) == []


def test_helper_is_validated_only_by_its_own_returns() -> None:
    """A validated return inside a nested def must not certify the outer helper."""
    src = (
        "class S:\n"
        "    def _label(self, job):\n"
        "        def _inner(v):\n"
        "            return sanitize_label(v)\n"
        "        return job.label\n"
        "    def q(self, job):\n"
        "        node_label = self._label(job)\n"
        "        return f'MATCH (f:{node_label}) RETURN f'\n"
    )
    findings = _blocking(src)
    assert [f.expression for f in findings] == ["node_label"]


# ── RU-138 / RG-174: false negatives closed ─────────────────────────────────


def test_raw_overwrite_after_sanitize_is_raw() -> None:
    """reassigned: a later raw binding wins over an earlier validated one."""
    src = (
        "def reassigned(spec):\n"
        "    label = sanitize_label('Safe')\n"
        "    label = spec.target_node\n"
        "    cypher = f'MATCH (n:{label}) RETURN n'\n"
        "    return cypher\n"
    )
    findings = _blocking(src)
    assert [(f.line_no, f.expression) for f in findings] == [(4, "label")]
    assert findings[0].kind.startswith("label / relationship type is not validated")


def test_sanitize_after_raw_binding_passes() -> None:
    """The legitimate inverse: raw then sanitized in the same block is validated."""
    src = (
        "def q(spec):\n"
        "    label = spec.target_node\n"
        "    label = sanitize_label(label)\n"
        "    return f'MATCH (n:{label}) RETURN n'\n"
    )
    assert _blocking(src) == []


def test_conditional_raw_binding_wins_on_conflict() -> None:
    src = (
        "def q(spec, flag):\n"
        "    label = sanitize_label(spec.a)\n"
        "    if flag:\n"
        "        label = spec.b\n"
        "    return f'MATCH (n:{label}) RETURN n'\n"
    )
    assert [f.expression for f in _blocking(src)] == ["label"]


def test_cypher_assigned_to_message_named_variable_is_scanned() -> None:
    """text_named: a message-like name does not hide a query-shaped f-string."""
    src = "def text_named(spec):\n    text = f'MATCH (n:{spec.target_node}) RETURN n'\n    return text\n"
    assert [f.expression for f in _blocking(src)] == ["spec.target_node"]


def test_query_shaped_message_used_only_in_an_exception_is_not_cypher() -> None:
    """A query-shaped error message that only reaches ``raise X(msg)`` is a diagnostic, not Cypher."""
    src = (
        "def check(spec, matches):\n"
        "    if len(matches) != 1:\n"
        "        msg = (\n"
        "            f\"Endpoint '{spec.path}': ontology must declare exactly one edge \"\n"
        '            f"({spec.fromnode})-[:{spec.targetedge}]->({spec.tonode}), found {len(matches)}"\n'
        "        )\n"
        "        raise ValueError(msg)\n"
    )
    assert _blocking(src) == []


def test_query_shaped_message_reaching_a_sink_is_still_scanned() -> None:
    """The diagnostic exemption needs every use to be diagnostic; one sink use keeps it scanned."""
    src = (
        "async def run(driver, spec):\n"
        "    msg = f'MATCH (n:{spec.target_node}) RETURN n'\n"
        "    if spec.strict:\n"
        "        raise ValueError(msg)\n"
        "    return await driver.execute_query(msg)\n"
    )
    assert [f.expression for f in _blocking(src)] == ["spec.target_node"]


def test_shadowed_method_name_does_not_validate_spec_get() -> None:
    """shadow: a same-module ``get`` method does not certify ``spec.get(...)``."""
    src = (
        "class Helper:\n"
        "    def get(self, key):\n"
        "        return 'Literal'\n"
        "\n"
        "\n"
        "def lookup(spec):\n"
        "    cypher = f\"MATCH (n:{spec.get('target_node')}) RETURN n\"\n"
        "    return cypher\n"
    )
    assert [f.expression for f in _blocking(src)] == ["spec.get('target_node')"]


def test_safe_func_still_resolves_for_direct_and_self_calls() -> None:
    src = (
        "def _label(x):\n"
        "    return sanitize_label(x)\n"
        "class S:\n"
        "    def _lbl(self, x):\n"
        "        return sanitize_label(x)\n"
        "    def q(self, spec):\n"
        "        a = _label(spec.a)\n"
        "        b = self._lbl(spec.b)\n"
        "        return f'MATCH (n:{a})-[:{b}]->(m) RETURN n'\n"
    )
    assert _blocking(src) == []


def test_string_concatenation_is_scanned() -> None:
    src = "def concat_case(spec):\n    cypher = 'MATCH (n:' + spec.target_node + ') RETURN n'\n    return cypher\n"
    findings = _blocking(src)
    assert [(f.line_no, f.expression) for f in findings] == [(2, "spec.target_node")]
    assert findings[0].kind.startswith("label / relationship type is not validated")


def test_str_format_is_scanned() -> None:
    src = (
        "def format_case(spec):\n"
        "    cypher = 'MATCH (n:{}) RETURN n'.format(spec.target_node)\n"
        "    other = 'MATCH (n:{lbl}) WHERE n.{0} IS NULL'.format(spec.prop, lbl=spec.label)\n"
        "    return cypher, other\n"
    )
    assert [f.expression for f in _blocking(src)] == ["spec.target_node", "spec.label", "spec.prop"]


def test_percent_format_is_scanned() -> None:
    src = (
        "def percent_case(spec):\n"
        "    cypher = 'MATCH (n:%s) RETURN n' % spec.target_node\n"
        "    pair = 'MATCH (n:%s)-[:%s]->(m)' % (spec.a, spec.b)\n"
        "    return cypher, pair\n"
    )
    assert [f.expression for f in _blocking(src)] == ["spec.target_node", "spec.a", "spec.b"]


def test_str_format_field_access_is_classified_as_the_accessed_value() -> None:
    """RU-138 review: `{0.threshold}` interpolates spec.threshold, not the bare argument."""
    src = (
        "def format_access(spec):\n"
        "    a = 'MATCH (n) WHERE n.score > {0.threshold} RETURN n'.format(spec)\n"
        "    b = 'MATCH (n:{0.target_node}) RETURN n'.format(spec)\n"
        "    c = 'MATCH (n:{0[label]}) RETURN n'.format(spec.labels)\n"
        "    return a, b, c\n"
    )
    findings = _blocking(src)
    assert [f.expression for f in findings] == ["spec.threshold", "spec.target_node", "spec.labels['label']"]
    assert findings[0].kind.startswith("raw domain-spec value")


def test_str_format_field_access_on_a_validated_attribute_passes() -> None:
    src = (
        "class C:\n"
        "    def __init__(self, spec):\n"
        "        self.lbl = sanitize_label(spec.label)\n"
        "    def compile_q(self):\n"
        "        return 'MATCH (n:{0.lbl}) RETURN n'.format(self)\n"
    )
    assert _blocking(src) == []


def test_percent_format_over_a_runtime_mapping_is_scanned() -> None:
    """RU-138 review: `%(key)s % params` interpolates params[key]; it is not dropped."""
    src = (
        "def percent_mapping(params, m, spec):\n"
        "    a = 'MATCH (n) WHERE n.score > %(threshold)s RETURN n' % params\n"
        "    b = 'MATCH (n:%(label)s) RETURN n' % m\n"
        "    c = 'MATCH (n:%(label)s) RETURN n' % {'other': 1, **spec.labels}\n"
        "    return a, b, c\n"
    )
    findings = _blocking(src)
    assert [(f.line_no, f.expression) for f in findings] == [
        (2, "params['threshold']"),
        (3, "m['label']"),
        (4, "spec.labels['label']"),
    ]
    assert findings[0].kind.startswith("raw domain-spec value")


def test_str_format_over_unpacked_mapping_is_scanned() -> None:
    src = (
        "def format_mapping(params, m):\n"
        "    a = 'MATCH (n) WHERE n.score > {threshold} RETURN n'.format(**params)\n"
        "    b = 'MATCH (n:{label}) RETURN n'.format(**m)\n"
        "    return a, b\n"
    )
    assert [f.expression for f in _blocking(src)] == ["params['threshold']", "m['label']"]


def test_mapping_placeholders_with_validated_values_pass() -> None:
    src = (
        "def q(spec):\n"
        "    label = sanitize_label(spec.label)\n"
        "    a = 'MATCH (n:%(label)s) RETURN n' % {'label': label}\n"
        "    b = 'MATCH (n:%(label)s) RETURN n' % {**spec.labels, 'label': label}\n"
        "    c = 'MATCH (n) WHERE n.score > %(t)s RETURN n' % {'t': 0.5}\n"
        "    d = 'MATCH (n:{label}) RETURN n'.format(**{'label': label})\n"
        "    return a, b, c, d\n"
    )
    assert _blocking(src) == []


def test_concat_and_format_with_validated_operands_pass() -> None:
    src = (
        "def q(spec):\n"
        "    label = sanitize_label(spec.label)\n"
        "    a = 'MATCH (n:' + label + ') RETURN n'\n"
        "    b = 'MATCH (n:{}) RETURN n'.format(sanitize_label(spec.x))\n"
        "    c = 'MATCH (n:%s) RETURN n' % label\n"
        "    d = 'WHERE n.score > $' + 'min_score'\n"
        "    return a, b, c, d\n"
    )
    assert _blocking(src) == []


def test_non_cypher_concatenation_and_format_ignored() -> None:
    src = (
        "def key(tenant, spec):\n"
        "    a = 'ceg:' + tenant + ':' + spec.name\n"
        "    b = '{}/{}'.format(tenant, spec.name)\n"
        "    c = 'total %d' % spec.count\n"
        "    return a, b, c, 1 + 2\n"
    )
    assert _blocking(src) == []


def test_unvalidated_bare_value_stays_out_of_scope() -> None:
    """Documented design: a bare non-spec value in a fragment position is not reported."""
    src = (
        "def unvalidated_value(value):\n    cypher = f'MATCH (n) WHERE n.score > {value} RETURN n'\n    return cypher\n"
    )
    assert _blocking(src) == []


def test_concat_waiver_applies_on_operand_line() -> None:
    src = (
        "def q(spec):\n"
        "    return ('MATCH (n:' +\n"
        "        spec.label  # cypher-lint: allow spec-authored label checked upstream\n"
        "        + ') RETURN n')\n"
    )
    findings = scan_source(src, rel_path="engine/x.py")
    assert len(findings) == 1
    assert findings[0].line_no == 3
    assert not findings[0].blocking


# ── waivers are explicit and visible ────────────────────────────────────────


def test_reasoned_waiver_is_reported_but_not_blocking() -> None:
    src = (
        "def compile(self):\n"
        "    pattern = self.spec.pattern\n"
        "    return f'EXISTS {{ MATCH {pattern} }}'  # cypher-lint: allow spec-authored escape hatch\n"
    )
    findings = scan_source(src, rel_path="engine/x.py")
    assert len(findings) == 1
    assert findings[0].waiver == "spec-authored escape hatch"
    assert not findings[0].blocking


def test_waiver_without_a_reason_is_ignored() -> None:
    src = "def compile(self):\n    pattern = self.spec.pattern\n    return f'EXISTS {{ MATCH {pattern} }}'  # cypher-lint: allow\n"
    assert len(_blocking(src)) == 1


# ── tree scan ───────────────────────────────────────────────────────────────


def test_scan_tree_reports_relative_path(tmp_path: Path) -> None:
    target = tmp_path / "engine" / "sync"
    target.mkdir(parents=True)
    (target / "generator.py").write_text(
        "def generate(status):\n    return f\"SET n.status = '{status}'\"\n",
        encoding="utf-8",
    )
    findings = scan_tree(tmp_path)
    assert [f.rel_path for f in findings] == ["engine/sync/generator.py"]
    assert findings[0].line_no == 2
    assert "status" in findings[0].pattern


def test_scan_tree_without_engine_dir_is_empty(tmp_path: Path) -> None:
    assert scan_tree(tmp_path) == []


def test_live_engine_tree_has_no_blocking_findings() -> None:
    """The scanner is the C-009 gate: the checked-in engine must pass it."""
    root = Path(__file__).resolve().parents[2]
    blocking = [f for f in scan_tree(root) if f.blocking]
    assert blocking == [], "\n".join(f"{f.rel_path}:{f.line_no} {f.kind} {{{f.expression}}}" for f in blocking)

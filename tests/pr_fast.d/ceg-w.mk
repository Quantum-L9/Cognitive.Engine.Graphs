# --- L9_META ---
# l9_schema: 1
# origin: engine-specific
# engine: graph
# layer: [test]
# tags: [test, ci, pr-fast]
# owner: engine-team
# status: active
# --- /L9_META ---
#
# Lane CEG-W (campaign EIE-CEG-GATE-COLLAB-2026-11-21): the pull-request
# selection for the graph-inference trigger (A3/A3b) and match acceptance (A6).
# Owned by this lane only. One literal test path per line; see README.md.

PR_FAST_TESTS += tests/unit/test_graph_inference_egress.py
PR_FAST_TESTS += tests/unit/test_first_match_direction.py
PR_FAST_TESTS += tests/unit/test_second_match_direction.py
PR_FAST_TESTS += tests/unit/test_match_gate_row_scope.py
PR_FAST_TESTS += tests/unit/test_match_dimension_projection.py

"""Deterministic CEG outcome replay over recorded transport events (TASK-033).

The replay core is producer-neutral; accepted producer schemas are registered in
``ACCEPTED_INPUT_SCHEMAS`` (today: the Odoo exporter's, ``ODOO_INPUT_SCHEMA``).
"""

from engine.replay.outcome import (
    ACCEPTED_INPUT_SCHEMAS,
    ODOO_INPUT_SCHEMA,
    REPLAY_OUTCOME_SCHEMA,
    ReplayError,
    load_replay_input,
    replay_outcomes,
)

__all__ = [
    "ACCEPTED_INPUT_SCHEMAS",
    "ODOO_INPUT_SCHEMA",
    "REPLAY_OUTCOME_SCHEMA",
    "ReplayError",
    "load_replay_input",
    "replay_outcomes",
]

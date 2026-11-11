<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs]
tags: [requirements]
owner: engine-team
status: active
/L9_META -->

**4 files, 226 lines — P0 blockers cleared.**

***

## File Mapping

| Generated File | Target Path | Purpose |
| :-- | :-- | :-- |
| `pyproject.toml` | `pyproject.toml` | `[project]` runtime deps + `[dependency-groups]` (dev, ci, tools, licenses) |
| `uv.lock` | `uv.lock` | The single lock: every version and sha256, installed by `tools/deps/install_locked.sh` |
| `engine-config-settings.py` | `engine/config/settings.py` | pydantic-settings `Settings` class |
| `engine-api-dependencies.py` | `engine/api/dependencies.py` | FastAPI `Depends()` injection |


***

## How They Wire In

**`settings.py`** reads every env var from `.env.example` — same names, same defaults . Exports a singleton `settings` instance that the rest of the engine imports:

- `neo4j_uri`, `neo4j_username`, `neo4j_password` → consumed by `dependencies.py` to init `GraphDriver`[^1]
- `domains_root` → consumed by `dependencies.py` to init `DomainPackLoader`[^2]
- Scoring weights → declared per domain (`scoring.dimensions[*].defaultweight`); the former engine-wide `w_*` settings were retired (Stage 2)[^3]
- Decay half-lives → match the temporal decay system in `engine-core-modules.py`[^1]

**`dependencies.py`** provides the `Depends()` functions that `match.py`, `sync.py`, and `health.py` already import:[^4]

- `get_graph_driver()` → returns the shared async Neo4j driver
- `get_domain_loader()` → returns the cached domain pack loader
- `startup()` / `shutdown()` → called from `create_app()` lifespan

**`uv.lock`** is the only lock (2026-11-11: it replaced `poetry.lock`, the `requirements*.txt` files and the hashed `requirements-*.lock` files). Edit `pyproject.toml`, run `uv lock`, commit both.
<span style="display:none">[^5]<

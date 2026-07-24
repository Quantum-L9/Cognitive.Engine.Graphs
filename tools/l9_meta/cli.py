"""`l9-meta` command line: check | apply | sync | report | init.

`check` is the point of the whole pipeline. Before it existed, L9_META was
write-only metadata that nothing ever read, so nothing stopped it from drifting.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.l9_meta import adopt, emit
from tools.l9_meta import config as config_mod
from tools.l9_meta.discover import eligible_files
from tools.l9_meta.formats import for_path
from tools.l9_meta.formats.jsonmeta import NotInjectableError
from tools.l9_meta.model import MetaRecord
from tools.l9_meta.resolve import resolve, validate

DEFAULT_EXCLUDES = ["artifacts/"]


@dataclass
class FileStatus:
    rel_path: str
    expected: MetaRecord
    actual: MetaRecord | None
    problems: list[str]
    fields: tuple[str, ...] = ()
    misplaced: bool = False

    @property
    def missing(self) -> bool:
        return self.actual is None

    @property
    def drifted(self) -> bool:
        if self.actual is None:
            return False
        want = self.expected.normalized().as_dict()
        have = self.actual.normalized().as_dict()
        keys = self.fields or tuple(want)
        return any(want.get(k) != have.get(k) for k in keys)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.drifted and not self.problems and not self.misplaced


def scan(cfg: config_mod.Config, fields: tuple[str, ...] = ()) -> list[FileStatus]:
    """Compare every eligible file's header against what config says it should be.

    `fields` narrows the drift comparison. Phase 2 uses it to prove that
    origin/layer/status reproduce exactly while tag normalization is still
    pending in Phase 3.
    """
    out: list[FileStatus] = []
    for rel in eligible_files(cfg.root, cfg.exclude):
        fmt = for_path(rel)
        if fmt is None:
            continue
        expected = resolve(cfg, rel).normalized()
        problems = validate(cfg, expected, rel)
        try:
            text = (cfg.root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            out.append(FileStatus(rel, expected, None, [*problems, f"{rel}: unreadable ({exc})"], fields))
            continue
        try:
            actual = fmt.parse(text)
        except Exception as exc:
            out.append(FileStatus(rel, expected, None, [*problems, f"{rel}: malformed header ({exc})"], fields))
            continue
        # Byte comparison, not just field comparison. A header can hold the right
        # values and still be in the wrong place — a Python header above a leading
        # comment demotes the real docstring and breaks `from __future__`. Only
        # `inject(text) == text` rules that out.
        misplaced = False
        if actual is not None and not fields:
            try:
                misplaced = fmt.inject(text, expected) != text
            except Exception:
                misplaced = True
        out.append(FileStatus(rel, expected, actual, problems, fields, misplaced))
    return out


def cmd_check(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    fields = tuple(args.fields.split(",")) if args.fields else ()
    statuses = scan(cfg, fields)
    missing = [s for s in statuses if s.missing]
    drifted = [s for s in statuses if s.drifted]
    invalid = [s for s in statuses if s.problems]
    misplaced = [s for s in statuses if s.misplaced and not s.drifted]

    for status in invalid:
        for problem in status.problems:
            print(f"INVALID  {problem}")
    for status in drifted:
        print(f"DRIFT    {status.rel_path}")
        if args.verbose:
            _print_diff(status)
    for status in misplaced:
        print(f"MISPLACED  {status.rel_path}")
    for status in missing:
        print(f"MISSING  {status.rel_path}")

    total = len(statuses)
    bad = len({s.rel_path for s in missing + drifted + invalid + misplaced})
    print(
        f"\n{total - bad}/{total} files consistent "
        f"({len(missing)} missing, {len(drifted)} drift, {len(misplaced)} misplaced, {len(invalid)} invalid)"
    )
    return 1 if bad else 0


def _print_diff(status: FileStatus) -> None:
    expected = status.expected.normalized().as_dict()
    actual = (status.actual or status.expected).normalized().as_dict()
    for key in expected:
        if expected[key] != actual.get(key):
            print(f"           {key}: {actual.get(key)!r} -> {expected[key]!r}")


def cmd_apply(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    statuses = scan(cfg)
    blocking = [s for s in statuses if s.problems]
    if blocking and not args.force:
        for status in blocking:
            for problem in status.problems:
                print(f"INVALID  {problem}")
        print("\nRefusing to write while config is invalid. Fix l9-meta.yaml or pass --force.")
        return 1

    changed: list[str] = []
    skipped: list[str] = []
    for status in statuses:
        if status.ok:
            continue
        fmt = for_path(status.rel_path)
        if fmt is None:
            continue
        path = cfg.root / status.rel_path
        try:
            text = path.read_text(encoding="utf-8")
            updated = fmt.inject(text, status.expected)
        except NotInjectableError as exc:
            skipped.append(f"{status.rel_path}: {exc}")
            continue
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append(f"{status.rel_path}: {exc}")
            continue
        if updated == text:
            continue
        changed.append(status.rel_path)
        if not args.dry_run:
            path.write_text(updated, encoding="utf-8")

    verb = "would update" if args.dry_run else "updated"
    for rel in changed:
        print(f"{'PENDING' if args.dry_run else 'WROTE'}  {rel}")
    for note in skipped:
        print(f"SKIP     {note}")
    print(f"\n{verb} {len(changed)} file(s); skipped {len(skipped)}")
    return 0


def cmd_sync(cfg_or_root: config_mod.Config | Path, args: argparse.Namespace) -> int:
    """Regenerate `l9-meta.yaml` from on-disk headers (`--adopt`)."""
    root = cfg_or_root.root if isinstance(cfg_or_root, config_mod.Config) else cfg_or_root
    excludes = cfg_or_root.exclude if isinstance(cfg_or_root, config_mod.Config) else DEFAULT_EXCLUDES
    engine = cfg_or_root.engine if isinstance(cfg_or_root, config_mod.Config) else args.engine

    evidence, files = adopt.collect(root, excludes)
    derivation = adopt.derive(evidence, files)

    vocabulary = None
    if isinstance(cfg_or_root, config_mod.Config):
        vocab = cfg_or_root.vocabulary
        vocabulary = {
            "origin": vocab.origin,
            "status": vocab.status,
            "layer": vocab.layer,
            "tags": {
                "max_tags": vocab.max_tags,
                "family": vocab.tag_family,
                "capability": vocab.tag_capability,
                "concern": vocab.tag_concern,
            },
        }

    text = emit.render(
        engine=engine,
        defaults=derivation.defaults,
        rules=derivation.rules,
        overrides=derivation.overrides,
        exclude=excludes,
        vocabulary=vocabulary,
    )

    out = root / config_mod.CONFIG_NAME
    if args.dry_run:
        print(text)
    else:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")

    print(
        f"\nevidence: {len(evidence)} of {len(files)} eligible files\n"
        f"rules: {len(derivation.rules)}  overrides: {len(derivation.overrides)}\n"
        f"covered by rule: {derivation.covered_by_rule}\n"
        f"no prior evidence (values inferred, need review): {len(derivation.unresolved)}",
        file=sys.stderr,
    )
    return 0


def cmd_report(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    statuses = scan(cfg)
    by_layer: dict[str, int] = {}
    for status in statuses:
        for layer in status.expected.layer:
            by_layer[layer] = by_layer.get(layer, 0) + 1
    print(f"eligible files: {len(statuses)}")
    print(f"with header:    {sum(1 for s in statuses if not s.missing)}")
    print(f"consistent:     {sum(1 for s in statuses if s.ok)}")
    print("\nfiles per layer:")
    for layer, count in sorted(by_layer.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:4d}  {layer}")
    return 0


_INIT_TEMPLATE = """\
# L9_META source of truth for this repo.
#
# Header values are resolved from this file by path — never authored per file.
# To change a file's metadata, change the rule that matches it.
#
#   l9-meta apply    write/repair headers
#   l9-meta check    fail on missing, drifted, misplaced, or invalid
#   l9-meta sync --adopt   regenerate rules from headers already on disk
#
# Precedence, most specific wins: overrides[exact path] -> last matching rule
# -> defaults.

engine: {engine}

defaults:
  origin: engine-specific
  layer: [engine]
  status: active

# Paths never stamped. Generator output belongs here: injecting into it
# guarantees a diff on every build.
exclude:
  - artifacts/

# Closed vocabulary. Every section is opt-in: an empty or absent list means
# that field is unconstrained, which is the right posture while bootstrapping.
# Populate it once the real value set is known — that is what turns `check`
# from a presence test into a correctness test.
vocabulary:
  origin: []
  status: []
  layer: []

  # Tags are three facets, not one flat list. Constraints activate only once
  # at least one facet is non-empty.
  #   family      exactly 1  — which product surface this file serves
  #   capability  0-2        — the specific mechanism it implements
  #   concern     0-2        — cross-cutting properties (security, determinism)
  # A tag naming its own layer or directory carries no information; leave it out.
  tags:
    family: []
    capability: []
    concern: []
    max_tags: 4
    max_per_facet:
      capability: 2
      concern: 2
    # Reject tags used by fewer than this many files. A one-file tag is a
    # comment, not a category, and cannot support a query.
    min_files: 2

# Ordered least- to most-specific; the last match wins. Prefer a rule over an
# override — a rule generalizes to files not written yet.
rules: []

# Per-file escapes for paths a rule cannot express. Keep this short: a growing
# override list means a rule is missing.
overrides: {{}}
"""


def cmd_init(root: Path, args: argparse.Namespace) -> int:
    out = root / config_mod.CONFIG_NAME
    if out.exists() and not args.force:
        print(f"{out} already exists; pass --force to overwrite")
        return 1
    out.write_text(_INIT_TEMPLATE.format(engine=args.engine), encoding="utf-8")
    print(f"wrote {out}\nNext: `l9-meta sync --adopt` to derive rules from existing headers.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="l9-meta", description="L9_META header pipeline")
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="fail if any header is missing, drifted, or invalid")
    p_check.add_argument("-v", "--verbose", action="store_true", help="show field-level drift")
    p_check.add_argument("--fields", default="", help="comma-separated fields to compare (default: all)")

    p_apply = sub.add_parser("apply", help="write headers to match config")
    p_apply.add_argument("--dry-run", action="store_true", help="report without writing")
    p_apply.add_argument("--force", action="store_true", help="write even if config is invalid")

    p_sync = sub.add_parser("sync", help="regenerate l9-meta.yaml from on-disk headers")
    p_sync.add_argument("--adopt", action="store_true", help="required: confirms config regeneration")
    p_sync.add_argument("--dry-run", action="store_true", help="print instead of writing")
    p_sync.add_argument("--engine", default="graph", help="engine id when no config exists yet")

    sub.add_parser("report", help="coverage summary")

    p_init = sub.add_parser("init", help="scaffold l9-meta.yaml in a fresh repo")
    p_init.add_argument("--engine", required=True, help="engine id for this repo")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()

    if args.command == "init":
        return cmd_init(root, args)

    if args.command == "sync":
        if not args.adopt:
            print("sync rewrites l9-meta.yaml; pass --adopt to confirm")
            return 1
        try:
            return cmd_sync(config_mod.load(root), args)
        except config_mod.ConfigError:
            return cmd_sync(root, args)

    try:
        cfg = config_mod.load(root)
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}")
        return 2

    if args.command == "check":
        return cmd_check(cfg, args)
    if args.command == "apply":
        return cmd_apply(cfg, args)
    if args.command == "report":
        return cmd_report(cfg, args)

    print(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

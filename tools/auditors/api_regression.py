"""
--- L9_META ---
l9_schema: 1
origin: l9-template
engine: graph
layer: [audit]
tags: [L9_TEMPLATE, auditors, api]
owner: platform
status: active
--- /L9_META ---

API Regression Auditor — detects breaking changes vs main branch.
Prevents: Rule 7 (constructor signature drift), Rule 8 (payload contract drift)
Bug Classes: A) CLASS_REMOVED (CRITICAL), B) METHOD_REMOVED (CRITICAL),
             C) SIGNATURE_CHANGED (HIGH), D) RETURN_TYPE_CHANGED (HIGH)
"""

import ast
import subprocess

from tools.auditors.base import (
    AuditorScope,
    AuditResult,
    AuditTier,
    BaseAuditor,
    register_auditor,
)


def _run_git(args: list[str], cwd: str | None) -> str | None:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd, timeout=30, check=True)
        return r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError):
        return None


def _extract_public_api(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    api = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
            continue
        methods = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name.startswith("_") and item.name != "__init__":
                    continue
                args = [a.arg for a in item.args.args]
                ret = ast.unparse(item.returns) if item.returns else None
                methods[item.name] = {"args": args, "returns": ret}
        api[node.name] = {"methods": methods, "line": node.lineno}
    return api


@register_auditor
class APIRegressionAuditor(BaseAuditor):
    @property
    def name(self):
        return "api_regression"

    @property
    def domain(self):
        return "universal"

    @property
    def tier(self):
        return AuditTier.REGRESSION

    @property
    def scope(self):
        return AuditorScope(
            include=["engine/**/*.py"],
            exclude=["__pycache__", "tests/"],
            rationale="Public engine API — breaking changes crash downstream callers",
        )

    @property
    def contract_file(self):
        return "docs/contracts/METHOD_SIGNATURES.md"

    @property
    def requires(self):
        return ["git"]

    def scan(self, files, repo_root, index=None, dep_indexes=None):
        result = AuditResult(auditor_name=self.name)
        c = 0
        # Compare against the merge base, not main's tip: a class added on main
        # after this branch was cut is not something this branch removed.
        merge_base = None
        for ref in ("main", "origin/main"):
            out = _run_git(["merge-base", ref, "HEAD"], repo_root)
            if out is not None and out.strip():
                merge_base = out.strip()
                break
        if merge_base is None:
            return result
        diff = _run_git(["diff", "--name-only", merge_base, "HEAD"], repo_root)
        if diff is None:
            return result
        changed = [f for f in diff.strip().split("\n") if f.endswith(".py") and "/tests/" not in f]
        for rp in changed:
            fp = repo_root / rp
            if fp.exists():
                with open(fp) as fh:
                    cur_api = _extract_public_api(fh.read())
            else:
                cur_api = {}  # module deleted on this branch: every base public class is removed
            base = _run_git(["show", f"{merge_base}:{rp}"], repo_root)
            if base is None:
                continue  # file added on this branch: nothing to regress against
            base_api = _extract_public_api(base)
            for cn, bc in base_api.items():
                if cn not in cur_api:
                    c += 1
                    result.add(
                        severity="CRITICAL",
                        code=f"AR-{c:03d}",
                        rule="A",
                        group="api_regression",
                        category="CLASS_REMOVED",
                        message=f"Public class '{cn}' was removed",
                        file=rp,
                        line=bc.get("line", 0),
                        fix_hint=f"Restore '{cn}' or add deprecation shim",
                    )
                    continue
                cc = cur_api[cn]
                for mn, bm in bc["methods"].items():
                    if mn not in cc["methods"]:
                        c += 1
                        result.add(
                            severity="CRITICAL",
                            code=f"AR-{c:03d}",
                            rule="B",
                            group="api_regression",
                            category="METHOD_REMOVED",
                            message=f"Public method '{cn}.{mn}' was removed",
                            file=rp,
                            line=0,
                            fix_hint=f"Restore '{mn}' or add deprecation alias",
                        )
                        continue
                    cm = cc["methods"][mn]
                    if bm["args"] != cm["args"]:
                        c += 1
                        result.add(
                            severity="HIGH",
                            code=f"AR-{c:03d}",
                            rule="C",
                            group="api_regression",
                            category="SIGNATURE_CHANGED",
                            message=f"Signature changed: {cn}.{mn}({', '.join(bm['args'])}) -> ({', '.join(cm['args'])})",
                            file=rp,
                            line=0,
                            fix_hint="Update METHOD_SIGNATURES.md + all callers",
                            suggestions=[f"Old: {bm['args']}"],
                        )
                    if bm["returns"] and cm["returns"] and bm["returns"] != cm["returns"]:
                        c += 1
                        result.add(
                            severity="HIGH",
                            code=f"AR-{c:03d}",
                            rule="D",
                            group="api_regression",
                            category="RETURN_TYPE_CHANGED",
                            message=f"Return type changed: {cn}.{mn} {bm['returns']} -> {cm['returns']}",
                            file=rp,
                            line=0,
                            fix_hint="Update RETURNVALUES.md + all callers",
                        )
        return result

#!/usr/bin/env python3
"""Offline operator publication of explicitly reviewed native CEG packs.

No approval is generated or inferred. The operator supplies a named reviewer,
external approval reference and exact reviewed-byte SHA-256. This is an unsigned
assertion; deployment must independently authorize the writer/reviewer and mount
this store read-only. No compiler, deploy, network or runtime handler is used.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config.loader import (
    DomainApproval,
    DomainCurrent,
    DomainNotFoundError,
    DomainSpecError,
    load_native_revision,
    read_native_file,
    read_native_record,
    validate_native_bytes,
)
from engine.utils.security import tenant_id_defect


def _json_bytes(record: DomainCurrent | DomainApproval) -> bytes:
    return (json.dumps(record.model_dump(by_alias=True), indent=2, sort_keys=True) + "\n").encode()


def _plain_directory(path: Path) -> None:
    """Create operator-owned directories without accepting symlink ancestors."""
    for component in [*reversed(path.absolute().parents), path.absolute()]:
        if component.is_symlink():
            raise DomainSpecError(f"Native store directory is a symlink: {component}")
        if component.exists() and not component.is_dir():
            raise DomainSpecError(f"Native store path is not a directory: {component}")
    path.mkdir(parents=True, exist_ok=True)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _tenant_lock(root: Path, tenant_id: str) -> Iterator[Path]:
    """Serialize offline writers per tenant; readers use only atomic current.json."""
    defect = tenant_id_defect(tenant_id)
    if defect is not None:
        raise DomainSpecError(f"Invalid tenant: {defect}")
    _plain_directory(root)
    folder = root / tenant_id
    _plain_directory(folder)
    fd = os.open(folder / ".publish.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield folder
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _current(root: Path, tenant_id: str) -> DomainCurrent | None:
    path = root / tenant_id / "current.json"
    if not path.exists() and not path.is_symlink():
        return None
    current = read_native_record(path, DomainCurrent)
    spec, _receipt = load_native_revision(root, tenant_id, current.root["sha256"])
    if current.root["tenant_id"] != tenant_id or current.root["version"] != spec.domain.version:
        raise DomainSpecError("Existing current pointer tenant/version mismatch; refuse replacement")
    return current


def _replace_current(folder: Path, pointer: DomainCurrent) -> None:
    """Durable atomic pointer switch; interruption leaves old complete pointer."""
    target = folder / "current.json"
    if target.is_symlink():
        raise DomainSpecError("Native current pointer is a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".current-", suffix=".tmp", dir=folder)
    path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json_bytes(pointer))
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o444)
        os.replace(path, target)  # noqa: PTH105 — explicit atomic switch, fault-injected in tests
        _fsync_dir(folder)
    finally:
        path.unlink(missing_ok=True)


def _write_immutable(path: Path, data: bytes) -> None:
    """Exclusive, fsynced byte write; published revision files are never rewritten."""
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o444)


def _refuse_downgrade(current: DomainCurrent, version: str) -> None:
    old_number, old_stage = current.root["version"].rsplit("-", 1)
    new_number, new_stage = version.rsplit("-", 1)
    old = tuple(int(part) for part in old_number.split("."))
    new = tuple(int(part) for part in new_number.split("."))
    if new < old or (old_stage == "production" and new_stage != "production"):
        raise DomainSpecError("Native publication refuses version/stage downgrade; use explicit rollback")
    if version == current.root["version"]:
        raise DomainSpecError("Changed native bytes require a new version")


def publish_pack(
    spec_path: Path,
    root: Path,
    *,
    tenant_id: str,
    reviewed_by: str,
    approval_ref: str,
    reviewed_input_sha256: str,
    reviewed_at: str | None = None,
) -> dict:
    """Publish exact reviewed bytes after complete owner admission, never relabel.

    Layout: root/tenant/revisions/<bare lowercase sha256>/{spec.yaml,receipt.json}
    and root/tenant/current.json (ceg.domain-current/v1). Return pointer fields.
    reviewed_at defaults to the time the operator's assertion is recorded, not
    a claim that external review identity or audit evidence was authenticated.
    """
    data = read_native_file(spec_path)
    digest = hashlib.sha256(data).hexdigest()
    if reviewed_input_sha256 != digest:
        raise DomainSpecError("Exact reviewed input SHA-256 does not match supplied spec bytes")
    spec = validate_native_bytes(data, tenant_id, complete=True, require_reviewed=True)
    receipt = DomainApproval(
        schema="ceg.domain-approval/v1",
        tenant_id=tenant_id,
        version=spec.domain.version,
        sha256=digest,
        reviewed_input_sha256=reviewed_input_sha256,
        reviewed_by=reviewed_by,
        approval_ref=approval_ref,
        reviewed_at=reviewed_at or datetime.now(tz=UTC).isoformat(),
        previous_sha256=None,
    )
    pointer = DomainCurrent(
        schema="ceg.domain-current/v1", tenant_id=tenant_id, version=spec.domain.version, sha256=digest
    )
    root = root.absolute()
    with _tenant_lock(root, tenant_id) as folder:
        current = _current(root, tenant_id)
        revisions = folder / "revisions"
        _plain_directory(revisions)
        destination = revisions / digest
        if destination.is_symlink():
            raise DomainSpecError("Immutable native revision is a symlink")
        if destination.exists():
            _spec, old_receipt = load_native_revision(root, tenant_id, digest)
            if old_receipt.root["reviewed_by"] != reviewed_by or old_receipt.root["approval_ref"] != approval_ref:
                raise DomainSpecError("Immutable revision already has different review evidence")
            if current is not None and current.root["sha256"] != digest:
                _refuse_downgrade(current, spec.domain.version)
        else:
            if current is not None:
                _refuse_downgrade(current, spec.domain.version)
                receipt = receipt.model_copy(
                    update={"root": {**receipt.root, "previous_sha256": current.root["sha256"]}}
                )
            staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=revisions))
            try:
                _write_immutable(staging / "spec.yaml", data)
                _write_immutable(staging / "receipt.json", _json_bytes(receipt))
                _fsync_dir(staging)
                staging.chmod(0o555)
                os.rename(staging, destination)  # noqa: PTH104 — atomic complete revision installation
                _fsync_dir(revisions)
            finally:
                if staging.exists():
                    staging.chmod(0o755)
                    shutil.rmtree(staging)
        _replace_current(folder, pointer)
    return pointer.model_dump(by_alias=True)


def rollback_pack(root: Path, *, tenant_id: str, sha256: str) -> dict:
    """Select an already reviewed revision after fresh complete admission.

    Original receipt remains byte-identical. This operator action does not
    fabricate a new review or silently repair a tampered current publication.
    """
    root = root.absolute()
    with _tenant_lock(root, tenant_id) as folder:
        current = _current(root, tenant_id)
        if current is None:
            raise DomainSpecError("Rollback requires an existing admitted current revision")
        spec, _receipt = load_native_revision(root, tenant_id, sha256)
        pointer = DomainCurrent(
            schema="ceg.domain-current/v1", tenant_id=tenant_id, version=spec.domain.version, sha256=sha256
        )
        _replace_current(folder, pointer)
    return pointer.model_dump(by_alias=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline operator-only native CEG publication (unsigned review assertion)"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    publish = subparsers.add_parser("publish")
    publish.add_argument("spec_path", type=Path)
    publish.add_argument("--root", type=Path, required=True)
    publish.add_argument("--tenant-id", required=True)
    publish.add_argument("--reviewed-by", required=True, help="Explicit named human reviewer")
    publish.add_argument("--approval-ref", required=True, help="External approval/audit reference")
    publish.add_argument("--reviewed-input-sha256", required=True, help="Exact digest confirmed by the reviewer")
    publish.add_argument("--reviewed-at", help="ISO-8601 review timestamp; otherwise assertion-recorded time")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--root", type=Path, required=True)
    rollback.add_argument("--tenant-id", required=True)
    rollback.add_argument("--sha256", required=True, help="Previously reviewed immutable target digest")
    args = vars(parser.parse_args())
    action = args.pop("action")
    try:
        result = publish_pack(**args) if action == "publish" else rollback_pack(**args)
    except (DomainSpecError, DomainNotFoundError, ValueError, OSError) as exc:
        print(json.dumps({"status": "refused", "message": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

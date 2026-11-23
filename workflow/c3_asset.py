#!/usr/bin/env python3
"""Verify and atomically reconstruct the upload-split c3_main executable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any


MANIFEST_RELATIVE = Path("data") / "c3_main.parts.json"
BLOCK_SIZE = 1024 * 1024


class AssetError(RuntimeError):
    """Raised when the packaged native asset cannot be trusted."""


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AssetError(f"{label} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise AssetError(f"{label} must stay inside the pipeline folder: {value}")
    resolved = (root / relative).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AssetError(f"{label} escapes the pipeline folder: {value}") from exc
    return resolved


def load_manifest(pipeline_root: Path) -> dict[str, Any]:
    root = pipeline_root.resolve(strict=True)
    path = root / MANIFEST_RELATIVE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise AssetError("Unsupported c3_main asset manifest")
    if not isinstance(value.get("bytes"), int) or value["bytes"] <= 0:
        raise AssetError("Manifest bytes must be a positive integer")
    expected_hash = value.get("sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise AssetError("Manifest sha256 must contain 64 hexadecimal characters")
    try:
        int(expected_hash, 16)
    except ValueError as exc:
        raise AssetError("Manifest sha256 is not hexadecimal") from exc
    parts = value.get("parts")
    if not isinstance(parts, list) or len(parts) != 3:
        raise AssetError("c3_main must be packaged as exactly three parts")
    _safe_relative(root, value.get("asset"), "asset")
    return value


def inspect_c3_asset(pipeline_root: Path) -> dict[str, Any]:
    """Verify all chunks without writing a reconstructed executable."""
    root = pipeline_root.resolve(strict=True)
    manifest = load_manifest(root)
    combined = hashlib.sha256()
    part_reports: list[dict[str, Any]] = []
    total = 0
    for index, row in enumerate(manifest["parts"]):
        if not isinstance(row, dict):
            raise AssetError(f"Part {index} is not an object")
        path = _safe_relative(root, row.get("path"), f"parts[{index}].path")
        expected_bytes = row.get("bytes")
        expected_hash = row.get("sha256")
        if not isinstance(expected_bytes, int) or expected_bytes <= 0:
            raise AssetError(f"Part {index} bytes must be a positive integer")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise AssetError(f"Part {index} sha256 is invalid")
        if not path.is_file() or path.is_symlink():
            raise AssetError(f"Required c3_main part is missing or unsafe: {path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(BLOCK_SIZE), b""):
                size += len(block)
                digest.update(block)
                combined.update(block)
        actual_hash = digest.hexdigest()
        if size != expected_bytes or actual_hash != expected_hash.lower():
            raise AssetError(f"c3_main part failed integrity validation: {path}")
        total += size
        part_reports.append({"path": str(path), "bytes": size, "sha256": actual_hash})
    combined_hash = combined.hexdigest()
    if total != manifest["bytes"] or combined_hash != manifest["sha256"].lower():
        raise AssetError("Combined c3_main parts failed integrity validation")
    return {
        "ready": True,
        "asset": manifest["asset"],
        "bytes": total,
        "sha256": combined_hash,
        "parts": part_reports,
    }


def _valid_materialized(path: Path, expected_bytes: int, expected_hash: str) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size == expected_bytes
        and _hash_file(path) == expected_hash
    )


def materialize_c3_main(pipeline_root: Path, destination: Path) -> dict[str, Any]:
    """Create a verified executable at destination without editing the pipeline folder."""
    root = pipeline_root.resolve(strict=True)
    manifest = load_manifest(root)
    report = inspect_c3_asset(root)
    target = destination.expanduser().resolve(strict=False)
    expected_hash = manifest["sha256"].lower()
    expected_bytes = manifest["bytes"]
    if target.exists() or target.is_symlink():
        if not _valid_materialized(target, expected_bytes, expected_hash):
            raise AssetError(f"Refusing to replace an invalid existing runtime asset: {target}")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return {**report, "path": str(target), "reused": True}

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as output:
            for row in manifest["parts"]:
                part = _safe_relative(root, row["path"], "part.path")
                with part.open("rb") as source:
                    for block in iter(lambda: source.read(BLOCK_SIZE), b""):
                        output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if not _valid_materialized(temporary, expected_bytes, expected_hash):
            raise AssetError("Reconstructed c3_main failed integrity validation")
        if target.exists() or target.is_symlink():
            if not _valid_materialized(target, expected_bytes, expected_hash):
                raise AssetError(f"Concurrent runtime asset is invalid: {target}")
            temporary.unlink()
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return {**report, "path": str(target), "reused": True}
        os.replace(temporary, target)
        target.chmod(0o755)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {**report, "path": str(target), "reused": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-root", required=True)
    parser.add_argument("--destination")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.pipeline_root)
    try:
        if args.verify_only:
            result = inspect_c3_asset(root)
        else:
            if not args.destination:
                raise AssetError("--destination is required unless --verify-only is used")
            result = materialize_c3_main(root, Path(args.destination))
        result["status"] = "ready"
        exit_code = 0
    except (AssetError, OSError) as exc:
        result = {"status": "blocked", "ready": False, "error": str(exc)}
        exit_code = 1
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

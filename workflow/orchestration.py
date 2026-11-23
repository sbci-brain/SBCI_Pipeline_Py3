#!/usr/bin/env python3
"""Plan safe handoffs between isolated CyberNeuro pipeline skills."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

from workflow.preprocessing_executor import (
    ensure_work_subject,
    make_writable,
    execution_lock,
    sha256_file,
    write_json_atomic,
)
from workflow.workflow_executor import stage_output_paths, validate_stage_outputs


HANDOFF_SCHEMA_VERSION = "1.0"
ORCHESTRATION_MODES = ("manual_shared_artifacts",)

# These are candidates visible in the Wanda Longleaf pipeline list. A name in
# this table is never evidence that its outputs satisfy SBCI's file contract.
STAGE_PROVIDERS: dict[str, tuple[dict[str, str], ...]] = {
    "dwi_correction": (
        {"skill": "FSL", "compatibility": "candidate"},
        {"skill": "Brain MRI Data Preprocessing Pipelines", "compatibility": "candidate"},
    ),
    "fmri_correction": (
        {"skill": "FSL", "compatibility": "candidate"},
        {"skill": "Brain MRI Data Preprocessing Pipelines", "compatibility": "candidate"},
    ),
    "prepare_data": (
        {"skill": "Brain MRI Data Preprocessing Pipelines", "compatibility": "candidate"},
    ),
    "t1_dwi_registration": (
        {"skill": "ANTs", "compatibility": "candidate"},
        {"skill": "Brain MRI Data Preprocessing Pipelines", "compatibility": "candidate"},
    ),
    "freesurfer": (
        {"skill": "FreeSurfer", "compatibility": "candidate"},
        {"skill": "Brain MRI Data Preprocessing Pipelines", "compatibility": "candidate"},
    ),
    "fodf_tissue": (
        {"skill": "Brain MRI Data Preprocessing Pipelines", "compatibility": "candidate"},
    ),
    "fsfast": (
        {"skill": "FreeSurfer", "compatibility": "candidate"},
        {"skill": "Brain MRI Data Preprocessing Pipelines", "compatibility": "candidate"},
    ),
    "psc": (),
    "sbci": ({"skill": "SBCI HPC-first pipeline skill", "compatibility": "native"},),
}

STAGE_REQUIRED_ROLES: dict[str, tuple[str, ...]] = {
    "dwi_correction": ("correctedDwi", "rotatedBvec", "bval", "fieldHz", "hifiB0"),
    "fmri_correction": ("correctedBold",),
    "prepare_data": ("t1", "dwi", "bval", "bvec"),
    "t1_dwi_registration": ("fa", "md", "t1Warped", "t1WholebrainWarped", "fodfDwi"),
    # `parcellation` is the unwarped aparc.a2009s+aseg image. SBCI's own input
    # check, the subject metadata probe, and the sbciReady gate all require it,
    # so a freesurfer handoff that omits it cannot reach the scientific stage.
    # SBCI's own input check (FS_SURFACES in local_pipeline) requires ten
    # FreeSurfer surfaces, not just the white ones, plus the unwarped
    # aparc.a2009s+aseg parcellation that the sbciReady gate reads.
    "freesurfer": (
        "lhWhite", "rhWhite", "wmparc", "lhDestrieux", "rhDestrieux",
        "warpedParcellation", "parcellation",
        "lhPial", "rhPial", "lhSphere", "rhSphere",
        "lhSphereReg", "rhSphereReg", "lhInflated", "rhInflated",
    ),
    "fodf_tissue": ("fodf", "wm", "gm", "csf"),
    "fsfast": ("lhResidual", "rhResidual", "mniResidual"),
    "psc": ("pscConnectome",),
    "sbci": ("structuralConnectome", "functionalConnectome"),
}


def _error(code: str, message: str, path: Path | None = None) -> dict[str, str]:
    row = {"severity": "error", "code": code, "message": message}
    if path is not None:
        row["path"] = str(path)
    return row


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def handoff_template() -> dict[str, Any]:
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "handoff_id": "replace-with-a-unique-run-id",
        "producer": {
            "skill": "Brain MRI Data Preprocessing Pipelines",
            "stage": "freesurfer",
            "environment": "isolated-producer-container",
        },
        "subject": "sub-XXXX",
        "session": "ses-YYYYMMDD",
        "status": "complete",
        "read_only": True,
        "artifact_root": ".",
        "artifacts": [
            {"role": "lhWhite", "path": "sub-XXXX/ses-YYYYMMDD/dwi_pipeline/structure/t1_freesurfer/surf/lh.white"}
        ],
        "provenance": {"command": "", "container": "", "completed_at": ""},
    }


def load_handoff(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [_error("invalid_handoff_json", str(exc), path)]
    if not isinstance(value, dict):
        return None, [_error("invalid_handoff", "Handoff manifest must contain a JSON object.", path)]
    return value, []


def validate_handoff(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    *,
    shared_work_dir: Path | None = None,
    verify_hashes: bool = False,
) -> dict[str, Any]:
    """Validate metadata and artifacts without modifying or importing them."""
    issues: list[dict[str, str]] = []
    if manifest.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        issues.append(_error("unsupported_handoff_schema", f"schema_version must be {HANDOFF_SCHEMA_VERSION}", manifest_path))
    if not isinstance(manifest.get("handoff_id"), str) or not manifest.get("handoff_id"):
        issues.append(_error("missing_handoff_id", "handoff_id must be a non-empty string.", manifest_path))
    producer = manifest.get("producer")
    stage = producer.get("stage") if isinstance(producer, dict) else None
    if not isinstance(producer, dict):
        issues.append(_error("invalid_handoff_producer", "producer must be an object.", manifest_path))
    else:
        for key in ("skill", "environment"):
            if not isinstance(producer.get(key), str) or not producer.get(key):
                issues.append(_error("invalid_handoff_producer", f"producer.{key} must be a non-empty string.", manifest_path))
    if stage not in STAGE_REQUIRED_ROLES:
        issues.append(_error("invalid_handoff_stage", "producer.stage is missing or unsupported.", manifest_path))
    if not isinstance(manifest.get("subject"), str) or not manifest.get("subject"):
        issues.append(_error("missing_handoff_subject", "subject must be a non-empty string.", manifest_path))
    if manifest.get("status") != "complete":
        issues.append(_error("handoff_not_complete", "Only a completed producer run can be handed to SBCI.", manifest_path))
    if manifest.get("read_only") is not True:
        issues.append(_error("handoff_not_read_only", "Handoff artifacts must be declared read_only=true.", manifest_path))
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or any(not isinstance(provenance.get(key), str) for key in ("command", "container", "completed_at")):
        issues.append(_error("invalid_handoff_provenance", "provenance must contain string command, container, and completed_at fields.", manifest_path))

    configured_root = manifest.get("artifact_root", ".")
    if not isinstance(configured_root, str):
        configured_root = "."
        issues.append(_error("invalid_artifact_root", "artifact_root must be a string.", manifest_path))
    root = shared_work_dir.resolve() if shared_work_dir is not None else (manifest_path.parent / configured_root).resolve()
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
        issues.append(_error("invalid_artifacts", "artifacts must be a list.", manifest_path))

    seen_roles: set[str] = set()
    checked: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            issues.append(_error("invalid_artifact", f"artifacts[{index}] must be an object.", manifest_path))
            continue
        role = artifact.get("role")
        relative = artifact.get("path")
        if not isinstance(role, str) or not role:
            issues.append(_error("invalid_artifact_role", f"artifacts[{index}].role is required.", manifest_path))
            continue
        if role in seen_roles:
            issues.append(_error("duplicate_artifact_role", f"Artifact role is duplicated: {role}", manifest_path))
        if not isinstance(relative, str) or not relative:
            issues.append(_error("invalid_artifact_path", f"artifacts[{index}].path is required.", manifest_path))
            continue
        rel_path = Path(relative)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            issues.append(_error("unsafe_artifact_path", f"Artifact path must be relative and cannot contain '..': {relative}", manifest_path))
            continue
        candidate = (root / rel_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            issues.append(_error("unsafe_artifact_path", f"Artifact escapes the shared work directory: {relative}", candidate))
            continue
        seen_roles.add(role)
        exists = candidate.is_file()
        size = candidate.stat().st_size if exists else 0
        if not exists or size <= 0:
            issues.append(_error("missing_handoff_artifact", f"Artifact is missing or empty: {role}", candidate))
        expected_size = artifact.get("size")
        if expected_size is not None:
            if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
                issues.append(_error("invalid_artifact_size", f"Artifact size must be a positive integer for {role}.", manifest_path))
            elif exists and expected_size != size:
                issues.append(_error("handoff_size_mismatch", f"Artifact size mismatch for {role}.", candidate))
        expected_hash = artifact.get("sha256")
        hash_ok: bool | None = None
        actual_hash: str | None = None
        if expected_hash is not None:
            valid_hash = isinstance(expected_hash, str) and len(expected_hash) == 64 and all(char in "0123456789abcdefABCDEF" for char in expected_hash)
            if not valid_hash:
                issues.append(_error("invalid_artifact_hash", f"Artifact SHA-256 is malformed for {role}.", manifest_path))
            elif exists and verify_hashes:
                actual_hash = _sha256(candidate).lower()
                hash_ok = actual_hash == expected_hash.lower()
                if not hash_ok:
                    issues.append(_error("handoff_hash_mismatch", f"Artifact SHA-256 mismatch for {role}.", candidate))
        checked.append({
            "role": role,
            "path": str(candidate),
            "nonempty": exists and size > 0,
            "size": size,
            "hashVerified": hash_ok,
            "sha256": actual_hash,
        })

    if stage in STAGE_REQUIRED_ROLES:
        for role in STAGE_REQUIRED_ROLES[stage]:
            if role not in seen_roles:
                issues.append(_error("missing_artifact_role", f"Stage {stage} requires artifact role {role}.", manifest_path))
    return {
        "ready": not issues,
        "manifest": str(manifest_path),
        "artifactRoot": str(root),
        "producerStage": stage,
        "artifacts": checked,
        "issues": issues,
        "imported": False,
        "note": "Validation never copies, moves, or edits producer artifacts.",
    }


def consume_handoff(
    manifest_path: Path,
    subject: Path,
    output_dir: Path,
    *,
    shared_work_dir: Path | None = None,
) -> dict[str, Any]:
    """Copy one validated producer stage into the isolated SBCI work tree."""
    manifest_path = manifest_path.expanduser().resolve()
    subject = subject.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    manifest, load_issues = load_handoff(manifest_path)
    if manifest is None:
        raise RuntimeError(load_issues[0]["message"] if load_issues else "Handoff manifest could not be loaded.")
    validation = validate_handoff(
        manifest,
        manifest_path,
        shared_work_dir=shared_work_dir,
        verify_hashes=True,
    )
    expected_subject = next((part for part in subject.parts if part.startswith("sub-")), subject.name)
    expected_session = next((part for part in subject.parts if part.startswith("ses-")), None)
    compatibility_issues = validation.setdefault("issues", [])
    if manifest.get("subject") != expected_subject:
        compatibility_issues.append(_error(
            "handoff_subject_mismatch",
            f"The handoff subject {manifest.get('subject')!r} does not match {expected_subject!r}.",
            manifest_path,
        ))
    if expected_session is not None and manifest.get("session") != expected_session:
        compatibility_issues.append(_error(
            "handoff_session_mismatch",
            f"The handoff session {manifest.get('session')!r} does not match {expected_session!r}.",
            manifest_path,
        ))
    if compatibility_issues:
        codes = ", ".join(str(row.get("code")) for row in compatibility_issues)
        raise RuntimeError(f"Handoff validation failed: {codes}")

    stage = str(validation.get("producerStage") or "")
    with execution_lock(output_dir):
        work_subject, provenance, created = ensure_work_subject(subject, output_dir)
        try:
            targets = stage_output_paths(stage, work_subject)
        except ValueError as exc:
            raise RuntimeError(f"Handoff stage cannot be consumed into an SBCI work tree: {stage}") from exc
        sources = {str(row["role"]): row for row in validation["artifacts"]}
        required = STAGE_REQUIRED_ROLES.get(stage, ())
        pairs: list[tuple[str, Path, Path, str, bool]] = []
        for role in required:
            source_record = sources[role]
            source = Path(str(source_record["path"]))
            target = targets.get(role)
            if target is None:
                raise RuntimeError(f"No canonical SBCI work-tree path is defined for handoff role {role}.")
            verified_hash = source_record.get("sha256") if source_record.get("hashVerified") is True else None
            source_hash = str(verified_hash) if verified_hash else sha256_file(source)
            identical = target.is_file() and target.stat().st_size > 0 and sha256_file(target) == source_hash
            if target.exists() and not identical:
                raise RuntimeError(f"Existing work-tree artifact conflicts with handoff role {role}: {target}")
            pairs.append((role, source, target, source_hash, identical))

        created_targets: list[Path] = []
        imported: list[dict[str, Any]] = []
        try:
            for role, source, target, source_hash, identical in pairs:
                if not identical:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open("rb") as input_stream, target.open("xb") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                    shutil.copystat(source, target, follow_symlinks=True)
                    make_writable(target)
                    created_targets.append(target)
                destination_hash = sha256_file(target)
                if destination_hash != source_hash:
                    raise RuntimeError(f"Copied handoff artifact failed SHA-256 verification for role {role}.")
                imported.append({
                    "role": role,
                    "source": str(source),
                    "destination": str(target),
                    "sha256": source_hash,
                    "size": target.stat().st_size,
                    "copied": not identical,
                })
            output_validation = validate_stage_outputs(stage, work_subject)
            if not output_validation.get("ready"):
                raise RuntimeError(f"Consumed handoff did not satisfy the {stage} output contract.")
            marker = output_dir / "files" / "preprocessing_work" / "state" / f"{stage}.json"
            marker_value = {
                "schemaVersion": "1.0",
                "stage": stage,
                "state": "complete",
                "completionKind": "validated_handoff_copy",
                "handoffId": manifest["handoff_id"],
                "handoffManifest": str(manifest_path),
                "producer": manifest["producer"],
                "provenance": manifest["provenance"],
                "artifacts": imported,
                "completedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            write_json_atomic(marker, marker_value)
        except Exception:
            for target in reversed(created_targets):
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            raise
    return {
        "stage": stage,
        "state": "complete",
        "workSubject": str(work_subject),
        "workTreeCreated": created,
        "sourceProvenance": str(output_dir / "files" / "preprocessing_work" / "source_provenance.json"),
        "marker": str(marker),
        "artifacts": imported,
        "validation": output_validation,
        "producerArtifactsModified": False,
    }


def build_orchestration_plan(
    subject: Path | None,
    inventory: Mapping[str, Any] | None,
    workflow_plan: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = config if isinstance(config, Mapping) else {}
    mode = config.get("mode", "manual_shared_artifacts")
    shared_value = config.get("shared_work_dir", "")
    manifest_value = config.get("handoff_manifest", "")
    verify_hashes = config.get("verify_hashes", False) is True
    consume_requested = config.get("consume_handoff", False) is True
    shared = Path(shared_value).expanduser() if isinstance(shared_value, str) and shared_value.strip() else None
    if shared is not None and not shared.is_absolute() and subject is not None:
        shared = subject / shared
    manifest_path = Path(manifest_value).expanduser() if isinstance(manifest_value, str) and manifest_value.strip() else None
    if manifest_path is not None and not manifest_path.is_absolute() and subject is not None:
        manifest_path = subject / manifest_path

    stages = workflow_plan.get("stages", []) if isinstance(workflow_plan, Mapping) else []
    next_stage = next((row for row in stages if isinstance(row, dict) and row.get("state") != "complete"), None)
    stage_id = next_stage.get("id") if isinstance(next_stage, dict) else None
    providers = [dict(row) for row in STAGE_PROVIDERS.get(str(stage_id), ())]
    handoff_validation: dict[str, Any] | None = None
    if manifest_path is not None:
        loaded, load_issues = load_handoff(manifest_path)
        handoff_validation = (
            validate_handoff(loaded, manifest_path, shared_work_dir=shared, verify_hashes=verify_hashes)
            if loaded is not None
            else {"ready": False, "manifest": str(manifest_path), "issues": load_issues, "imported": False}
        )
        if loaded is not None and handoff_validation is not None:
            compatibility_issues = handoff_validation.setdefault("issues", [])
            if stage_id and handoff_validation.get("producerStage") != stage_id:
                compatibility_issues.append(_error(
                    "handoff_stage_mismatch",
                    f"The handoff produced {handoff_validation.get('producerStage')}; the next required stage is {stage_id}.",
                    manifest_path,
                ))
            if subject is not None:
                expected_subject = next((part for part in subject.parts if part.startswith("sub-")), subject.name)
                expected_session = next((part for part in subject.parts if part.startswith("ses-")), None)
                if loaded.get("subject") != expected_subject:
                    compatibility_issues.append(_error(
                        "handoff_subject_mismatch",
                        f"The handoff subject {loaded.get('subject')!r} does not match {expected_subject!r}.",
                        manifest_path,
                    ))
                if expected_session is not None and loaded.get("session") != expected_session:
                    compatibility_issues.append(_error(
                        "handoff_session_mismatch",
                        f"The handoff session {loaded.get('session')!r} does not match {expected_session!r}.",
                        manifest_path,
                    ))
            handoff_validation["ready"] = not compatibility_issues

    completed_connectivity = bool(inventory and inventory.get("completedConnectivity"))
    if completed_connectivity:
        state = "completed_connectivity_read_only"
        action = "Validate or analyze the existing matrices; do not rerun preprocessing or SBCI."
    elif handoff_validation and handoff_validation.get("ready"):
        state = "handoff_validated"
        action = (
            "An approved preprocess invocation will copy this one validated producer stage into the isolated SBCI work tree."
            if consume_requested
            else "The handoff is valid but remains read-only; set consume_handoff=true in an approved preprocess invocation to copy exactly one stage."
        )
    elif stage_id == "sbci" and next_stage and next_stage.get("state") == "pending":
        state = "sbci_ready"
        action = "Run SBCI in its isolated environment after approval and metadata validation."
    elif providers:
        state = "external_skill_candidate"
        action = "Run one candidate producer skill, save outputs in the shared directory, then create and validate a handoff manifest."
    elif stage_id:
        state = "local_or_manual_stage"
        action = "Use the SBCI skill's configured stage adapter or supply a validated handoff."
    else:
        state = "workflow_complete"
        action = "No incomplete workflow stage was found."

    return {
        "mode": mode,
        "state": state,
        "sharedWorkDir": str(shared.resolve()) if shared is not None else None,
        "nextStage": stage_id,
        "candidateProviders": providers,
        "requiredArtifactRoles": list(STAGE_REQUIRED_ROLES.get(str(stage_id), ())),
        "handoffManifest": str(manifest_path) if manifest_path is not None else None,
        "handoffValidation": handoff_validation,
        "handoffConsumptionRequested": consume_requested,
        "automaticSkillInvocation": False,
        "invocationReason": "Wanda confirms sequential skills can share outputs, but no documented cross-skill invocation API has been provided.",
        "environmentPolicy": "Each producer keeps its own environment/container; only declared read-only artifacts cross the boundary.",
        "action": action,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--shared-work-dir", type=Path)
    parser.add_argument("--verify-hashes", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    value, issues = load_handoff(args.manifest)
    result = (
        validate_handoff(value, args.manifest, shared_work_dir=args.shared_work_dir, verify_hashes=args.verify_hashes)
        if value is not None
        else {"ready": False, "issues": issues}
    )
    print(json.dumps(result, indent=2 if args.pretty else None, ensure_ascii=False))
    return 0 if result.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())

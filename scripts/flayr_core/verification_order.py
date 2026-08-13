"""Evidence-backed gate for the frozen evaluation execution order."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .run_manifest import SUCCESS_MANIFEST_NAME, validate_success_manifest


VERIFICATION_ORDER = (
    "fixture",
    "offline_replay",
    "fake_provider",
    "ordinary_sample",
    "boundary_sample",
)
PRODUCTION_STAGE = "production"
MARKER_SCHEMA_VERSION = 4
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class VerificationOrderError(ValueError):
    """Raised when verification evidence is missing, stale, or out of order."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(body.encode("utf-8"))


def _git(repo_root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise VerificationOrderError(f"cannot inspect verification source state: {detail}")
    return completed.stdout


def _source_proof(repo_root: Path) -> tuple[str, str]:
    repo_root = repo_root.expanduser().resolve()
    commit = _git(repo_root, "rev-parse", "HEAD").decode("ascii").strip().lower()
    names = [
        item.decode("utf-8", errors="surrogateescape")
        for item in _git(repo_root, "ls-files", "-co", "--exclude-standard", "-z").split(b"\0")
        if item
    ]
    digest = hashlib.sha256()
    for relative in sorted(names):
        path = (repo_root / relative).resolve()
        try:
            path.relative_to(repo_root)
        except ValueError as exc:
            raise VerificationOrderError(f"source file escapes repository: {relative}") from exc
        if not path.is_file():
            continue
        encoded = relative.encode("utf-8", errors="surrogateescape")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(_sha256_file(path)))
    return commit, digest.hexdigest()


def _source_commit_short(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "--short", "HEAD").decode("ascii").strip().lower()


def _read_marker(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationOrderError(f"verification marker is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise VerificationOrderError(f"verification marker must be an object: {path}")
    return value


def _verify_marker(
    root: Path,
    stage: str,
    *,
    repo_root: Path,
    expected_predecessor_sha256: str | None,
) -> str:
    marker_path = root / f"{stage}.json"
    marker = _read_marker(marker_path)
    if (
        marker.get("schema_version") != MARKER_SCHEMA_VERSION
        or marker.get("stage") != stage
        or marker.get("status") != "passed"
    ):
        raise VerificationOrderError(f"verification prerequisite {stage} is not passed")
    proof = marker.get("proof")
    if not isinstance(proof, dict):
        raise VerificationOrderError(f"verification prerequisite {stage} has no proof")
    proof_sha256 = str(proof.get("proof_sha256") or "")
    proof_body = {key: value for key, value in proof.items() if key != "proof_sha256"}
    if proof_sha256 != _canonical_sha256(proof_body):
        raise VerificationOrderError(f"verification prerequisite {stage} proof was tampered")
    if proof.get("predecessor_marker_sha256") != expected_predecessor_sha256:
        raise VerificationOrderError(f"verification prerequisite {stage} is not hash-chained")
    source_commit, source_state_sha256 = _source_proof(repo_root)
    if proof.get("source_commit") != source_commit or proof.get("source_state_sha256") != source_state_sha256:
        raise VerificationOrderError(f"verification prerequisite {stage} is stale for current source")
    execution_relative = str(proof.get("execution_record") or "")
    execution_path = (root / execution_relative).resolve()
    try:
        execution_path.relative_to(root)
    except ValueError as exc:
        raise VerificationOrderError(f"verification prerequisite {stage} execution record escapes root") from exc
    if not execution_path.is_file() or proof.get("execution_sha256") != _sha256_file(execution_path):
        raise VerificationOrderError(f"verification prerequisite {stage} execution record is invalid")
    execution = _read_marker(execution_path)
    if execution.get("stage") != stage or execution.get("returncode") != 0:
        raise VerificationOrderError(f"verification prerequisite {stage} did not execute successfully")
    evidence = proof.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise VerificationOrderError(f"verification prerequisite {stage} has no evidence files")
    for item in evidence:
        if not isinstance(item, dict):
            raise VerificationOrderError(f"verification prerequisite {stage} evidence is malformed")
        evidence_path = (root / str(item.get("path") or "")).resolve()
        try:
            evidence_path.relative_to(root)
        except ValueError as exc:
            raise VerificationOrderError(f"verification prerequisite {stage} evidence escapes root") from exc
        if not evidence_path.is_file() or evidence_path.stat().st_size <= 0:
            raise VerificationOrderError(f"verification prerequisite {stage} evidence is missing")
        if item.get("sha256") != _sha256_file(evidence_path):
            raise VerificationOrderError(f"verification prerequisite {stage} evidence was tampered")
    if stage in {"ordinary_sample", "boundary_sample"}:
        completed_run = proof.get("completed_run")
        if not isinstance(completed_run, dict):
            raise VerificationOrderError(f"verification prerequisite {stage} has no completed run proof")
        run_dir = Path(str(completed_run.get("path") or "")).expanduser().resolve()
        manifest_path = run_dir / SUCCESS_MANIFEST_NAME
        if (
            not validate_success_manifest(
                run_dir,
                expected_provenance={"code_commit": _source_commit_short(repo_root)},
            )
            or not manifest_path.is_file()
            or completed_run.get("success_manifest_sha256") != _sha256_file(manifest_path)
        ):
            raise VerificationOrderError(
                f"verification prerequisite {stage} completed run is invalid or stale"
            )
    return _sha256_file(marker_path)


def assert_verification_order(
    root: Path,
    stage: str,
    *,
    repo_root: Path = REPOSITORY_ROOT,
) -> None:
    normalized = str(stage or "").strip().lower()
    if normalized not in VERIFICATION_ORDER:
        raise VerificationOrderError(f"unknown verification stage: {stage}")
    root = root.expanduser().resolve()
    predecessor_sha256: str | None = None
    for prerequisite in VERIFICATION_ORDER[: VERIFICATION_ORDER.index(normalized)]:
        predecessor_sha256 = _verify_marker(
            root,
            prerequisite,
            repo_root=repo_root,
            expected_predecessor_sha256=predecessor_sha256,
        )


def run_verification_stage(
    root: Path,
    stage: str,
    *,
    command: Sequence[str],
    evidence_paths: Sequence[Path],
    completed_run_dir: Path | None = None,
    repo_root: Path = REPOSITORY_ROOT,
    cwd: Path | None = None,
) -> Path:
    """Execute one verifier and mint a marker only from its changed evidence."""
    repo_root = repo_root.expanduser().resolve()
    normalized = str(stage or "").strip().lower()
    if normalized not in VERIFICATION_ORDER:
        raise VerificationOrderError(f"unknown verification stage: {stage}")
    if not command or not all(isinstance(item, str) and item for item in command):
        raise VerificationOrderError("verification command must be a non-empty string sequence")
    requires_completed_run = normalized in {"ordinary_sample", "boundary_sample"}
    if requires_completed_run and completed_run_dir is None:
        raise VerificationOrderError(
            f"verification stage {normalized} requires --run-dir for semantic completion proof"
        )
    completed_manifest_before: str | None = None
    completed_manifest_path: Path | None = None
    if completed_run_dir is not None:
        completed_run_path = completed_run_dir.expanduser().resolve()
        try:
            completed_run_path.relative_to(repo_root)
        except ValueError:
            pass
        else:
            raise VerificationOrderError("completed verification run must be outside the source repository")
        completed_manifest_path = completed_run_path / SUCCESS_MANIFEST_NAME
        if completed_manifest_path.is_file():
            completed_manifest_before = _sha256_file(completed_manifest_path)
    root = root.expanduser().resolve()
    try:
        root.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise VerificationOrderError("verification root must be outside the source repository")
    root.mkdir(parents=True, exist_ok=True)
    assert_verification_order(root, normalized, repo_root=repo_root)
    evidence: list[Path] = []
    before: dict[Path, str | None] = {}
    for raw_path in evidence_paths:
        path = raw_path.expanduser().resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise VerificationOrderError("verification evidence must be inside verification root") from exc
        evidence.append(path)
        before[path] = _sha256_file(path) if path.is_file() else None
    if not evidence:
        raise VerificationOrderError("verification stage requires at least one evidence path")

    stdout_path = root / f"{normalized}.stdout.log"
    stderr_path = root / f"{normalized}.stderr.log"
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        completed = subprocess.run(
            list(command),
            cwd=str((cwd or repo_root).expanduser().resolve()),
            check=False,
            stdout=stdout_file,
            stderr=stderr_file,
        )
    execution = {
        "schema_version": 1,
        "stage": normalized,
        "returncode": completed.returncode,
        "command_sha256": _canonical_sha256(list(command)),
        "stdout_sha256": _sha256_file(stdout_path),
        "stderr_sha256": _sha256_file(stderr_path),
    }
    execution_path = root / f"{normalized}.execution.json"
    execution_path.write_text(
        json.dumps(execution, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise VerificationOrderError(
            f"verification stage {normalized} failed with exit code {completed.returncode}"
        )

    completed_run_proof: dict[str, str] | None = None
    if requires_completed_run:
        assert completed_run_dir is not None
        run_dir = completed_run_dir.expanduser().resolve()
        manifest_path = run_dir / SUCCESS_MANIFEST_NAME
        if (
            not validate_success_manifest(
                run_dir,
                expected_provenance={"code_commit": _source_commit_short(repo_root)},
            )
            or not manifest_path.is_file()
        ):
            raise VerificationOrderError(
                f"verification stage {normalized} command exited successfully but run is not completed"
            )
        manifest_sha256 = _sha256_file(manifest_path)
        if manifest_sha256 == completed_manifest_before:
            raise VerificationOrderError(
                f"verification stage {normalized} completed run was not produced by this command"
            )
        completed_run_proof = {
            "path": str(run_dir),
            "success_manifest_sha256": manifest_sha256,
        }

    evidence_manifest: list[dict[str, str]] = []
    for path in evidence:
        if not path.is_file() or path.stat().st_size <= 0:
            raise VerificationOrderError(f"verification evidence was not produced: {path}")
        digest = _sha256_file(path)
        if before[path] == digest:
            raise VerificationOrderError(f"verification evidence was not changed by the command: {path}")
        evidence_manifest.append({"path": str(path.relative_to(root)), "sha256": digest})

    predecessor_sha256 = None
    index = VERIFICATION_ORDER.index(normalized)
    if index:
        predecessor_sha256 = _sha256_file(root / f"{VERIFICATION_ORDER[index - 1]}.json")
    source_commit, source_state_sha256 = _source_proof(repo_root)
    proof_body = {
        "source_commit": source_commit,
        "source_state_sha256": source_state_sha256,
        "command_sha256": execution["command_sha256"],
        "execution_record": execution_path.name,
        "execution_sha256": _sha256_file(execution_path),
        "evidence": evidence_manifest,
        "predecessor_marker_sha256": predecessor_sha256,
    }
    if completed_run_proof is not None:
        proof_body["completed_run"] = completed_run_proof
    proof = {**proof_body, "proof_sha256": _canonical_sha256(proof_body)}
    marker = root / f"{normalized}.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": MARKER_SCHEMA_VERSION,
                "stage": normalized,
                "status": "passed",
                "proof": proof,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return marker

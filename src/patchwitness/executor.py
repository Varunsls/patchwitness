from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patchwitness.core import (
    LoadedContracts,
    PatchWitnessError,
    get_path,
    runtime_preflight,
)


@dataclass(frozen=True)
class CommandResult:
    phase: str
    command: dict[str, Any]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    def to_result(self, expected: Any = None, status: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "command": {
                "argv": self.command.get("argv", []),
                "shell": bool(self.command.get("shell", False)),
            },
            "exitCode": self.exit_code,
            "durationMs": self.duration_ms,
            "timedOut": self.timed_out,
        }
        if expected is not None:
            data["expected"] = expected
        if status is not None:
            data["status"] = status
        return data


def run_local_evidence(contracts: LoadedContracts, work_root: Path | None = None) -> dict[str, Any]:
    preflight = runtime_preflight(contracts.task)
    preflight["previewOnly"] = False
    if preflight["status"] != "pass":
        return build_evidence(
            contracts,
            preflight=preflight,
            verdict="infrastructure_error",
            failure_code="runtime.missing",
            explanation="Required runtime preflight failed. Checkout and reproduction were not executed.",
        )

    try:
        ensure_git_available()
        with temporary_workspace(work_root) as workspace_text:
            workspace = Path(workspace_text)
            base_dir = workspace / "base"
            candidate_dir = workspace / "candidate"
            checkout_workspaces(contracts, base_dir, candidate_dir)
            apply_reproduction_assets(contracts.task, base_dir)
            apply_reproduction_assets(contracts.task, candidate_dir)

            setup_results = run_setup(contracts.task, base_dir, candidate_dir)
            reproduce_command = get_path(contracts.task, ["spec", "reproduce", "command"])
            base_result = execute_command("base", reproduce_command, base_dir, contracts.task)
            candidate_result = execute_command("candidate", reproduce_command, candidate_dir, contracts.task)
            scope = analyze_scope(contracts, candidate_dir)

            return build_oracle_evidence(
                contracts,
                preflight=preflight,
                setup_results=setup_results,
                base_result=base_result,
                candidate_result=candidate_result,
                scope=scope,
            )
    except PatchWitnessError as exc:
        return build_evidence(
            contracts,
            preflight=preflight,
            verdict="policy_error" if exc.code == "policy.prohibited" else "infrastructure_error",
            failure_code=exc.code,
            explanation=exc.message,
        )


def temporary_workspace(work_root: Path | None) -> tempfile.TemporaryDirectory[str]:
    if work_root is not None:
        work_root.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix="patchwitness-", dir=str(work_root))
    return tempfile.TemporaryDirectory(prefix="patchwitness-")


def ensure_git_available() -> None:
    if shutil.which("git") is None:
        raise PatchWitnessError("runtime.missing", "Required command 'git' was not found.")


def checkout_workspaces(contracts: LoadedContracts, base_dir: Path, candidate_dir: Path) -> None:
    repo_url = (
        get_path(contracts.run_input, ["spec", "candidate", "repository", "url"])
        or get_path(contracts.task, ["spec", "repository", "url"])
    )
    base_revision = get_path(contracts.task, ["spec", "repository", "base"])
    candidate_revision = get_path(contracts.run_input, ["spec", "candidate", "revision"]) or get_path(
        contracts.run_input,
        ["spec", "candidate", "ref"],
    )

    if not isinstance(repo_url, str) or not repo_url:
        raise PatchWitnessError("checkout.failed", "Repository URL is missing.")
    if not isinstance(base_revision, str) or not base_revision:
        raise PatchWitnessError("checkout.failed", "Base revision is missing.")
    if not isinstance(candidate_revision, str) or not candidate_revision:
        raise PatchWitnessError("checkout.failed", "Candidate revision or ref is missing.")

    clone_checkout(repo_url, base_revision, base_dir)
    clone_checkout(repo_url, candidate_revision, candidate_dir)


def clone_checkout(repo_url: str, revision: str, destination: Path) -> None:
    run_git(["clone", "--quiet", "--no-checkout", repo_url, str(destination)])
    run_git(["checkout", "--quiet", revision], cwd=destination)


def run_git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PatchWitnessError("checkout.failed", detail or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def apply_reproduction_assets(task: dict[str, Any], workspace: Path) -> None:
    assets = get_path(task, ["spec", "reproductionAssets"], {}) or {}
    if not isinstance(assets, dict):
        raise PatchWitnessError("reproduction_asset.failed", "spec.reproductionAssets must be an object.")

    for unsupported in ("patches", "pointers"):
        if assets.get(unsupported):
            raise PatchWitnessError(
                "reproduction_asset.failed",
                f"Reproduction asset type '{unsupported}' is not implemented in the local executor yet.",
            )

    inline_files = assets.get("inlineFiles", []) or []
    if not isinstance(inline_files, list):
        raise PatchWitnessError("reproduction_asset.failed", "reproductionAssets.inlineFiles must be a list.")

    for item in inline_files:
        if not isinstance(item, dict):
            raise PatchWitnessError("reproduction_asset.failed", "Inline file entries must be objects.")
        rel_path = item.get("path")
        content = item.get("content")
        if not isinstance(rel_path, str) or not isinstance(content, str):
            raise PatchWitnessError("reproduction_asset.failed", "Inline files require string path and content.")
        target = safe_workspace_path(workspace, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def safe_workspace_path(workspace: Path, relative_path: str) -> Path:
    if os.path.isabs(relative_path):
        raise PatchWitnessError("policy.prohibited", f"Absolute path is prohibited: {relative_path}")
    target = (workspace / relative_path).resolve()
    root = workspace.resolve()
    if not target.is_relative_to(root):
        raise PatchWitnessError("policy.prohibited", f"Path escapes workspace: {relative_path}")
    return target


def run_setup(task: dict[str, Any], base_dir: Path, candidate_dir: Path) -> list[CommandResult]:
    setup_items = get_path(task, ["spec", "environment", "setup"], []) or []
    if not isinstance(setup_items, list):
        raise PatchWitnessError("task.invalid", "spec.environment.setup must be a list when provided.")

    results: list[CommandResult] = []
    for index, item in enumerate(setup_items):
        if not isinstance(item, dict) or not isinstance(item.get("command"), dict):
            raise PatchWitnessError("task.invalid", "Each setup item must contain a command object.")
        for phase, workspace in (("base-setup", base_dir), ("candidate-setup", candidate_dir)):
            result = execute_command(f"{phase}-{index}", item["command"], workspace, task)
            results.append(result)
            if result.exit_code != 0 or result.timed_out:
                raise PatchWitnessError("setup.failed", f"Setup command failed during {phase}.")
    return results


def execute_command(phase: str, command: dict[str, Any], workspace: Path, task: dict[str, Any]) -> CommandResult:
    if not isinstance(command, dict):
        raise PatchWitnessError("task.invalid", "Command must be an object.")
    if command.get("shell", False):
        raise PatchWitnessError("policy.prohibited", "shell=true commands are not supported by the local executor yet.")

    argv = command.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        raise PatchWitnessError("task.invalid", "Command argv must be a non-empty string list.")

    working_directory = command.get("workingDirectory", ".")
    if not isinstance(working_directory, str):
        raise PatchWitnessError("task.invalid", "Command workingDirectory must be a string.")
    cwd = safe_workspace_path(workspace, working_directory)
    timeout_seconds = int(command.get("timeoutSeconds") or get_path(task, ["spec", "environment", "resources", "timeoutSeconds"], 300))
    max_output_bytes = int(get_path(task, ["spec", "environment", "resources", "maxOutputBytes"], 65536) or 65536)

    env = build_command_env(task)
    start = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=env,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResult(
            phase=phase,
            command=command,
            exit_code=result.returncode,
            stdout=bounded(redact_workspace_paths(result.stdout, workspace), max_output_bytes),
            stderr=bounded(redact_workspace_paths(result.stderr, workspace), max_output_bytes),
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResult(
            phase=phase,
            command=command,
            exit_code=None,
            stdout=bounded(redact_workspace_paths(as_text(exc.stdout), workspace), max_output_bytes),
            stderr=bounded(redact_workspace_paths(as_text(exc.stderr), workspace), max_output_bytes),
            duration_ms=duration_ms,
            timed_out=True,
        )


def build_command_env(task: dict[str, Any]) -> dict[str, str]:
    allowed_host_keys = [
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
    ]
    env = {key: os.environ[key] for key in allowed_host_keys if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"

    task_env = get_path(task, ["spec", "environment", "env"], {}) or {}
    if not isinstance(task_env, dict):
        raise PatchWitnessError("task.invalid", "spec.environment.env must be an object when provided.")
    for key, value in task_env.items():
        key_text = str(key)
        if is_secret_key(key_text):
            raise PatchWitnessError("policy.prohibited", f"Secret-like environment key is prohibited: {key_text}")
        env[key_text] = str(value)
    return env


def is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY"))


def bounded(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n[patchwitness: output truncated]"


def redact_workspace_paths(value: str, workspace: Path) -> str:
    root = str(workspace.resolve())
    redacted = value.replace(root, "<workspace>")
    redacted = redacted.replace(root.replace("\\", "/"), "<workspace>")
    redacted = redacted.replace(root.replace("/", "\\"), "<workspace>")
    return redacted


def as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def analyze_scope(contracts: LoadedContracts, candidate_dir: Path) -> dict[str, Any]:
    base_revision = get_path(contracts.task, ["spec", "repository", "base"])
    candidate_revision = get_path(contracts.run_input, ["spec", "candidate", "revision"]) or "HEAD"
    output = run_git(["diff", "--name-only", base_revision, candidate_revision], cwd=candidate_dir)
    changed_files = [line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()]
    allow = get_path(contracts.task, ["spec", "scope", "allow"], []) or []
    deny = get_path(contracts.task, ["spec", "scope", "deny"], []) or []

    out_of_scope: list[str] = []
    for changed_file in changed_files:
        denied = any(fnmatch.fnmatch(changed_file, pattern) for pattern in deny)
        not_allowed = bool(allow) and not any(fnmatch.fnmatch(changed_file, pattern) for pattern in allow)
        if denied or not_allowed:
            out_of_scope.append(changed_file)

    return {
        "changedFiles": changed_files,
        "outOfScopeFiles": out_of_scope,
        "status": "fail" if out_of_scope else "pass",
    }


def build_oracle_evidence(
    contracts: LoadedContracts,
    preflight: dict[str, Any],
    setup_results: list[CommandResult],
    base_result: CommandResult,
    candidate_result: CommandResult,
    scope: dict[str, Any],
) -> dict[str, Any]:
    base_oracle = get_path(contracts.task, ["spec", "oracle", "base"], {})
    candidate_oracle = get_path(contracts.task, ["spec", "oracle", "candidate"], {})
    base_status = "pass" if matches_oracle(base_result, base_oracle) else "fail"
    candidate_status = "pass" if matches_oracle(candidate_result, candidate_oracle) else "fail"

    if base_result.timed_out or candidate_result.timed_out:
        verdict = "infrastructure_error"
        failure_code = "runner.failed"
        explanation = "A reproduction command timed out."
    elif base_status != "pass":
        verdict = "inconclusive"
        failure_code = "base.did_not_fail"
        explanation = "Base revision did not fail in the expected way."
    elif candidate_status != "pass":
        verdict = "fail"
        failure_code = "candidate.did_not_pass"
        explanation = "Candidate revision did not pass the declared oracle."
    elif scope["status"] != "pass":
        verdict = "fail"
        failure_code = "scope.violation"
        explanation = "Candidate changed files outside the declared scope."
    else:
        verdict = "pass"
        failure_code = "evidence.created"
        explanation = "Base failed as expected and candidate passed."

    return build_evidence(
        contracts,
        preflight=preflight,
        verdict=verdict,
        failure_code=failure_code,
        explanation=explanation,
        setup_results=setup_results,
        base_result=base_result,
        candidate_result=candidate_result,
        base_status=base_status,
        candidate_status=candidate_status,
        scope=scope,
    )


def matches_oracle(result: CommandResult, oracle: dict[str, Any]) -> bool:
    expected_exit = oracle.get("expectedExit")
    if expected_exit == "nonzero":
        exit_ok = result.exit_code is not None and result.exit_code != 0
    elif isinstance(expected_exit, int):
        exit_ok = result.exit_code == expected_exit
    else:
        exit_ok = True

    stdout_contains = oracle.get("expectedStdoutContains")
    stderr_contains = oracle.get("expectedStderrContains")
    stdout_ok = not isinstance(stdout_contains, str) or stdout_contains in result.stdout
    stderr_ok = not isinstance(stderr_contains, str) or stderr_contains in result.stderr
    return exit_ok and stdout_ok and stderr_ok


def build_evidence(
    contracts: LoadedContracts,
    preflight: dict[str, Any],
    verdict: str,
    failure_code: str,
    explanation: str,
    setup_results: list[CommandResult] | None = None,
    base_result: CommandResult | None = None,
    candidate_result: CommandResult | None = None,
    base_status: str | None = None,
    candidate_status: str | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    task_id = get_path(contracts.task, ["metadata", "id"])
    run_id = get_path(contracts.run_input, ["metadata", "id"])
    evidence_seed = f"{contracts.task_digest}:{contracts.run_input_digest}"
    evidence_id = "evb_" + hashlib.sha256(evidence_seed.encode("utf-8")).hexdigest()[:16]

    results: dict[str, Any] = {
        "verdict": verdict,
        "failureCode": failure_code,
        "explanation": explanation,
        "previewOnly": False,
        "limitations": [
            "Local preview executor does not enforce OS sandboxing.",
            "Local preview executor does not enforce network isolation.",
        ],
    }
    if setup_results:
        results["setup"] = [item.to_result(status="pass") for item in setup_results]
    if base_result is not None:
        results["base"] = base_result.to_result(
            expected=get_path(contracts.task, ["spec", "oracle", "base", "expectedExit"]),
            status=base_status,
        )
    if candidate_result is not None:
        results["candidate"] = candidate_result.to_result(
            expected=get_path(contracts.task, ["spec", "oracle", "candidate", "expectedExit"]),
            status=candidate_status,
        )
    if scope is not None:
        results["scope"] = scope

    log_results = [item for item in [base_result, candidate_result] if item is not None]

    return {
        "apiVersion": get_path(contracts.task, ["apiVersion"]),
        "kind": "EvidenceBundle",
        "metadata": {
            "id": evidence_id,
            "taskId": task_id,
            "taskDigest": contracts.task_digest,
            "runInputDigest": contracts.run_input_digest,
            "createdAt": now,
        },
        "run": {
            "id": run_id,
            "executionMode": get_path(contracts.run_input, ["spec", "execution", "mode"]),
            "candidate": get_path(contracts.run_input, ["spec", "candidate"]),
            "repository": {
                "url": get_path(contracts.task, ["spec", "repository", "url"]),
                "base": get_path(contracts.task, ["spec", "repository", "base"]),
                "candidate": get_path(contracts.run_input, ["spec", "candidate", "revision"]),
            },
            "environment": {
                "image": get_path(contracts.task, ["spec", "environment", "image"]),
                "toolchains": preflight["checks"],
                "network": get_path(contracts.task, ["spec", "environment", "network"]),
            },
        },
        "preflight": preflight,
        "results": results,
        "artifacts": {
            "logs": make_log_artifacts(log_results),
            "files": [],
        },
        "privacy": {
            "telemetryUploaded": False,
            "redactionsApplied": True,
        },
    }


def make_log_artifacts(results: list[CommandResult]) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    for result in results:
        for stream_name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
            digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
            logs.append(
                {
                    "name": f"{result.phase}-{stream_name}.log",
                    "digest": f"sha256:{digest}",
                    "maxBytes": len(text.encode("utf-8", errors="replace")),
                    "excerpt": text,
                }
            )
    return logs

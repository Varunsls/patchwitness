from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_VERSION = "repoeval.dev/v1alpha1"


class PatchWitnessError(Exception):
    """User-facing error with a stable failure code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LoadedContracts:
    task: dict[str, Any]
    run_input: dict[str, Any]
    task_digest: str
    run_input_digest: str


def load_document(path: Path, error_code: str = "task.invalid") -> dict[str, Any]:
    """Load a JSON contract, or YAML when PyYAML is installed."""

    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if suffix == ".json":
        data = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise PatchWitnessError(
                error_code,
                "YAML input requires PyYAML. Use JSON for the zero-dependency "
                "preview path or install PyYAML.",
            ) from exc
        loaded = yaml.safe_load(text)
        data = loaded
    else:
        raise PatchWitnessError(
            error_code,
            f"Unsupported contract extension '{suffix}'. Use .json, .yaml, or .yml.",
        )

    if not isinstance(data, dict):
        raise PatchWitnessError(error_code, f"{path} must contain an object.")
    return data


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_document(data: Any) -> str:
    digest = hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def load_contracts(task_path: Path, run_input_path: Path) -> LoadedContracts:
    task = load_document(task_path, "task.invalid")
    run_input = load_document(run_input_path, "run_input.invalid")
    validate_task(task)
    validate_run_input(run_input, task)
    return LoadedContracts(
        task=task,
        run_input=run_input,
        task_digest=digest_document(task),
        run_input_digest=digest_document(run_input),
    )


def validate_task(task: dict[str, Any]) -> None:
    require_value(task, "apiVersion", API_VERSION, "task.invalid")
    require_value(task, "kind", "Task", "task.invalid")
    require_path(task, ["metadata", "id"], "task.invalid")
    require_path(task, ["metadata", "title"], "task.invalid")
    require_path(task, ["metadata", "source"], "task.invalid")
    require_path(task, ["metadata", "provenance"], "task.invalid")
    require_path(task, ["spec", "repository", "url"], "task.invalid")
    require_path(task, ["spec", "repository", "base"], "task.invalid")
    require_path(task, ["spec", "environment"], "task.invalid")
    require_path(task, ["spec", "environment", "network"], "task.invalid")
    require_path(task, ["spec", "environment", "resources"], "task.invalid")
    require_path(task, ["spec", "reproduce", "command", "argv"], "task.invalid")
    require_path(task, ["spec", "oracle"], "task.invalid")

    argv = get_path(task, ["spec", "reproduce", "command", "argv"])
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
        raise PatchWitnessError("task.invalid", "spec.reproduce.command.argv must be a non-empty string list.")


def validate_run_input(run_input: dict[str, Any], task: dict[str, Any]) -> None:
    require_value(run_input, "apiVersion", API_VERSION, "run_input.invalid")
    require_value(run_input, "kind", "RunInput", "run_input.invalid")
    require_path(run_input, ["metadata", "id"], "run_input.invalid")
    require_path(run_input, ["spec", "taskRef", "id"], "run_input.invalid")
    require_path(run_input, ["spec", "candidate", "type"], "run_input.invalid")
    require_path(run_input, ["spec", "execution", "mode"], "run_input.invalid")

    task_id = get_path(task, ["metadata", "id"])
    task_ref_id = get_path(run_input, ["spec", "taskRef", "id"])
    if task_ref_id != task_id:
        raise PatchWitnessError(
            "run_input.invalid",
            f"RunInput taskRef.id '{task_ref_id}' does not match Task id '{task_id}'.",
        )


def require_value(data: dict[str, Any], key: str, expected: Any, code: str) -> None:
    observed = data.get(key)
    if observed != expected:
        raise PatchWitnessError(code, f"{key} must be {expected!r}; observed {observed!r}.")


def require_path(data: dict[str, Any], path: list[str], code: str) -> None:
    value = get_path(data, path)
    if value is None:
        raise PatchWitnessError(code, f"Missing required field: {'.'.join(path)}.")


def get_path(data: dict[str, Any], path: list[str], default: Any = None) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def inspect_contracts(contracts: LoadedContracts) -> dict[str, Any]:
    preflight = runtime_preflight(contracts.task)
    return {
        "apiVersion": API_VERSION,
        "kind": "PatchWitnessInspection",
        "task": {
            "id": get_path(contracts.task, ["metadata", "id"]),
            "title": get_path(contracts.task, ["metadata", "title"]),
            "digest": contracts.task_digest,
        },
        "runInput": {
            "id": get_path(contracts.run_input, ["metadata", "id"]),
            "digest": contracts.run_input_digest,
        },
        "preflight": preflight,
    }


def runtime_preflight(task: dict[str, Any]) -> dict[str, Any]:
    toolchains = get_path(task, ["spec", "environment", "toolchains"], []) or []
    checks: list[dict[str, Any]] = []

    if not isinstance(toolchains, list):
        raise PatchWitnessError("task.invalid", "spec.environment.toolchains must be a list when provided.")

    for toolchain in toolchains:
        if not isinstance(toolchain, dict):
            raise PatchWitnessError("task.invalid", "Each toolchain entry must be an object.")
        checks.append(check_toolchain(toolchain))

    status = "pass"
    if any(check["status"] == "fail" for check in checks):
        status = "fail"

    return {
        "status": status,
        "checks": checks,
        "previewOnly": True,
    }


def check_toolchain(toolchain: dict[str, Any]) -> dict[str, Any]:
    name = str(toolchain.get("name") or toolchain.get("command") or "toolchain")
    command = toolchain.get("command")
    required = bool(toolchain.get("required", False))
    version_args = toolchain.get("versionArgs", [])
    version_constraint = toolchain.get("versionConstraint")

    if not isinstance(command, str) or not command:
        return {
            "name": name,
            "status": "fail" if required else "warn",
            "failureCode": "runtime.missing",
            "explanation": "Toolchain command is missing.",
        }

    resolved = shutil.which(command)
    if resolved is None:
        return {
            "name": name,
            "command": command,
            "status": "fail" if required else "warn",
            "failureCode": "runtime.missing",
            "explanation": f"Required command '{command}' was not found." if required else f"Optional command '{command}' was not found.",
        }

    observed = None
    if isinstance(version_args, list) and all(isinstance(arg, str) for arg in version_args):
        observed = run_version_command([resolved, *version_args])

    check: dict[str, Any] = {
        "name": name,
        "command": command,
        "found": True,
        "status": "pass",
    }
    if observed:
        check["observed"] = observed
    if version_constraint:
        check["versionConstraint"] = version_constraint
        check["note"] = "Preview preflight records version output but does not enforce constraints yet."
    return check


def run_version_command(argv: list[str]) -> str:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # pragma: no cover - defensive reporting
        return f"version check failed: {exc}"

    output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part.strip())
    return output.splitlines()[0] if output else f"exit {result.returncode}"


def build_preview_evidence(contracts: LoadedContracts) -> dict[str, Any]:
    inspection = inspect_contracts(contracts)
    preflight = inspection["preflight"]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    missing_required = [
        check for check in preflight["checks"] if check.get("status") == "fail"
    ]
    if missing_required:
        verdict = "infrastructure_error"
        failure_code = "runtime.missing"
        explanation = "Required runtime preflight failed. Checkout and reproduction were not executed."
    else:
        verdict = "inconclusive"
        failure_code = "runner.preview_only"
        explanation = "Contracts and runtime preflight were inspected. Checkout and reproduction are not implemented in this preview runner."

    task_id = get_path(contracts.task, ["metadata", "id"])
    run_id = get_path(contracts.run_input, ["metadata", "id"])
    evidence_seed = f"{contracts.task_digest}:{contracts.run_input_digest}"
    evidence_id = "evb_" + hashlib.sha256(evidence_seed.encode("utf-8")).hexdigest()[:16]

    return {
        "apiVersion": API_VERSION,
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
        "results": {
            "verdict": verdict,
            "failureCode": failure_code,
            "explanation": explanation,
            "previewOnly": True,
        },
        "artifacts": {
            "logs": [],
            "files": [],
        },
        "privacy": {
            "telemetryUploaded": False,
            "redactionsApplied": True,
        },
    }


def render_markdown_report(evidence: dict[str, Any]) -> str:
    metadata = evidence["metadata"]
    results = evidence["results"]
    repo = evidence["run"]["repository"]
    preview_only = bool(results.get("previewOnly", False))
    title = "# PatchWitness Preview Evidence" if preview_only else "# PatchWitness Evidence"
    status = (
        "This is preview-only evidence. Checkout, setup, reproduction, oracle evaluation, and scope analysis are not implemented yet."
        if preview_only
        else "This evidence was produced by the local executor. It is advisory and does not certify correctness, security, or compliance."
    )
    lines = [
        title,
        "",
        f"- Evidence ID: `{metadata['id']}`",
        f"- Task ID: `{metadata['taskId']}`",
        f"- Verdict: `{results['verdict']}`",
        f"- Failure code: `{results['failureCode']}`",
        f"- Repository: `{repo.get('url')}`",
        f"- Base revision: `{repo.get('base')}`",
        f"- Candidate revision: `{repo.get('candidate')}`",
        "",
        "## Explanation",
        "",
        results["explanation"],
        "",
    ]
    limitations = results.get("limitations")
    if isinstance(limitations, list) and limitations:
        lines.extend(["## Limitations", ""])
        lines.extend(f"- {item}" for item in limitations)
        lines.append("")
    lines.extend([
        "## Digests",
        "",
        f"- Task: `{metadata['taskDigest']}`",
        f"- Run input: `{metadata['runInputDigest']}`",
        "",
        "## Status",
        "",
        status,
        "",
    ])
    return "\n".join(lines)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_error(exc: PatchWitnessError) -> int:
    print(f"patchwitness: {exc.code}: {exc.message}", file=sys.stderr)
    return 2

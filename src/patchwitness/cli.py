from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from patchwitness import __version__
from patchwitness.core import (
    API_VERSION,
    PatchWitnessError,
    build_preview_evidence,
    inspect_contracts,
    load_contracts,
    print_error,
    render_markdown_report,
    write_json,
)
from patchwitness.demo import run_demo
from patchwitness.executor import run_local_evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patchwitness",
        description="PatchWitness preview CLI for executable PR evidence contracts.",
    )
    parser.add_argument("--version", action="version", version=f"patchwitness {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Validate contracts and print digests/preflight.")
    add_contract_args(inspect_parser)
    inspect_parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
        help="Output format.",
    )
    inspect_parser.set_defaults(func=run_inspect)

    evidence_parser = subparsers.add_parser(
        "preview-evidence",
        help="Write preview EvidenceBundle JSON and Markdown report.",
    )
    add_contract_args(evidence_parser)
    evidence_parser.add_argument(
        "--out",
        type=Path,
        default=Path("out/patchwitness"),
        help="Output directory for preview evidence.",
    )
    evidence_parser.set_defaults(func=run_preview_evidence)

    run_parser = subparsers.add_parser(
        "run",
        help="Run local Git checkout, reproduction command, oracle, and evidence writer.",
    )
    add_contract_args(run_parser)
    run_parser.add_argument(
        "--out",
        type=Path,
        default=Path("out/patchwitness"),
        help="Output directory for evidence.",
    )
    run_parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Optional parent directory for temporary workspaces.",
    )
    run_parser.set_defaults(func=run_evidence)

    demo_parser = subparsers.add_parser(
        "demo",
        help="Create a local demo repository and produce passing evidence.",
    )
    demo_parser.add_argument(
        "--out",
        type=Path,
        default=Path("out/local-demo"),
        help="Output directory for the demo repository, contracts, and evidence.",
    )
    demo_parser.set_defaults(func=run_demo_command)

    init_parser = subparsers.add_parser(
        "init",
        help="Write Task and RunInput JSON contracts for a local repository.",
    )
    init_parser.add_argument("--task-id", required=True, help="Stable task identifier.")
    init_parser.add_argument("--title", help="Human-readable task title. Defaults to task-id.")
    init_parser.add_argument("--repo", type=Path, default=Path("."), help="Local Git repository path.")
    init_parser.add_argument("--base", required=True, help="Base revision that should fail.")
    init_parser.add_argument("--candidate", default="HEAD", help="Candidate revision that should pass.")
    init_parser.add_argument(
        "--out",
        type=Path,
        default=Path(".patchwitness"),
        help="Output directory for task.json and run-input.json.",
    )
    init_parser.add_argument(
        "--allow",
        action="append",
        default=None,
        help="Allowed changed-file glob. May be repeated. Defaults to **.",
    )
    init_parser.add_argument(
        "--deny",
        action="append",
        default=None,
        help="Denied changed-file glob. May be repeated. Defaults to .github/**.",
    )
    init_parser.add_argument(
        "reproduce_command",
        nargs=argparse.REMAINDER,
        help="Command to run, after --. Example: -- python -m pytest tests/test_bug.py",
    )
    init_parser.set_defaults(func=run_init)

    return parser


def add_contract_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", required=True, type=Path, help="Task contract path.")
    parser.add_argument("--run-input", required=True, type=Path, help="RunInput contract path.")


def run_inspect(args: argparse.Namespace) -> int:
    contracts = load_contracts(args.task, args.run_input)
    inspection = inspect_contracts(contracts)

    if args.format == "json":
        print(json.dumps(inspection, indent=2, sort_keys=True))
        return 0

    print(f"Task: {inspection['task']['id']}")
    print(f"Task digest: {inspection['task']['digest']}")
    print(f"Run input: {inspection['runInput']['id']}")
    print(f"Run input digest: {inspection['runInput']['digest']}")
    print(f"Preflight: {inspection['preflight']['status']}")
    for check in inspection["preflight"]["checks"]:
        observed = f" ({check.get('observed')})" if check.get("observed") else ""
        print(f"- {check['name']}: {check['status']}{observed}")
    return 0 if inspection["preflight"]["status"] == "pass" else 1


def run_preview_evidence(args: argparse.Namespace) -> int:
    contracts = load_contracts(args.task, args.run_input)
    evidence = build_preview_evidence(contracts)

    args.out.mkdir(parents=True, exist_ok=True)
    evidence_path = args.out / "evidence-bundle.json"
    report_path = args.out / "report.md"
    write_json(evidence_path, evidence)
    report_path.write_text(render_markdown_report(evidence), encoding="utf-8")

    print(f"Wrote {evidence_path}")
    print(f"Wrote {report_path}")
    return 0 if evidence["preflight"]["status"] == "pass" else 1


def run_evidence(args: argparse.Namespace) -> int:
    contracts = load_contracts(args.task, args.run_input)
    evidence = run_local_evidence(contracts, work_root=args.work_dir)

    args.out.mkdir(parents=True, exist_ok=True)
    evidence_path = args.out / "evidence-bundle.json"
    report_path = args.out / "report.md"
    write_json(evidence_path, evidence)
    report_path.write_text(render_markdown_report(evidence), encoding="utf-8")

    print(f"Wrote {evidence_path}")
    print(f"Wrote {report_path}")
    print(f"Verdict: {evidence['results']['verdict']} ({evidence['results']['failureCode']})")
    return 0 if evidence["results"]["verdict"] == "pass" else 1


def run_demo_command(args: argparse.Namespace) -> int:
    result = run_demo(args.out)
    print("PatchWitness local demo completed.")
    print(f"Verdict: {result['verdict']} ({result['failureCode']})")
    print(f"Evidence JSON: {result['evidencePath']}")
    print(f"Markdown report: {result['reportPath']}")
    return 0 if result["verdict"] == "pass" else 1


def run_init(args: argparse.Namespace) -> int:
    command = normalize_remainder(args.reproduce_command)
    base = resolve_git_revision(args.repo, args.base)
    candidate = resolve_git_revision(args.repo, args.candidate)

    task, run_input = build_init_contracts(
        task_id=args.task_id,
        title=args.title or args.task_id,
        repo=args.repo,
        base=base,
        candidate=candidate,
        command=command,
        allow=args.allow or ["**"],
        deny=args.deny or [".github/**"],
    )

    args.out.mkdir(parents=True, exist_ok=True)
    task_path = args.out / "task.json"
    run_input_path = args.out / "run-input.json"
    write_json(task_path, task)
    write_json(run_input_path, run_input)

    print(f"Wrote {task_path}")
    print(f"Wrote {run_input_path}")
    print(f"Base: {base}")
    print(f"Candidate: {candidate}")
    return 0


def normalize_remainder(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise PatchWitnessError("init.invalid", "A reproduction command is required after --.")
    return command


def resolve_git_revision(repo: Path, revision: str) -> str:
    if shutil.which("git") is None:
        raise PatchWitnessError("runtime.missing", "Required command 'git' was not found.")

    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", revision],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PatchWitnessError("init.invalid", detail or f"Could not resolve revision '{revision}'.")
    return result.stdout.strip()


def build_init_contracts(
    *,
    task_id: str,
    title: str,
    repo: Path,
    base: str,
    candidate: str,
    command: list[str],
    allow: list[str],
    deny: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    repo_url = str(repo)
    toolchain_command = command[0]
    toolchain_name = Path(toolchain_command).name or toolchain_command
    task = {
        "apiVersion": API_VERSION,
        "kind": "Task",
        "metadata": {
            "id": task_id,
            "title": title,
            "source": {"type": "cli-init"},
            "provenance": {"license": "user-supplied", "redistribution": "local-only"},
        },
        "spec": {
            "repository": {"url": repo_url, "base": base, "submodules": "disabled"},
            "environment": {
                "toolchains": [
                    {
                        "name": toolchain_name,
                        "command": toolchain_command,
                        "versionArgs": ["--version"],
                        "required": True,
                    }
                ],
                "network": {"checkout": "disabled", "setup": "disabled", "reproduce": "disabled"},
                "resources": {"timeoutSeconds": 300, "memoryMb": 1024, "maxOutputBytes": 65536},
            },
            "reproduce": {
                "command": {
                    "argv": command,
                    "shell": False,
                    "workingDirectory": ".",
                    "timeoutSeconds": 300,
                }
            },
            "oracle": {
                "base": {"expectedExit": "nonzero"},
                "candidate": {"expectedExit": 0},
            },
            "scope": {"allow": allow, "deny": deny},
            "artifacts": {"logs": {"maxBytesPerStream": 65536}},
        },
    }
    run_input = {
        "apiVersion": API_VERSION,
        "kind": "RunInput",
        "metadata": {"id": f"{task_id}-run", "source": {"type": "cli-init"}},
        "spec": {
            "taskRef": {"id": task_id},
            "candidate": {
                "type": "commit",
                "repository": {"url": repo_url},
                "revision": candidate,
            },
            "execution": {"mode": "local", "cleanWorkspaces": True, "uploadTelemetry": False},
            "reporting": {"formats": ["json", "markdown"], "advisoryComment": False},
        },
    }
    return task, run_input


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PatchWitnessError as exc:
        return print_error(exc)

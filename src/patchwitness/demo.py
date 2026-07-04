from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from patchwitness.core import load_contracts, render_markdown_report, write_json
from patchwitness.executor import run_local_evidence


def run_demo(out_dir: Path) -> dict[str, Path | str]:
    demo_root = out_dir
    repo = demo_root / "repo"
    contracts_dir = demo_root / "contracts"
    evidence_dir = demo_root / "evidence"

    reset_demo_dir(demo_root)
    repo.mkdir(parents=True)
    contracts_dir.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)

    create_repo(repo)
    base = git(repo, "rev-parse", "HEAD")
    fix_repo(repo)
    candidate = git(repo, "rev-parse", "HEAD")

    task_path, run_input_path = write_contracts(contracts_dir, base, candidate)
    contracts = load_contracts(task_path, run_input_path)
    with working_directory(demo_root):
        evidence = run_local_evidence(contracts)

    evidence_path = evidence_dir / "evidence-bundle.json"
    report_path = evidence_dir / "report.md"
    write_json(evidence_path, evidence)
    report_path.write_text(render_markdown_report(evidence), encoding="utf-8")

    return {
        "verdict": evidence["results"]["verdict"],
        "failureCode": evidence["results"]["failureCode"],
        "evidencePath": evidence_path,
        "reportPath": report_path,
    }


def reset_demo_dir(demo_root: Path) -> None:
    if demo_root.exists():
        resolved = demo_root.resolve()
        parent = demo_root.parent.resolve()
        if not resolved.is_relative_to(parent):
            raise RuntimeError(f"Refusing to remove outside output parent: {resolved}")
        shutil.rmtree(resolved)


@contextmanager
def working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path.resolve())
    try:
        yield
    finally:
        os.chdir(previous)


def create_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    git(repo, "config", "user.email", "patchwitness@example.invalid")
    git(repo, "config", "user.name", "PatchWitness Demo")
    (repo / "app.py").write_text("def is_duplicate(email, existing):\n    return False\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "--quiet", "-m", "base bug")


def fix_repo(repo: Path) -> None:
    (repo / "app.py").write_text(
        "def is_duplicate(email, existing):\n    return email in existing\n",
        encoding="utf-8",
    )
    git(repo, "add", "app.py")
    git(repo, "commit", "--quiet", "-m", "fix duplicate email")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def write_contracts(contracts_dir: Path, base: str, candidate: str) -> tuple[Path, Path]:
    repo_url = "repo"
    task = {
        "apiVersion": "repoeval.dev/v1alpha1",
        "kind": "Task",
        "metadata": {
            "id": "local-demo-duplicate-email",
            "title": "Duplicate email should be detected",
            "source": {"type": "local-demo"},
            "provenance": {"license": "Apache-2.0", "redistribution": "local-demo"},
        },
        "spec": {
            "repository": {"url": repo_url, "base": base, "submodules": "disabled"},
            "environment": {
                "toolchains": [
                    {
                        "name": "python",
                        "command": "python",
                        "versionArgs": ["--version"],
                        "required": True,
                    }
                ],
                "network": {"checkout": "disabled", "setup": "disabled", "reproduce": "disabled"},
                "resources": {"timeoutSeconds": 60, "memoryMb": 512, "maxOutputBytes": 65536},
            },
            "reproductionAssets": {
                "inlineFiles": [
                    {
                        "path": "patchwitness_repro.py",
                        "language": "python",
                        "content": (
                            "from app import is_duplicate\n"
                            "assert is_duplicate('a@example.com', {'a@example.com'})\n"
                        ),
                    }
                ]
            },
            "reproduce": {
                "command": {
                    "argv": ["python", "patchwitness_repro.py"],
                    "shell": False,
                    "workingDirectory": ".",
                    "timeoutSeconds": 30,
                }
            },
            "oracle": {
                "base": {"expectedExit": "nonzero", "expectedStderrContains": "AssertionError"},
                "candidate": {"expectedExit": 0},
            },
            "scope": {"allow": ["app.py"], "deny": [".github/**"]},
            "artifacts": {"logs": {"maxBytesPerStream": 65536}},
        },
    }
    run_input = {
        "apiVersion": "repoeval.dev/v1alpha1",
        "kind": "RunInput",
        "metadata": {"id": "local-demo-run", "source": {"type": "cli"}},
        "spec": {
            "taskRef": {"id": "local-demo-duplicate-email"},
            "candidate": {
                "type": "commit",
                "repository": {"url": repo_url},
                "revision": candidate,
            },
            "execution": {"mode": "local", "cleanWorkspaces": True, "uploadTelemetry": False},
            "reporting": {"formats": ["json", "markdown"], "advisoryComment": False},
        },
    }

    task_path = contracts_dir / "task.json"
    run_input_path = contracts_dir / "run-input.json"
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    run_input_path.write_text(json.dumps(run_input, indent=2) + "\n", encoding="utf-8")
    return task_path, run_input_path

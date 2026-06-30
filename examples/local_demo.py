from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = ROOT / "out" / "local-demo"
REPO = DEMO_ROOT / "repo"
CONTRACTS = DEMO_ROOT / "contracts"
EVIDENCE = DEMO_ROOT / "evidence"


def main() -> int:
    reset_demo_dir()
    create_repo()
    base = git("rev-parse", "HEAD")
    fix_repo()
    candidate = git("rev-parse", "HEAD")
    task_path, run_input_path = write_contracts(base, candidate)
    run_patchwitness(task_path, run_input_path)

    print("PatchWitness local demo completed.")
    print(f"Evidence JSON: {relative(EVIDENCE / 'evidence-bundle.json')}")
    print(f"Markdown report: {relative(EVIDENCE / 'report.md')}")
    return 0


def reset_demo_dir() -> None:
    if DEMO_ROOT.exists():
        resolved = DEMO_ROOT.resolve()
        out_root = (ROOT / "out").resolve()
        if not resolved.is_relative_to(out_root):
            raise RuntimeError(f"Refusing to remove outside out/: {resolved}")
        shutil.rmtree(resolved)
    REPO.mkdir(parents=True)
    CONTRACTS.mkdir(parents=True)


def create_repo() -> None:
    subprocess.run(["git", "init", "--quiet", str(REPO)], check=True)
    git("config", "user.email", "patchwitness@example.invalid")
    git("config", "user.name", "PatchWitness Demo")
    (REPO / "app.py").write_text("def is_duplicate(email, existing):\n    return False\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "--quiet", "-m", "base bug")


def fix_repo() -> None:
    (REPO / "app.py").write_text(
        "def is_duplicate(email, existing):\n    return email in existing\n",
        encoding="utf-8",
    )
    git("add", "app.py")
    git("commit", "--quiet", "-m", "fix duplicate email")


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def write_contracts(base: str, candidate: str) -> tuple[Path, Path]:
    repo_url = "out/local-demo/repo"
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
    task_path = CONTRACTS / "task.json"
    run_input_path = CONTRACTS / "run-input.json"
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    run_input_path.write_text(json.dumps(run_input, indent=2) + "\n", encoding="utf-8")
    return task_path, run_input_path


def run_patchwitness(task_path: Path, run_input_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "patchwitness",
            "run",
            "--task",
            str(task_path),
            "--run-input",
            str(run_input_path),
            "--out",
            str(EVIDENCE),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )


def relative(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


if __name__ == "__main__":
    raise SystemExit(main())

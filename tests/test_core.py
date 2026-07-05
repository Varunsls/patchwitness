from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from patchwitness.core import (
    PatchWitnessError,
    build_preview_evidence,
    digest_document,
    inspect_contracts,
    load_contracts,
)
from patchwitness.cli import main as cli_main
from patchwitness.demo import run_demo
from patchwitness.executor import run_local_evidence


ROOT = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    def test_loads_examples_and_computes_stable_digests(self) -> None:
        contracts = load_contracts(
            ROOT / "examples" / "preview-task.json",
            ROOT / "examples" / "preview-run-input.json",
        )

        self.assertTrue(contracts.task_digest.startswith("sha256:"))
        self.assertEqual(contracts.task_digest, digest_document(contracts.task))
        self.assertEqual(contracts.run_input_digest, digest_document(contracts.run_input))

    def test_inspection_preflight_passes_for_python(self) -> None:
        contracts = load_contracts(
            ROOT / "examples" / "preview-task.json",
            ROOT / "examples" / "preview-run-input.json",
        )

        inspection = inspect_contracts(contracts)

        self.assertEqual(inspection["kind"], "PatchWitnessInspection")
        self.assertEqual(inspection["preflight"]["status"], "pass")
        self.assertEqual(inspection["preflight"]["checks"][0]["name"], "python")

    def test_preview_evidence_is_inconclusive_until_runner_exists(self) -> None:
        contracts = load_contracts(
            ROOT / "examples" / "preview-task.json",
            ROOT / "examples" / "preview-run-input.json",
        )

        evidence = build_preview_evidence(contracts)

        self.assertEqual(evidence["kind"], "EvidenceBundle")
        self.assertEqual(evidence["results"]["verdict"], "inconclusive")
        self.assertEqual(evidence["results"]["failureCode"], "runner.preview_only")

    def test_action_metadata_exposes_required_inputs(self) -> None:
        action = (ROOT / "action.yml").read_text(encoding="utf-8")

        self.assertIn("using: composite", action)
        self.assertIn("task:", action)
        self.assertIn("run-input:", action)
        self.assertIn("github-step-summary:", action)
        self.assertIn("fail-on-verdict:", action)
        self.assertIn("verdict:", action)
        self.assertIn("failure-code:", action)
        self.assertIn("exit-code=", action)
        self.assertIn('python -m pip install "$GITHUB_ACTION_PATH"', action)
        self.assertIn("python -m patchwitness run", action)

    def test_preview_evidence_appends_github_step_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            summary_path = root / "summary.md"

            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}), redirect_stdout(StringIO()):
                exit_code = cli_main(
                    [
                        "preview-evidence",
                        "--task",
                        str(ROOT / "examples" / "preview-task.json"),
                        "--run-input",
                        str(ROOT / "examples" / "preview-run-input.json"),
                        "--out",
                        str(out),
                        "--github-step-summary",
                    ]
                )

            report = (out / "report.md").read_text(encoding="utf-8")
            summary = summary_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary, report)
        self.assertIn("# PatchWitness Preview Evidence", summary)

    def test_run_input_must_match_task_id(self) -> None:
        task_path = ROOT / "examples" / "preview-task.json"
        run_input = json.loads((ROOT / "examples" / "preview-run-input.json").read_text(encoding="utf-8"))
        run_input["spec"]["taskRef"]["id"] = "different-task"

        with tempfile.TemporaryDirectory() as tmp:
            run_input_path = Path(tmp) / "run-input.json"
            run_input_path.write_text(json.dumps(run_input), encoding="utf-8")

            with self.assertRaises(PatchWitnessError) as raised:
                load_contracts(task_path, run_input_path)

        self.assertEqual(raised.exception.code, "run_input.invalid")

    def test_run_input_loader_errors_use_run_input_code(self) -> None:
        task_path = ROOT / "examples" / "preview-task.json"

        with tempfile.TemporaryDirectory() as tmp:
            run_input_path = Path(tmp) / "run-input.txt"
            run_input_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(PatchWitnessError) as raised:
                load_contracts(task_path, run_input_path)

        self.assertEqual(raised.exception.code, "run_input.invalid")

    @unittest.skipIf(shutil.which("git") is None, "git is required for local executor tests")
    def test_local_executor_creates_passing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_demo_repo(Path(tmp))
            contracts = write_demo_contracts(Path(tmp), paths)

            evidence = run_local_evidence(contracts, work_root=Path(tmp) / "work")

        self.assertEqual(evidence["results"]["verdict"], "pass")
        self.assertEqual(evidence["results"]["failureCode"], "evidence.created")
        self.assertEqual(evidence["results"]["base"]["status"], "pass")
        self.assertEqual(evidence["results"]["candidate"]["status"], "pass")
        self.assertEqual(evidence["results"]["scope"]["outOfScopeFiles"], [])

    @unittest.skipIf(shutil.which("git") is None, "git is required for local executor tests")
    def test_local_executor_reports_candidate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_demo_repo(Path(tmp))
            paths["candidate"] = paths["base"]
            contracts = write_demo_contracts(Path(tmp), paths)

            evidence = run_local_evidence(contracts, work_root=Path(tmp) / "work")

        self.assertEqual(evidence["results"]["verdict"], "fail")
        self.assertEqual(evidence["results"]["failureCode"], "candidate.did_not_pass")

    @unittest.skipIf(shutil.which("git") is None, "git is required for local executor tests")
    def test_local_executor_reports_scope_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_demo_repo(Path(tmp), include_scope_violation=True)
            contracts = write_demo_contracts(Path(tmp), paths)

            evidence = run_local_evidence(contracts, work_root=Path(tmp) / "work")

        self.assertEqual(evidence["results"]["verdict"], "fail")
        self.assertEqual(evidence["results"]["failureCode"], "scope.violation")
        self.assertEqual(evidence["results"]["scope"]["outOfScopeFiles"], ["README.md"])

    @unittest.skipIf(shutil.which("git") is None, "git is required for local executor tests")
    def test_package_demo_creates_passing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_demo(Path(tmp) / "demo")
            evidence = json.loads(Path(result["evidencePath"]).read_text(encoding="utf-8"))

        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["failureCode"], "evidence.created")
        self.assertEqual(evidence["results"]["verdict"], "pass")
        self.assertNotIn(str(Path(tmp)), json.dumps(evidence))

    @unittest.skipIf(shutil.which("git") is None, "git is required for local executor tests")
    def test_init_command_writes_runnable_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = create_demo_repo(root)
            out = root / "contracts"

            with redirect_stdout(StringIO()):
                exit_code = cli_main(
                    [
                        "init",
                        "--task-id",
                        "generated-duplicate-email",
                        "--title",
                        "Generated duplicate email check",
                        "--repo",
                        paths["repo"],
                        "--base",
                        paths["base"],
                        "--candidate",
                        paths["candidate"],
                        "--out",
                        str(out),
                        "--",
                        sys.executable,
                        "-c",
                        "from app import is_duplicate; assert is_duplicate('a@example.com', {'a@example.com'})",
                    ]
                )

            contracts = load_contracts(out / "task.json", out / "run-input.json")
            evidence = run_local_evidence(contracts, work_root=root / "work")

        self.assertEqual(exit_code, 0)
        self.assertEqual(contracts.task["metadata"]["id"], "generated-duplicate-email")
        self.assertEqual(contracts.task["spec"]["reproduce"]["command"]["argv"][0], sys.executable)
        self.assertEqual(evidence["results"]["verdict"], "pass")


def create_demo_repo(root: Path, include_scope_violation: bool = False) -> dict[str, str]:
    repo = root / "demo-repo"
    repo.mkdir()
    git_init(repo)
    write_app(repo, "def is_duplicate(email, existing):\n    return False\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "--quiet", "-m", "base bug")
    base = git(repo, "rev-parse", "HEAD")

    write_app(repo, "def is_duplicate(email, existing):\n    return email in existing\n")
    if include_scope_violation:
        (repo / "README.md").write_text("scope violation\n", encoding="utf-8")
        git(repo, "add", "app.py", "README.md")
    else:
        git(repo, "add", "app.py")
    git(repo, "commit", "--quiet", "-m", "fix duplicate email")
    candidate = git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), "base": base, "candidate": candidate}


def git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    git(repo, "config", "user.email", "patchwitness@example.invalid")
    git(repo, "config", "user.name", "PatchWitness Tests")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def write_app(repo: Path, text: str) -> None:
    (repo / "app.py").write_text(text, encoding="utf-8")


def write_demo_contracts(root: Path, paths: dict[str, str]):
    task = {
        "apiVersion": "repoeval.dev/v1alpha1",
        "kind": "Task",
        "metadata": {
            "id": "local-duplicate-email",
            "title": "Duplicate email should be detected",
            "source": {"type": "local-demo"},
            "provenance": {"license": "Apache-2.0", "redistribution": "local-demo"},
        },
        "spec": {
            "repository": {
                "url": paths["repo"],
                "base": paths["base"],
                "submodules": "disabled",
            },
            "environment": {
                "toolchains": [
                    {
                        "name": "python",
                        "command": sys.executable,
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
                    "argv": [sys.executable, "patchwitness_repro.py"],
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
        "metadata": {"id": "local-duplicate-email-run", "source": {"type": "cli"}},
        "spec": {
            "taskRef": {"id": "local-duplicate-email"},
            "candidate": {
                "type": "commit",
                "repository": {"url": paths["repo"]},
                "revision": paths["candidate"],
            },
            "execution": {"mode": "local", "cleanWorkspaces": True, "uploadTelemetry": False},
            "reporting": {"formats": ["json", "markdown"], "advisoryComment": False},
        },
    }
    task_path = root / "task.json"
    run_input_path = root / "run-input.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    run_input_path.write_text(json.dumps(run_input), encoding="utf-8")
    return load_contracts(task_path, run_input_path)


if __name__ == "__main__":
    unittest.main()

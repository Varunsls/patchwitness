from __future__ import annotations

import argparse
import json
from pathlib import Path

from patchwitness import __version__
from patchwitness.core import (
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PatchWitnessError as exc:
        return print_error(exc)

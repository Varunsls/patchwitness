# PatchWitness

PatchWitness is a local CLI for executable pull request evidence.

It checks a declared bug-fix claim by running the same reproduction command on:

- a base Git revision that should fail
- a candidate Git revision that should pass

It then writes an advisory evidence bundle with command results, digests,
bounded logs, and changed-file scope.

## Status

Early preview. Use it only on repositories and task files you trust.

PatchWitness executes local commands. It is not a sandbox and does not certify
security, correctness, compliance, or AI authorship.

## Requirements

- Python 3.11+
- Git

## Quickstart

From the repository root:

```powershell
python -m pip install .
python -m patchwitness demo
```

The demo creates a local Git repository under `out/local-demo`, runs a
fail-before/pass-after check, and writes:

```text
out/local-demo/evidence/evidence-bundle.json
out/local-demo/evidence/report.md
```

Inspect static example contracts:

```powershell
python -m patchwitness inspect --task examples/preview-task.json --run-input examples/preview-run-input.json
```

Run against contracts:

```powershell
python -m patchwitness run --task path/to/task.json --run-input path/to/run-input.json --out out/evidence
```

## What It Does

- loads `Task` and `RunInput` JSON contracts
- computes canonical SHA-256 digests
- checks required local toolchains
- checks out base and candidate Git revisions
- applies inline reproduction files
- runs structured commands without shell execution
- evaluates fail-before/pass-after outcomes
- reports changed-file scope violations
- writes JSON and Markdown evidence

## License

Apache-2.0.

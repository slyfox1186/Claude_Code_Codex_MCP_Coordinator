# Project Validation Environments Design

## Problem

`setup.sh add-repo` detects Python tests while running inside Agent Duet's private
environment and writes that interpreter into `validation_commands`. The generated command
therefore assumes the coordinator environment contains pytest and every dependency of every
registered project. It does not, and making it do so would couple unrelated projects and allow
their dependency versions to break the coordinator.

Final validation also starts only after reconciliation. A failure records a manifest path but
does not expose the measured command result in durable evidence or give Claude one opportunity
to repair the exact failure. The operator can consequently spend a full three-phase run only to
receive an opaque terminal error.

## Design

### Isolated project environment

Each registered Python project gets a validation environment separate from Agent Duet and from
other projects. Setup reuses a compatible project-local `.venv` or `venv` only when its Python
can import pytest. Otherwise it creates a deterministic environment below Agent Duet's data
directory. When Conda is installed, setup creates a uniquely named Conda environment; otherwise
it creates a private virtual environment with the discovered system `python3`.

Before installing project packages, interactive setup explains the exact environment and
dependency files and asks for consent. `--yes` supplies that consent for automation. A declined
installation leaves the project registered without a false validation command and prints the
manual configuration action.

Dependency discovery uses declared project metadata rather than import guessing:

- a root `constraints.txt` constrains pip resolution;
- existing `requirements.txt`, `app/requirements.txt`, and `requirements-dev.txt` files are
  installed in that order;
- a standards-based `pyproject.toml` with a `[project]` table is installed editable, using a
  `dev` or `test` extra when declared;
- pytest itself is installed at the project-declared exact pin when one exists, otherwise at the
  coordinator-supported version range.

Setup verifies `import pytest` after installation and writes the validation environment's
absolute Python path into `validation_commands`. It never installs target-project packages into
Agent Duet's runtime environment or Conda `base`.

### Validation feedback and repair

All configured validation vectors are included verbatim in the implementation and reconciliation
prompts. The coordinator remains authoritative and reruns them independently after phase 3.

If final validation fails, its manifest, command result, exit code, and redacted output tail are
merged into durable run evidence before any next action. The coordinator then invokes one fresh
Claude repair pass containing the exact failed result and reruns the complete validation set.
One retry is a strict cost and loop bound. A second failure becomes terminal with both attempts
preserved and an actionable error.

## Compatibility and safety

- Existing hand-authored repository commands are unchanged.
- Existing generated commands continue to run until `setup.sh add-repo PATH` is rerun; setup
  replaces only its own marked repository block.
- Project environments are deterministic per canonical path and safe for concurrent projects.
- Validation output remains redacted and size-bounded.
- No branch, commit, push, or deployment behavior changes.

## Verification

Black-box installer tests cover environment isolation, dependency discovery, consent, reuse, and
generated configuration. Worker integration tests prove validation evidence is persisted, one
repair is attempted, the full gate is rerun, success advances to finalization, and a second
failure terminates without looping. The complete pytest, Ruff, mypy, shell syntax, installer
check, and a real registration of the selected project must pass before shipping.

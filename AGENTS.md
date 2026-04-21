# os-autoinst-scripts Agent Guidelines

Collection of scripts (Bash, Python, Perl) for os-autoinst and openQA.

## Build & Test Commands

- `make test`: Run all unit tests (Bash and Python).
- `make test-bash`: Run only Bash tests (uses `prove` and `test-tap-bash`).
- `make test-python`: Run only Python tests (uses `pytest`).
- `make checkstyle`: Run all style checks (shellcheck, shfmt, yamllint, ruff).
- `make shfmt`: Automatically format shell scripts.
- `make update-deps`: Update dependencies based on `dependencies.yaml`.

## Conventions

- Code style: For shell scripts, use `make shfmt`. For Python, use `ruff` (via `make checkstyle`).
- Linting: Always run `make checkstyle` before claiming completion.
- Testing: Add tests for new features or bug fixes in `test/` (for Bash) or `tests/` (for Python).
- Dependencies: Update `dependencies.yaml` and run `make update-deps`.

## Constraints

- `tasks/`: Read/write for planning. Never run git operations on this
  directory.
- Never run git clean or any command that deletes unversioned files. Ask for
  confirmation.

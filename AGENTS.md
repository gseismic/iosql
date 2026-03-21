# Repository Guidelines

## Project Structure & Module Organization
- `iosql/` contains the main Python package: entry points in `database.py`, `table.py`, and subpackages (`partition`, `query`, `sqlite`, `postgresql`) that hold partition logic, SQL helpers, and dialect-specific adapters.
- `examples/` documents how to wire the library into real use cases; run these scripts to see partition setup and querying patterns.
- `tests/` mirrors the package layout with unit suites (`tests/test_partition*.py`) plus helper folders (`tests/query`, `tests/sqlite`, `tests/utils`).
- `database/` is where example SQLite files live when running the integration tests or demos, so keep it tidy even though the directory is checked in for sample data.
- Build artifacts land in `build/` and `dist/`; do not edit them manually.

## Build, Test, and Development Commands
- `python -m pip install -e .` installs the package in editable mode so imports reflect ongoing edits.
- `python setup.py sdist bdist_wheel` (or `python -m build` once added) regenerates the source/wheel distributions before publishing.
- `python -m pytest tests` runs the full test matrix and should pass before pushing.
- `python -m pytest tests/test_partition_integration.py` targets the heavier partition-flow checks when making partition logic changes.

## Coding Style & Naming Conventions
- Stick to 4-space indentation, PEP 8 line lengths, and snake_case module/function names; classes like `DurationPartition` and `CalendarPartition` follow PascalCase.
- Keep SQL helper functions in `iosql/query`/`iosql/sqlite` to avoid leaking implementation details.
- Prefer explicit parameter names (e.g., `partition=DurationPartition(...)`, `pk=['timestamp']`) to match examples in `README.md` and internal docs.

## Testing Guidelines
- Tests use `pytest`; keep new files under `tests/` with names starting with `test_` and matching the module under test.
- Integration scenarios that touch `database/` should clean up by deleting temporary SQLite files in `tests/sqlite` to avoid stale partitions.
- Add fixtures in `tests/utils` when shared setup is needed; call them via module-level imports so other suites can reuse them.

## Commit & Pull Request Guidelines
- Follow the existing pattern of `type(scope): summary` (e.g., `fix(sqlite): keep handles open` or `feat: add partition setter`).
- PR descriptions should explain the intent, list commands run (tests/build), and reference any related issues or bug numbers.
- Include screenshots or logs only when visual/behavioral output changes; otherwise confirm that no UI artifacts were impacted.
- Tag reviewers early, keep PR titles descriptive, and mention any manual cleanup steps required after merging.

## Security & Configuration Tips
- Partition metadata in `database/<name>/meta.sqlite3` is the single source of truth—avoid manual edits unless you fully understand the partition objects stored there.
- Secrets should never land in tracked files; if you need configuration, pass it via environment variables or test fixtures rather than editing files under version control.

# Example: P&C insurance

A fictional three-person startup, Acme, deciding what to build for property and casualty insurance: claims (footage as evidence, fraud, adjuster workload), underwriting (submission intake and research) and actuarial pricing and reserving.

- `domain.yml`: company, market, what is being explored, scope fields and roles.
- `seats.yml`: 20 seats grouped by underwriting, actuarial, risk, claims and distribution.
- `entities.yml`: ASR aliases heard in the test fixtures.

This is the market of the shipped test calls (`tests/fixtures/`) and their labels (`tests/labels/`), so the test suite copies these three files into its temporary repos. Change them only together with the labels.

Use it: `uv run python -m nua init --example insurance`.

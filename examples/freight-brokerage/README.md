# Example: freight brokerage

A fictional two-person startup, Halyard Labs, deciding what to build for the back office of mid-size freight brokerages: carrier onboarding and vetting, track and trace, carrier payments and disputes, shipper audits and load documentation, and spot quoting.

- `domain.yml`: the market, problem areas, interviewees, scope fields (freight mode or workflow, party, brokerage size) and roles (broker, shipper, carrier, vendor, expert, competitor).
- `seats.yml`: 13 seats across brokerage operations, the back office and the other side of the load.
- `entities.yml`: empty, with the shape of an entry.

The worked examples in `nua/prompts/extract.py` and `consolidate.py` come from a fictional call in this market (an operations lead at a brokerage). There are no labeled calls for it yet: to score models on your own calls, see `tests/labels/README.md` ("Labels for your own market").

Use it: `uv run python -m nua init --example freight-brokerage`.

# Starting points

Each folder holds a `domain.yml`, `seats.yml` and `entities.yml` to copy into `config/`. Start from one with `nua init --example`:

```sh
uv run python -m nua init                                # asks, starting from what is in config/ now
uv run python -m nua init --example freight-brokerage    # starts from an example, then asks the rest
uv run python -m nua init --example insurance --yes      # takes the example as is, no questions
```

| Folder | What it is |
|---|---|
| `blank/` | Commented templates with empty values: what `config/` ships with |
| `insurance/` | A fictional startup exploring AI for P&C insurance. The test fixtures and labels in `tests/` are calls from this market, and the test suite reads its config from here |
| `freight-brokerage/` | A fictional startup exploring software for freight brokerages, the market of the worked examples in `nua/prompts/extract.py` and `consolidate.py` |

Starting from an example replaces those three files in `config/` and nothing else: your team (`config/people.yml`) and pipeline settings (`config/pipeline.yml`) stay as they are. Every person and company in the examples is fictional.

To add a market, copy `blank/` to a new folder, fill it in, and add a short `README.md`.

# Contributing

Thanks for helping. Bug reports, fixes, docs and new recorder or provider support are all welcome.

## Dev setup

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/<owner>/discovery-base.git
cd discovery-base
uv sync                      # builds .venv from uv.lock, dev tools included
cp .env.example .env         # only needed to call real models
```

Try the pipeline with no keys at all. `--fake` swaps every model for label-backed fakes (`tests/labels/`), so the shipped fixtures run end to end:

```sh
uv run python -m discovery run tests/fixtures/darius-kemp.md --dry-run --fake
```

## Tests

```sh
uv run pytest                          # the whole suite
uv run pytest tests/unit/test_verify.py -q
```

The suite needs no keys, network or cloud. Terraform is checked by `tests/unit/test_infra.py`; run `terraform init -backend=false && terraform validate` in `infra/terraform/` by hand when you change it.

## Rules that keep the base trustworthy

- **Quotes are verbatim.** Code checks every quote against the transcript body. Never add a code path that lets a model's text become a stored quote, and never quote the recorder's AI notes.
- **No em dashes** in prompts, briefs, docs or any text the pipeline writes. Several tests check this. Use colons, commas or parentheses.
- **Prompts are code.** They live in `discovery/prompts/` as Python modules, one `str` constant per part (`SYSTEM`, `EXTRACT`, `REPAIR`...). Nothing in them names a market: domain wording comes from `config/domain.yml` through `discovery/prompts/__init__.py`.
- **Bump `prompt_version`** in `config/pipeline.yml` when you change `extract.py`, `consolidate.py`, `verdict.py`, `arbiter.py` or any model id. Every digest records the version it was made with, and `reprocess_stale: true` re-runs the stale ones.
- **Dry run pipeline changes** before you open a PR: `uv run python -m discovery run <transcript> --dry-run` (add `--fake` if you have no keys). For a prompt change, also run the bake-off on real models: `uv run python -m discovery bakeoff`.
- **Plain style** in anything a person reads: short sentences, lead with what changed, no filler.
- **No real call data** in issues, PRs or fixtures. Use made-up transcripts, or ones you have explicit permission to publish.

## Proposing a change

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Branch from `main`, keep the change focused, and add or update tests.
3. Run `uv run pytest` and a dry run.
4. Open a PR that says what changed, why, and how you checked it. Note any `prompt_version` bump.

`docs/SPEC.md` describes how the pipeline behaves, and code docstrings point into it (`SPEC "Nightly sweep"`). If your change alters behaviour, update the matching SPEC section in the same PR.

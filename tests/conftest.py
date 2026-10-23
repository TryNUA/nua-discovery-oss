"""Shared test helpers. Every test runs offline against a temporary repo copy."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FIXTURE_NAMES = ["darius-kemp", "lena-okafor", "viktor-lang", "wade-pruitt"]

# Config a person maintains by hand. The market (domain, seats, entities) is the insurance example the fixtures
# come from, the team is tests/config/people.yml, and pipeline.yml and overrides.yml are copied from the real
# repo with the team settings pinned (TEST_PIPELINE), so `nua init` on a clone never changes what tests see.
EXAMPLE = ROOT / "examples" / "insurance"
HAND_CONFIG = {"pipeline.yml": ROOT / "config", "people.yml": ROOT / "tests" / "config", "seats.yml": EXAMPLE,
               "entities.yml": EXAMPLE, "overrides.yml": ROOT / "config", "domain.yml": EXAMPLE}
TEST_PIPELINE = {"timezone": "America/Los_Angeles", "internal_email_domains": ["acme.dev"],
                 "channels": {"calls": "#discovery-calls", "ops": "#discovery-ops", "test": "#discovery-test"}}
# Config the processor writes: always starts empty, so live roll-up and backcheck state never leaks in.
PROCESSOR_CONFIG = ["proposals.yml", "themes.yml", "rejected.yml", "verdict_overrides.yml", "backcheck.yml"]
PROCESSOR_HEADER = "# Processor-written. Do not edit by hand.\n"


def fixture_text(name: str) -> str:
    """Raw text of tests/fixtures/<name>.md."""
    return (FIXTURES / f"{name}.md").read_text(encoding="utf-8")


def copy_config(dst_root: Path) -> Path:
    """Write <dst_root>/config as on day one: hand-maintained files copied, processor-written files empty."""
    config = dst_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    for name, folder in HAND_CONFIG.items():
        shutil.copy(folder / name, config / name)
    _pin_pipeline(config / "pipeline.yml")
    for name in PROCESSOR_CONFIG:
        (config / name).write_text(PROCESSOR_HEADER, encoding="utf-8")
    (config / "batches").mkdir(exist_ok=True)
    return config


def _pin_pipeline(path: Path) -> None:
    """Set TEST_PIPELINE's keys in a copied pipeline.yml, keeping everything else as written."""
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    y.representer.add_representer(type(None), lambda rep, _: rep.represent_scalar("tag:yaml.org,2002:null", "null"))
    with path.open(encoding="utf-8") as f:
        data = y.load(f)
    data.update(TEST_PIPELINE)
    with path.open("w", encoding="utf-8") as f:
        y.dump(data, f)


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A temporary repo root with day-one config/ (see copy_config) and empty data folders. Never the real repo.

    prompt_version is pinned to v1 so a bump in the real config/pipeline.yml does not make every test's
    digests look stale. Tests that need a stale run set ctx.config.pipeline.prompt_version themselves.
    ingest_cutoff is reset to null (day one: recorders not live), whatever go-live set in the real file.
    """
    copy_config(tmp_path)
    pipeline = tmp_path / "config" / "pipeline.yml"
    text = re.sub(r"(?m)^prompt_version:.*$", "prompt_version: v1", pipeline.read_text(encoding="utf-8"))
    text = re.sub(r"(?m)^ingest_cutoff:.*$", "ingest_cutoff: null", text)
    pipeline.write_text(text, encoding="utf-8")
    for d in ("transcripts", "digests", "prep", "requests"):
        (tmp_path / d).mkdir()
    (tmp_path / "beliefs.md").write_text("## Beliefs\n\n## Watching\n\n## Retired\n", encoding="utf-8")
    (tmp_path / "decisions.md").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def ctx(tmp_repo: Path):
    from nua.context import Ctx
    return Ctx.for_tests(tmp_repo)

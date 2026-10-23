"""Shared test helpers. Every test runs offline against a temporary repo copy."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FIXTURE_NAMES = ["darius-kemp", "lena-okafor", "viktor-lang", "wade-pruitt"]

# Config a person maintains by hand: copied from the real repo.
HAND_CONFIG = ["pipeline.yml", "people.yml", "seats.yml", "entities.yml", "overrides.yml", "domain.yml"]
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
    for name in HAND_CONFIG:
        shutil.copy(ROOT / "config" / name, config / name)
    for name in PROCESSOR_CONFIG:
        (config / name).write_text(PROCESSOR_HEADER, encoding="utf-8")
    (config / "batches").mkdir(exist_ok=True)
    return config


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
    from discovery.context import Ctx
    return Ctx.for_tests(tmp_repo)

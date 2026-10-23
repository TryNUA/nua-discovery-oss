"""nua/setup/configio.py and `nua init`, on temporary copies of the repo's config/. Offline."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from conftest import ROOT
from nua import __main__ as cli
from nua import config as config_mod
from nua.setup import configio

EM_DASH = chr(0x2014)
EXAMPLES = ROOT / "examples"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A repo root whose config/ is the shipped one (blank templates) plus the team and pipeline files."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    for name in configio.EXAMPLE_FILES:
        shutil.copy(EXAMPLES / "blank" / name, tmp_path / "config" / name)
    shutil.copy(ROOT / "tests" / "config" / "people.yml", tmp_path / "config" / "people.yml")
    return tmp_path


def filled(root: Path) -> dict:
    data = configio.load_setup(root)
    data.update(configio.example_setup(root, "freight-brokerage"))
    return data


# ---------- templates and examples ----------

def test_examples_are_listed_blank_first():
    assert configio.list_examples(ROOT)[:1] == ["blank"]
    assert {"insurance", "freight-brokerage"} <= set(configio.list_examples(ROOT))


@pytest.mark.parametrize("name", ["blank", "insurance", "freight-brokerage"])
def test_every_example_loads_and_has_no_em_dash(root, name):
    for file in configio.EXAMPLE_FILES:
        text = (EXAMPLES / name / file).read_text(encoding="utf-8")
        assert EM_DASH not in text, file
        yaml.safe_load(text)
    configio.apply_example(root, name)
    cfg = config_mod.load(root)
    assert cfg.domain.company
    if name != "blank":
        assert configio.validate_setup(configio.load_setup(root)) == []
        assert "other" in cfg.seats and "expert" in cfg.domain.roles


def test_shipped_config_is_the_blank_template_and_loads_with_neutral_defaults():
    for file in configio.EXAMPLE_FILES:
        assert (ROOT / "config" / file).read_text() == (EXAMPLES / "blank" / file).read_text(), file
    cfg = config_mod.load(ROOT)
    assert cfg.domain.company == "our company" and cfg.domain.roles == config_mod.DEFAULT_ROLES
    assert cfg.seats == [] and cfg.entities == {}


def test_tests_read_the_insurance_example(ctx):
    assert ctx.config.domain.market == "P&C insurance"
    assert "claims adjuster (staff)" in ctx.config.seats
    assert ctx.config.canonical("guide wire") == "Guidewire"


def test_apply_example_copies_only_the_market_files(root):
    people = (root / "config" / "people.yml").read_text()
    changed = configio.apply_example(root, "insurance")
    assert sorted(p.name for p in changed) == ["domain.yml", "entities.yml", "seats.yml"]
    assert (root / "config" / "people.yml").read_text() == people
    assert configio.apply_example(root, "insurance") == []
    with pytest.raises(ValueError, match="no example"):
        configio.apply_example(root, "../config")


# ---------- load and validate ----------

def test_load_setup_shape(root):
    data = configio.load_setup(root)
    assert set(data) == {"domain", "people", "seats", "pipeline", "examples"}
    assert set(data["domain"]) == {*configio.DOMAIN_TEXT, "scope", "roles"}
    assert data["domain"]["company"] == "" and data["domain"]["roles"] == {}
    assert data["people"]["alex"] == {"name": "Alex Rivera", "email": "alex.rivera@gmail.com", "slack_id": None,
                                      "github_login": None, "circleback_key_name": "circleback-key-alex"}
    assert data["seats"] == []
    assert data["pipeline"]["channels"] == configio.DEFAULT_CHANNELS
    assert set(data["pipeline"]) == {"timezone", "internal_email_domains", "channels", "ingest_cutoff"}


def test_blank_setup_names_what_is_missing(root):
    problems = configio.validate_setup(configio.load_setup(root))
    assert "config/domain.yml: company is empty" in problems
    assert all(p.startswith("config/domain.yml") for p in problems)


@pytest.mark.parametrize("change, expected", [
    (lambda d: d["pipeline"].update(timezone="Mars/Base"), "is not an IANA time zone"),
    (lambda d: d["pipeline"].update(timezone="Pacific Time"), "is not an IANA time zone"),
    (lambda d: d["pipeline"].update(internal_email_domains=["not a domain"]), "is not a domain"),
    (lambda d: d["pipeline"]["channels"].update(calls="discovery"), "Slack channel name"),
    (lambda d: d["pipeline"]["channels"].update(ops="#discovery-calls"), "must differ"),
    (lambda d: d["pipeline"].update(ingest_cutoff="next tuesday"), "ingest_cutoff"),
    (lambda d: d["people"].update(Bad={"name": "B"}), "short lowercase id"),
    (lambda d: d["people"]["alex"].update(email="nope"), "is not an email"),
    (lambda d: d["people"]["alex"].update(slack_id="alex"), "slack_id"),
    (lambda d: d.update(people={}), "at least one person"),
    (lambda d: d["domain"]["roles"].pop("expert"), "must keep expert"),
    (lambda d: d["domain"]["scope"].update(region="x"), "unknown key"),
    (lambda d: d.update(seats=["a", "A"]), "listed twice"),
])
def test_validate_setup_catches(root, change, expected):
    data = filled(root)
    assert configio.validate_setup(data) == []
    change(data)
    problems = configio.validate_setup(data)
    assert any(expected in p for p in problems), problems


def test_validate_checks_only_the_sections_given():
    assert configio.validate_setup({"pipeline": {"timezone": "Europe/Berlin"}}) == []
    assert configio.validate_setup({"pipeline": {"timezone": "Europe/Nowhere"}})


# ---------- save ----------

def test_save_keeps_comments_and_reads_back(root):
    data = filled(root)
    data["people"] = {"maya": {"name": "Maya Ortiz", "email": "Maya@Halyard.dev", "slack_id": "U0123ABCD"},
                      "alex": data["people"]["alex"]}
    data["pipeline"].update(timezone="America/Chicago", internal_email_domains=["halyard.dev"])
    data["pipeline"]["channels"]["calls"] = "#halyard-calls"
    changed = configio.save_setup(root, data)
    assert sorted(p.name for p in changed) == ["domain.yml", "people.yml", "pipeline.yml", "seats.yml"]

    domain = (root / "config" / "domain.yml").read_text()
    assert "# Your company, as the models should name it" in domain and "company: Halyard Labs" in domain
    assert "# the kind of organization a finding is about" in domain
    assert "# Examples of the short institution label" in domain
    pipeline = (root / "config" / "pipeline.yml").read_text()
    assert "# Model roles." in pipeline and "timezone: America/Chicago" in pipeline
    assert 'calls: "#halyard-calls"' in pipeline and "  - halyard.dev\n" in pipeline
    assert (root / "config" / "seats.yml").read_text().startswith("# Seat list:")
    assert "# Your team:" in (root / "config" / "people.yml").read_text()

    cfg = config_mod.load(root)
    assert cfg.domain.company == "Halyard Labs" and "carrier" in cfg.domain.roles
    assert cfg.domain.scope["party"].startswith("who the finding is about")
    assert cfg.seats[0] == "brokerage operations lead"
    assert [p.key for p in cfg.people] == ["alex", "maya"]
    maya = cfg.person("maya")
    assert maya.email == "maya@halyard.dev" and maya.circleback_key_name == "circleback-key-maya"
    assert cfg.pipeline.timezone == "America/Chicago" and cfg.pipeline.channels["calls"] == "#halyard-calls"
    assert cfg.pipeline.extractors, "model roles are left as written"

    assert configio.save_setup(root, configio.load_setup(root)) == [], "a second save writes nothing"


def test_save_of_an_unchanged_example_writes_nothing(root):
    configio.apply_example(root, "insurance")
    before = {p.name: p.read_text() for p in (root / "config").glob("*.yml")}
    assert configio.save_setup(root, configio.load_setup(root)) == []
    assert {p.name: p.read_text() for p in (root / "config").glob("*.yml")} == before


def test_save_refuses_invalid_data_and_writes_nothing(root):
    before = (root / "config" / "pipeline.yml").read_text()
    data = filled(root)
    data["pipeline"]["timezone"] = "Nowhere/Land"
    with pytest.raises(configio.SetupError) as err:
        configio.save_setup(root, data)
    assert any("timezone" in p for p in err.value.problems)
    assert (root / "config" / "pipeline.yml").read_text() == before
    assert (root / "config" / "domain.yml").read_text() == (EXAMPLES / "blank" / "domain.yml").read_text()


def test_save_writes_a_cutoff_the_loader_reads(root):
    data = filled(root)
    data["pipeline"]["ingest_cutoff"] = "2026-11-02T09:00:00-08:00"
    configio.save_setup(root, data)
    assert configio.load_setup(root)["pipeline"]["ingest_cutoff"] == "2026-11-02T09:00:00-08:00"
    from nua.rules import as_datetime
    assert as_datetime(config_mod.load(root).pipeline.ingest_cutoff).isoformat() == "2026-11-02T09:00:00-08:00"


def test_seats_rebuilt_keep_the_header_and_can_go_back_to_empty(root):
    configio.apply_example(root, "insurance")
    configio.save_setup(root, {"seats": ["head of operations", "other"]})
    text = (root / "config" / "seats.yml").read_text()
    assert text.startswith("# Example config/seats.yml") and "- head of operations\n- other\n" in text
    configio.save_setup(root, {"seats": []})
    assert config_mod.load(root).seats == []


# ---------- nua init ----------

def init(root: Path, *args: str, answers: list[str] | None = None, monkeypatch=None) -> int:
    if answers is not None:
        replies = iter(answers)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    return cli.main(["--root", str(root), "init", *args])


def test_init_example_yes(root, capsys):
    assert init(root, "--example", "insurance", "--yes") == 0
    out = capsys.readouterr().out
    assert "wrote config/domain.yml" in out
    assert config_mod.load(root).domain.market == "P&C insurance"
    assert init(root, "--example", "insurance", "--yes") == 0
    assert "nothing written" in capsys.readouterr().out


def test_init_flags_answer_every_question(root, capsys):
    code = init(root, "--example", "blank", "--yes", "--company", "Zephyr", "--team", "founders",
                "--about", "two founders deciding what to build for dental practices", "--market", "dental practices",
                "--exploring", "scheduling and insurance billing", "--interviewees", "office managers and dentists",
                "--person", "zoe,Zoe Park,zoe@zephyr.dev,U0ZOE1", "--person", "ian,Ian Ross,",
                "--internal-domain", "zephyr.dev", "--timezone", "Europe/London", "--calls-channel", "#zephyr-calls",
                "--seat", "office manager", "--seat", "other")
    assert code == 0, capsys.readouterr()
    cfg = config_mod.load(root)
    assert cfg.domain.company == "Zephyr" and cfg.seats == ["office manager", "other"]
    assert [(p.key, p.email, p.slack_id) for p in cfg.people] == [("zoe", "zoe@zephyr.dev", "U0ZOE1"), ("ian", None, None)]
    assert cfg.pipeline.internal_email_domains == ["zephyr.dev"] and cfg.pipeline.timezone == "Europe/London"
    assert cfg.pipeline.channels == {"calls": "#zephyr-calls", "ops": "#discovery-ops", "test": "#discovery-test"}
    out = capsys.readouterr().out
    assert "Europe/London" in out and "terraform.tfvars" in out


def test_init_refuses_a_bad_timezone_and_writes_nothing(root, capsys):
    before = {p.name: p.read_text() for p in (root / "config").glob("*.yml")}
    assert init(root, "--example", "insurance", "--yes", "--timezone", "Mars/Olympus") == 1
    assert "not an IANA time zone" in capsys.readouterr().out
    assert {p.name: p.read_text() for p in (root / "config").glob("*.yml")} == before


def test_init_blank_yes_reports_the_missing_answers(root, capsys):
    assert init(root, "--yes") == 1
    out = capsys.readouterr().out
    assert "Nothing written" in out and "company is empty" in out


def test_init_writes_time_zone_into_the_gcp_tfvars(root, capsys):
    tfvars = root / "infra" / "terraform" / "terraform.tfvars"
    tfvars.parent.mkdir(parents=True)
    tfvars.write_text('project_id = "p"\ntime_zone  = "America/Los_Angeles"\n')
    assert init(root, "--example", "insurance", "--yes", "--timezone", "Asia/Tokyo") == 0
    assert tfvars.read_text() == 'project_id = "p"\ntime_zone = "Asia/Tokyo"\n'
    assert "infra/aws/terraform/terraform.tfvars" in capsys.readouterr().out


def test_init_interactive(root, monkeypatch, capsys):
    answers = [
        "freight-brokerage",          # start from
        "Halyard", "", "", "", "", "",  # company, then keep the example's other domain answers
        "n",                          # do not keep the team
        "maya", "Maya Ortiz", "maya@halyard.dev", "",
        "",                           # no more teammates
        "halyard.dev, halyard.io",    # internal domains
        "Not/AZone", "America/Chicago",  # a bad zone is asked again
        "", "", "",                   # channels
        "y",                          # keep the example's seats
    ]
    assert init(root, answers=answers, monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "not an IANA time zone" in out
    cfg = config_mod.load(root)
    assert cfg.domain.company == "Halyard" and cfg.domain.market == "freight brokerage"
    assert [p.key for p in cfg.people] == ["maya"]
    assert cfg.pipeline.internal_email_domains == ["halyard.dev", "halyard.io"]
    assert cfg.pipeline.timezone == "America/Chicago"
    assert "dispatcher / track and trace" in cfg.seats


def test_configio_and_init_have_no_em_dash():
    for rel in ("nua/setup/configio.py", "nua/setup/init.py", "examples/README.md",
                "examples/insurance/README.md", "examples/freight-brokerage/README.md"):
        assert EM_DASH not in (ROOT / rel).read_text(encoding="utf-8"), rel

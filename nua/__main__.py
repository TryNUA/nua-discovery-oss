"""Command line for a local clone: `uv run python -m nua <command>` (or `uv run nua <command>`).

    run <transcript> [--dry-run] [--beliefs PATH] [--decisions PATH] [--prep PATH] [--fake] [--[no-]digest]
    backfill <paths...>                   local processing, no Slack
    ask "<question>" [--since DATE] [--seat SEAT]
    reindex | rollup [--bootstrap] | sweep | backcheck
    bakeoff [--models spec,spec,spec] [--labels DIR] | label <transcript> [--labels DIR]
    init [--example NAME] [--yes] [answer flags]
    slack-manifest [--receiver-url URL]   the Slack app manifest and its one-click create link
    github-app --repo OWNER/NAME [--owner USER_OR_ORG] [--receiver-url URL]   register the GitHub App
    setup [--port N] [--no-browser]       a local setup page (config, keys, GitHub and Slack apps, deploy checks)

Every command takes `--root` (default: $NUA_ROOT, else $NUA_REPO_ROOT, else the nearest folder
above the current one with config/pipeline.yml; no such folder is an error) and `--fake`
(label-backed fake models from nua/bakeoff.py, so nothing needs a key).

Commands run against a local context: files are written to the working tree, nothing is committed
or pushed, nothing is sent to Slack (messages a task would post are printed to stderr as a preview)
and follow-on tasks are listed, not run. You review and commit the changes yourself. The deployed
services do the rest.

`run --dry-run` works in a temporary copy of the repo and leaves the working tree untouched. It
prints the brief as plain text (`brief.render_text`) on stdout, then the digest on stderr, so stdout
stays the brief alone for golden comparisons. `--digest` prints the digest on stdout instead (dry
run or not), `--no-digest` leaves it out. A re-run that keeps the existing digest because the
transcript now fails to parse, now reads as internal or is gone (`skipped:parse_failed`,
`skipped:internal`, `skipped:missing`) prints one line saying so instead of the old digest's brief,
and exits 1: the brief on stdout is always this run's output, so a golden comparison catches the regression.

`ask` exits 1 when it read none of the digests it should have (every map batch failed); a partial
read exits 0 and the coverage line says how much was read.

Model keys come from the environment or a gitignored `.env` at the repo root, which this module
loads once the root is known (variables already set win).
"""
from __future__ import annotations

import argparse
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from nua import config as config_mod
from nua.beliefs import EMPTY_BELIEFS, parse_beliefs, parse_decisions
from nua.brief import acted_on, render_brief, render_text
from nua.context import Ctx
from nua.digest import (DigestError, Unreadable, apply_overrides, digest_for_transcript, digest_path, is_unreadable,
                        load_digests, override_problems, read_digest, write_digest)
from nua.llm import CostTracker, LLMFactory
from nua.ports import FakeSlack, NullRemote, RecordingQueue, RepoFS
from nua.types import Digest

# Copied into the scratch repo for `run --dry-run`.
REPO_DIRS = ("config", "transcripts", "digests", "prep", "requests")
REPO_FILES = ("beliefs.md", "decisions.md", "beliefs-status.md", "findings.md", "open-questions.md",
              "calls.csv", "claims.csv")
TRANSCRIPTS = "transcripts"
BAKEOFF_CAP_USD = 25.0
OK_OUTCOMES = ("digested", "superseded", "duplicate", "stub:internal")
# process._KEPT: the re-run failed and process_call kept the existing digest. Its brief is not this
# run's output, so it is never printed as if it were.
KEPT_OUTCOMES = ("skipped:missing", "skipped:internal", "skipped:parse_failed")


class CliError(Exception):
    """A problem the user can fix; printed without a traceback."""


def _mod(name: str):
    """Import `nua.<name>` at call time, so a missing optional module only breaks its own command."""
    return importlib.import_module(f"nua.{name}")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------- repo and context ----------

ROOT_ENV = ("NUA_ROOT", "NUA_REPO_ROOT")     # NUA_REPO_ROOT is the name nua/settings.py reads


def _is_repo(folder: Path) -> bool:
    return (folder / "config" / "pipeline.yml").is_file()


def find_root(explicit: str | None) -> Path:
    """Repo root: `--root`, else $NUA_ROOT, else $NUA_REPO_ROOT, else the nearest folder at or above
    the current one with config/pipeline.yml.

    Raises CliError when there is no such folder, or when the root given has no config/pipeline.yml,
    so a command run outside the clone fails instead of reading an empty repo.
    """
    given = explicit or next((os.environ[k] for k in ROOT_ENV if os.environ.get(k, "").strip()), None)
    if given:
        root = Path(given).expanduser().resolve()
        if not _is_repo(root):
            raise CliError(f"{root} has no config/pipeline.yml: pass --root with the repo root")
        return root
    here = Path.cwd().resolve()
    for folder in (here, *here.parents):
        if _is_repo(folder):
            return folder
    raise CliError("no repo found: pass --root or set NUA_ROOT")


def _load_env(root: Path) -> None:
    """Read model and service keys from `<root>/.env` when present. Existing env vars win."""
    path = root / ".env"
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


def local_ctx(root: Path, *, fake: bool = False, cap_usd: float | None = None,
              labels_root: Path | None = None) -> Ctx:
    """A context for local runs: working-tree files, no pushes, Slack preview only, tasks recorded.

    `dry_run` is set, so process_call writes files but skips Slack and the remote.
    """
    cfg = config_mod.load(root)
    cap = cap_usd if cap_usd is not None else cfg.pipeline.cost_cap_usd_per_task
    ctx = Ctx(config=cfg, repo=RepoFS(root), llms=LLMFactory(CostTracker(cap_usd=cap)), remote=NullRemote(),
              slack=FakeSlack(), queue=RecordingQueue(), dry_run=True)
    if fake:
        _mod("bakeoff").install_fakes(ctx, root=labels_root or root)
    return ctx


def _ctx(args: argparse.Namespace, root: Path, *, cap_usd: float | None = None,
         labels_root: Path | None = None) -> Ctx:
    """The command's `local_ctx`, kept on `args` so `main` can print its log with `--verbose`."""
    ctx = local_ctx(root, fake=args.fake, cap_usd=cap_usd, labels_root=labels_root)
    args.built_ctx = ctx
    return ctx


def _reset_cost(ctx: Ctx) -> None:
    """The cost cap is per task: start each call's budget at zero."""
    if ctx.llms.cost is not None:
        ctx.llms.cost.spent_usd = 0.0


def _report_side_effects(ctx: Ctx) -> None:
    """Print what a local run would have sent to Slack or queued, to stderr."""
    if isinstance(ctx.slack, FakeSlack):
        sent = ctx.slack.live()
        if sent:
            _err(f"Slack preview, not sent (local run): {len(sent)} message(s)")
            for m in sent:
                where = f"{m['channel']}" + (" (thread)" if m.get("thread_ts") else "")
                _err(f"--- {where} ---\n{m['text']}")
    if isinstance(ctx.queue, RecordingQueue) and ctx.queue.tasks:
        kinds = ", ".join(t["kind"] for t in ctx.queue.tasks)
        _err(f"Queued, not run (local run): {kinds}")


def _repo_path(root: Path, path: str | None) -> str | None:
    """A user path as the repo sees it: relative to the root when inside it, else absolute."""
    if path is None:
        return None
    p = Path(path).resolve()
    try:
        return p.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p)


def place_transcript(repo: RepoFS, src: Path, real_root: Path, *, rename: bool = False) -> str:
    """Repo-relative path of the transcript to process, copying it into `transcripts/` when needed.

    A file already under the real repo's `transcripts/` is used where it is. Anything else is
    copied, unchanged, to `transcripts/<name>`. A different file already there is an error, or with
    `rename` (dry runs) the copy goes to `transcripts/<stem>-2.md`, `-3`, ... instead.
    """
    src = src.resolve()
    if not src.is_file():
        raise CliError(f"no such file: {src}")
    try:
        rel = src.relative_to((real_root / TRANSCRIPTS).resolve())
        return f"{TRANSCRIPTS}/{rel.as_posix()}"
    except ValueError:
        pass
    text = src.read_text(encoding="utf-8")
    dest, n = f"{TRANSCRIPTS}/{src.name}", 2
    while repo.exists(dest) and repo.read(dest) != text:
        if not rename:
            raise CliError(f"{dest} already exists with different content; rename the file to add it")
        dest, n = f"{TRANSCRIPTS}/{src.stem}-{n}{src.suffix}", n + 1
    if not repo.exists(dest):
        repo.write(dest, text)
    return dest


def _copy_repo(root: Path, dest: Path) -> None:
    """The parts of the repo a pipeline run reads, copied into `dest`."""
    skip = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in REPO_DIRS:
        src = root / name
        if src.is_dir():
            shutil.copytree(src, dest / name, ignore=skip)
        else:
            (dest / name).mkdir(parents=True, exist_ok=True)
    for name in REPO_FILES:
        if (root / name).is_file():
            shutil.copy2(root / name, dest / name)
    if not (dest / "beliefs.md").exists():
        (dest / "beliefs.md").write_text(EMPTY_BELIEFS, encoding="utf-8")


# ---------- brief ----------

def brief_text(ctx: Ctx, d: Digest, *, beliefs_path: str = "beliefs.md", decisions_path: str = "decisions.md") -> str:
    """The brief for `d` as plain text, rendered against the given beliefs and decisions files."""
    beliefs = parse_beliefs(ctx.repo.read_or(beliefs_path, EMPTY_BELIEFS))
    decisions = parse_decisions(ctx.repo.read_or(decisions_path, ""))
    indexes = _mod("indexes")
    counts = indexes.evidence_counts(load_digests(ctx.repo), beliefs,
                                     overrides=indexes.load_verdict_overrides(ctx.repo))
    spans, acted = acted_on(d, beliefs)
    return render_text(render_brief(d, beliefs, decisions, counts=counts, acted_sources=acted, acted_spans=spans))


def _result_digest(ctx: Ctx, result: Any, transcript: str) -> Digest | None:
    """The digest process_call wrote or found for `transcript`. A digest file that cannot be read (a
    `skipped:unreadable` result) comes back as its stand-in (is_unreadable), with the reason."""
    call_id = getattr(result, "call_id", None)
    if call_id and ctx.repo.exists(digest_path(call_id)):
        rel = digest_path(call_id)
        try:
            return read_digest(ctx.repo, rel)
        except (DigestError, OSError) as e:
            return Unreadable(path=rel, call_id=call_id, transcript=transcript,
                              error=" ".join(str(e).split())).stand_in()
    return digest_for_transcript(load_digests(ctx.repo), transcript)


def _unreadable_line(d: Digest) -> str:
    """One line naming a digest file that cannot be read and why."""
    rel = digest_path(d.call_id)
    reason = d.reason or "cannot be read"
    if reason.startswith(f"{rel}: "):
        reason = reason[len(rel) + 2:]
    return f"Unreadable digest {rel}: {reason}. Fix it by hand; the pipeline never overwrites it."


def _show_raw(ctx: Ctx, rel: str) -> str:
    """A digest file's text for --digest, even when it is not UTF-8; a note when it cannot be read at all."""
    try:
        return ctx.repo.read_bytes(rel).decode("utf-8", errors="replace")
    except OSError as e:
        return f"(cannot be read: {e})\n"


def _brief_for(ctx: Ctx, process: Any, result: Any, d: Digest, *, beliefs_path: str, decisions_path: str) -> str:
    """The brief as the pipeline renders it: the dry-run text process_call returned, else
    `process.build_brief` (header with call length), else `brief_text`."""
    text = getattr(result, "brief_text", None)
    if text:
        return text
    build = getattr(process, "build_brief", None)
    if build is not None:
        return render_text(build(ctx, d, beliefs_path=beliefs_path, decisions_path=decisions_path))
    return brief_text(ctx, d, beliefs_path=beliefs_path, decisions_path=decisions_path)


def _kept_line(outcome: str, kept: Digest | None) -> str:
    """One line for a run that kept an earlier digest: what this run found, and which file was kept."""
    status = outcome.split(":", 1)[1]
    if kept is None:
        return f"Not digested: {status}."
    return (f"{status} on re-run: existing digest {digest_path(kept.call_id)} kept. Its brief is not this "
            f"run's output, so it is not shown.")


def _process_and_print(ctx: Ctx, transcript: str, *, force: bool, beliefs_path: str, decisions_path: str,
                       prep_path: str | None, show_digest: str | None, label: str) -> int:
    """process_call one transcript, then print its brief (or its stub), and the digest file after it
    on `show_digest` ("stdout" or "stderr"; None skips it). Returns the exit code.

    A digest kept from an earlier run (KEPT_OUTCOMES: this run failed to parse, read the call as
    internal or found no transcript) is named in one line, never rendered as this run's brief, and
    exits 1, like the parse_failed stub. Only skipped:current and skipped:stub (a non-forced run that had
    nothing to do) print the existing brief or stub."""
    _reset_cost(ctx)
    process = _mod("process")
    result = process.process_call(ctx, transcript, force=force, beliefs_path=beliefs_path,
                                  decisions_path=decisions_path, prep_path=prep_path)
    outcome = str(getattr(result, "outcome", ""))
    _err(f"{label}: {outcome}" + (f" ({result.call_id})" if getattr(result, "call_id", None) else ""))
    if outcome in KEPT_OUTCOMES:
        print(_kept_line(outcome, _result_digest(ctx, result, transcript)))
        return 1
    d = _result_digest(ctx, result, transcript) if outcome != "duplicate" else None
    if d is not None and d.status == "digested":
        print(_brief_for(ctx, process, result, d, beliefs_path=beliefs_path, decisions_path=decisions_path), end="")
    elif d is not None and is_unreadable(d):
        print(_unreadable_line(d))
    elif d is not None:
        print(f"Stub digest {digest_path(d.call_id)}: {d.status}" + (f" ({d.reason})" if d.reason else ""))
    if show_digest and d is not None:
        rel = digest_path(d.call_id)
        text = _show_raw(ctx, rel) if is_unreadable(d) else ctx.repo.read(rel)
        print(f"\n=== {rel} ===\n{text}", end="", file=sys.stderr if show_digest == "stderr" else sys.stdout)
    return 0 if outcome in OK_OUTCOMES or outcome.startswith("skipped") else 1


# ---------- commands ----------

def _digest_stream(args: argparse.Namespace) -> str | None:
    """Where `run` prints the digest: stdout with --digest, nowhere with --no-digest, else stderr on
    a dry run (SPEC: the dry run prints the digest and brief) and nowhere on a real run."""
    if args.digest is not None:
        return "stdout" if args.digest else None
    return "stderr" if args.dry_run else None


def cmd_run(args: argparse.Namespace, root: Path) -> int:
    """`run`: process one transcript in the working tree, or with `--dry-run` in a temporary copy."""
    src = Path(args.transcript)
    show_digest = _digest_stream(args)
    if not args.dry_run:
        if args.fake:
            _err(f"warning: --fake without --dry-run writes fake digests into {root}")
        ctx = _ctx(args, root)
        rel = place_transcript(ctx.repo, src, root)
        code = _process_and_print(ctx, rel, force=args.force,
                                  beliefs_path=_repo_path(root, args.beliefs) or "beliefs.md",
                                  decisions_path=_repo_path(root, args.decisions) or "decisions.md",
                                  prep_path=_repo_path(root, args.prep), show_digest=show_digest, label=rel)
        _report_side_effects(ctx)
        return code

    with tempfile.TemporaryDirectory(prefix="discovery-dry-run-") as tmp:
        scratch = Path(tmp)
        _copy_repo(root, scratch)
        if args.beliefs:
            shutil.copyfile(args.beliefs, scratch / "beliefs.md")
        if args.decisions:
            shutil.copyfile(args.decisions, scratch / "decisions.md")
        prep_path = None
        if args.prep:
            prep_path = f"prep/{Path(args.prep).name}"
            shutil.copyfile(args.prep, scratch / prep_path)
        ctx = _ctx(args, scratch, labels_root=root)
        rel = place_transcript(ctx.repo, src, root, rename=True)
        return _process_and_print(ctx, rel, force=True, beliefs_path="beliefs.md", decisions_path="decisions.md",
                                  prep_path=prep_path, show_digest=show_digest, label=f"{rel} (dry run)")


def _expand(paths: list[str]) -> list[Path]:
    """Files as given, folders as their *.md files, each once."""
    out: list[Path] = []
    for p in paths:
        path = Path(p)
        found = sorted(path.glob("*.md")) if path.is_dir() else [path]
        for f in found:
            if f.resolve() not in {x.resolve() for x in out}:
                out.append(f)
    return out


def cmd_backfill(args: argparse.Namespace, root: Path) -> int:
    """`backfill`: process_call each path in turn (no batch file, no Slack), then print a summary
    with the fix-up list. One failure never stops the rest."""
    ctx = _ctx(args, root)
    process = _mod("process")
    outcomes: dict[str, int] = {}
    call_ids: list[str] = []
    errors = 0
    for src in _expand(args.paths):
        label = str(src)
        try:
            rel = place_transcript(ctx.repo, src, root)
            label = rel
            _reset_cost(ctx)
            result = process.process_call(ctx, rel, force=args.force)
        except Exception as e:     # keep going; one bad file never stops a batch
            errors += 1
            outcomes["error"] = outcomes.get("error", 0) + 1
            print(f"{label}: error: {e}")
            continue
        outcome = str(result.outcome)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if result.call_id:
            call_ids.append(result.call_id)
        print(f"{label}: {outcome}" + (f" {result.call_id}" if result.call_id else ""))

    print(f"\nBackfill: {sum(outcomes.values())} transcript(s)")
    for outcome, n in sorted(outcomes.items()):
        print(f"  {outcome}: {n}")
    fixups = _fixups(ctx, call_ids)
    if fixups:
        print("Fix-up list (correct in config/overrides.yml):")
        for line in fixups:
            print(f"  {line}")
    _report_side_effects(ctx)
    return 1 if errors or outcomes.get("stub:failed") or outcomes.get("stub:parse_failed") else 0


def _fixups(ctx: Ctx, call_ids: list[str]) -> list[str]:
    """Unknown dates and firms, failed stubs, and digest files that cannot be read (fixed by hand in
    the file itself), among the digests just written or found."""
    lines = []
    for call_id in call_ids:
        rel = digest_path(call_id)
        if not ctx.repo.exists(rel):
            continue
        try:
            d = read_digest(ctx.repo, rel)
        except (DigestError, OSError) as e:
            reason = " ".join(str(e).split())
            reason = reason[len(rel) + 2:] if reason.startswith(f"{rel}: ") else reason
            lines.append(f"{call_id}: {rel} cannot be read ({reason}); fix the file by hand")
            continue
        if d.status in ("failed", "parse_failed"):
            lines.append(f"{call_id}: {d.status}" + (f" ({d.reason})" if d.reason else ""))
            continue
        if d.status != "digested":
            continue
        missing = [what for what, bad in (("date", d.date_source == "unknown" or not d.date),
                                          ("firm", not d.firm)) if bad]
        if missing:
            lines.append(f"{call_id}: {' and '.join(missing)} unknown")
    return lines


def cmd_ask(args: argparse.Namespace, root: Path) -> int:
    """`ask`: map/reduce over every digest; prints the answer, citations and coverage.

    Exits 1 when there were digests to read and none was read (every map batch failed, e.g. a
    missing model key), so a caller going by the exit code sees the failure.
    """
    ctx = _ctx(args, root)

    def progress(done: int, total: int) -> None:     # "processed": only the coverage line says "read"
        print(f"\rprocessed {done}/{total}", end="" if done < total else "\n", file=sys.stderr, flush=True)

    res = _mod("ask").ask(ctx, args.question, since=args.since, seat=args.seat, progress=progress)
    print(res.answer.rstrip())
    if res.citations:
        print("\nCitations:")
        for c in res.citations:
            print(f'- {c.get("call_id")}: "{c.get("quote")}"' + ("" if c.get("verified") else "  (not verified)"))
    print(f"\n{res.coverage}")
    return 1 if res.total > 0 and res.read == 0 else 0


def _apply_digest_overrides(ctx: Ctx) -> list[str]:
    """Rewrite digests whose front-matter `config/overrides.yml` changes. Returns their paths."""
    changed = []
    for d in load_digests(ctx.repo):
        fixed = apply_overrides(d, ctx.config.overrides)
        if fixed.to_dict() != d.to_dict():
            changed.append(write_digest(ctx.repo, fixed))
    return changed


def cmd_reindex(args: argparse.Namespace, root: Path) -> int:
    """`reindex`: rewrite digests changed by config/overrides.yml, then regenerate the indexes. Prints the
    paths written, then one line per correction in config/overrides.yml that was skipped."""
    ctx = _ctx(args, root)
    for rel in _apply_digest_overrides(ctx):
        print(rel)
    for rel in _mod("indexes").regenerate(ctx):
        print(rel)
    for line in override_problems(ctx.config.overrides):
        print(line)
    _report_side_effects(ctx)
    return 0


def _print_result(result: Any) -> None:
    """Print a dict or dataclass result as `key: value` lines, lists joined by commas; a `text`
    field (a task's own summary) is printed last, as is."""
    data = result if isinstance(result, dict) else vars(result) if hasattr(result, "__dict__") else {"result": result}
    for key, value in data.items():
        if key == "text":
            continue
        if isinstance(value, (list, tuple, set)):
            value = ", ".join(str(v) for v in value) or "none"
        print(f"{key}: {value}")
    if data.get("text"):
        print(f"\n{str(data['text']).rstrip()}")


def cmd_rollup(args: argparse.Namespace, root: Path) -> int:
    """`rollup`: the weekly roll-up, or the bootstrap with `--bootstrap`."""
    ctx = _ctx(args, root)
    _print_result(_mod("rollup").run_rollup(ctx, bootstrap=args.bootstrap, week=args.week))
    _report_side_effects(ctx)
    return 0


def cmd_sweep(args: argparse.Namespace, root: Path) -> int:
    """`sweep`: the nightly sweep. Recorder clients come from the environment (GRANOLA_API_KEY,
    CIRCLEBACK_API_KEY_<PERSON>) through the same helpers the services use; a missing key skips that recorder."""
    ctx = _ctx(args, root)
    settings = _mod("settings")
    report = _mod("sweep").run_sweep(ctx, granola=settings.granola_client(),
                                     circleback=settings.circleback_clients(ctx.config),
                                     granola_only=args.granola_only, last_sweep=args.last_sweep)
    _print_result(report)
    _report_side_effects(ctx)
    return 1 if getattr(report, "errors", None) else 0


def cmd_backcheck(args: argparse.Namespace, root: Path) -> int:
    """`backcheck`: check past digests against belief versions they have not seen."""
    ctx = _ctx(args, root)
    _print_result(_mod("backcheck").backcheck(ctx))
    _report_side_effects(ctx)
    return 0


def cmd_bakeoff(args: argparse.Namespace, root: Path) -> int:
    """`bakeoff`: score models on the golden set, print the tables, write docs/research/bakeoff-<date>.md."""
    ctx = _ctx(args, root, cap_usd=args.cap_usd)
    bakeoff = _mod("bakeoff")
    if args.fake and (args.models or args.labels):
        bakeoff.install_fakes(ctx, args.models, root=root, labels=args.labels)
    report = bakeoff.run_bakeoff(ctx, args.models, root=root, labels=args.labels, progress=_err)
    print(report.table(), end="")
    if report.cost_usd is not None:
        print(f"\nEstimated model cost: ${report.cost_usd:.2f}")
    print(f"Report: {report.path}")
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace, Path], int]] = {
    "run": cmd_run, "backfill": cmd_backfill, "ask": cmd_ask, "reindex": cmd_reindex, "rollup": cmd_rollup,
    "sweep": cmd_sweep, "backcheck": cmd_backcheck, "bakeoff": cmd_bakeoff,
    "slack-manifest": lambda args, root: _mod("setup.slack").command(args, root),
    "github-app": lambda args, root: _mod("setup.github_app").command(args, root),
    "setup": lambda args, root: _mod("setup.server").command(args, root),
    "label": lambda args, root: _mod("bakeoff").label_command(args, _ctx(args, root), root),
    "init": lambda args, root: _mod("setup.init").command(args, root),
}


# ---------- arguments ----------

def _spec_list(text: str) -> list[str]:
    specs = [s.strip() for s in text.split(",") if s.strip()]
    if not specs:
        raise argparse.ArgumentTypeError("give at least one model spec, e.g. anthropic:claude-sonnet-5")
    return specs


def build_parser() -> argparse.ArgumentParser:
    """The `python -m nua` argument parser."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help="repo root (also accepted before the command)")
    common.add_argument("--fake", action="store_true",
                        help="use label-backed fake models (tests/labels); no keys needed")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="print the pipeline log, and tracebacks on errors, to stderr")

    parser = argparse.ArgumentParser(prog="python -m nua", description="nua-discovery: local commands.")
    parser.add_argument("--root", help="repo root (default: $NUA_ROOT, $NUA_REPO_ROOT or the nearest folder "
                                       "with config/pipeline.yml)")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("run", parents=[common], help="process one transcript and print its brief")
    p.add_argument("transcript", help="transcript file; copied into transcripts/ when it is not there already")
    p.add_argument("--dry-run", action="store_true", help="work in a temporary copy; change nothing, post nothing")
    p.add_argument("--beliefs", help="beliefs file to check against (default: beliefs.md)")
    p.add_argument("--decisions", help="decisions file (default: decisions.md)")
    p.add_argument("--prep", help="use this prep file instead of matching one")
    p.add_argument("--force", action="store_true", help="re-run even if a digest is current (dry runs always do)")
    p.add_argument("--digest", action=argparse.BooleanOptionalAction, default=None,
                   help="print the digest file on stdout after the brief (--no-digest: not at all; "
                        "default: on stderr for --dry-run, not for a real run)")

    p = sub.add_parser("backfill", parents=[common], help="process dropped transcripts locally, no Slack")
    p.add_argument("paths", nargs="+", help="transcript files or folders of .md files")
    p.add_argument("--force", action="store_true", help="re-run transcripts whose digest is current")

    p = sub.add_parser("ask", parents=[common], help="answer a question from every digest")
    p.add_argument("question")
    p.add_argument("--since", help="only calls on or after this date (YYYY-MM-DD)")
    p.add_argument("--seat", help="only calls from this seat")

    sub.add_parser("reindex", parents=[common], help="apply overrides and regenerate the index files")

    p = sub.add_parser("rollup", parents=[common], help="weekly roll-up (or the bootstrap)")
    p.add_argument("--bootstrap", action="store_true", help="propose the first beliefs from the backfill")
    p.add_argument("--week", help="ISO week key, e.g. 2026-W40 (default: this week)")

    p = sub.add_parser("sweep", parents=[common], help="nightly sweep")
    p.add_argument("--granola-only", action="store_true", help="only list and ingest Granola notes")
    p.add_argument("--last-sweep", help="ISO timestamp of the previous sweep, for untagged-call reminders")

    sub.add_parser("backcheck", parents=[common], help="check past calls against new or changed beliefs")

    p = sub.add_parser("bakeoff", parents=[common], help="score models as extractor and judge on the golden set")
    p.add_argument("--models", type=_spec_list, help="comma-separated specs (default: extractors and arbiter)")
    p.add_argument("--cap-usd", type=float, default=BAKEOFF_CAP_USD, help="cost cap for the whole bake-off")
    p.add_argument("--labels", help="labels folder to score on (default: tests/labels)")
    _mod("bakeoff").add_label_arguments(sub.add_parser("label", parents=[common], help="draft a label file from one extraction"))
    _mod("setup.init").add_arguments(sub.add_parser("init", parents=[common], help="write config/ for your team"))

    p = sub.add_parser("slack-manifest", parents=[common], help="print the Slack app manifest and its create link")
    _mod("setup.slack").add_arguments(p)
    p = sub.add_parser("github-app", parents=[common], help="register the GitHub App and write .env and tfvars")
    _mod("setup.github_app").add_arguments(p)
    _mod("setup.server").add_arguments(sub.add_parser("setup", parents=[common], help="open the local setup page"))
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one command. Returns the process exit code: 0 ok, 1 failed, 2 usage."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        root = find_root(args.root)
        _load_env(root)
        return COMMANDS[args.command](args, root)
    except CliError as e:
        _err(f"error: {e}")
        return 1
    except Exception as e:     # a failed command prints one line; --verbose adds the traceback
        _err(f"error: {type(e).__name__}: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1
    finally:
        ctx = getattr(args, "built_ctx", None)
        if args.verbose and ctx is not None and ctx.log:
            _err("log:\n" + "\n".join(f"  {line}" for line in ctx.log))


if __name__ == "__main__":
    sys.exit(main())

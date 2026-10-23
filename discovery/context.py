"""Everything a task needs, passed explicitly. Tests build one with fakes via `Ctx.for_tests`."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from discovery import config as config_mod
from discovery.config import Config
from discovery.llm import CostTracker, LLMFactory
from discovery.ports import FakeSlack, NullRemote, RecordingQueue, Remote, RepoFS, SlackIO, TaskQueue

log = logging.getLogger("discovery")


@dataclass
class Ctx:
    config: Config
    repo: RepoFS
    llms: LLMFactory
    remote: Remote = field(default_factory=NullRemote)
    slack: SlackIO | None = None
    queue: TaskQueue | None = None
    now: Callable[[], datetime] = field(default_factory=lambda: (lambda: datetime.now(ZoneInfo("America/Los_Angeles"))))
    dry_run: bool = False
    log: list[str] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.config.pipeline.timezone)

    def channel(self, name: str) -> str:
        """Slack channel for 'calls' | 'ops' | 'test'."""
        return self.config.pipeline.channels[name]

    def info(self, msg: str) -> None:
        """Record `msg` in `log` (tests and dry runs read it) and log it at INFO on the "discovery" logger, so it
        reaches Cloud Logging in the service."""
        self.log.append(msg)
        log.info("%s", msg)

    @classmethod
    def for_tests(cls, root: str | Path, *, now: datetime | None = None) -> "Ctx":
        cfg = config_mod.load(root)
        fixed = now or datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        return cls(config=cfg, repo=RepoFS(root), llms=LLMFactory(CostTracker(cap_usd=1e9)),
                   remote=NullRemote(), slack=FakeSlack(), queue=RecordingQueue(), now=lambda: fixed)

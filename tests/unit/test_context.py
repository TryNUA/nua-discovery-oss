"""Tests for nua/context.py: Ctx.info keeps each line and logs it for Cloud Logging. Offline."""
from __future__ import annotations

import logging


def test_info_is_kept_and_logged_at_info_on_the_internal_logger(ctx, caplog):
    caplog.set_level(logging.INFO, logger="nua")
    ctx.info("sweep: front-matter of transcripts/x.md unreadable (YAMLError)")
    assert ctx.log == ["sweep: front-matter of transcripts/x.md unreadable (YAMLError)"]
    [rec] = [r for r in caplog.records if r.name == "nua"]
    assert (rec.levelno, rec.getMessage()) == (logging.INFO,
                                               "sweep: front-matter of transcripts/x.md unreadable (YAMLError)")


def test_info_text_is_never_treated_as_a_format_string(ctx, caplog):
    caplog.set_level(logging.INFO, logger="nua")
    ctx.info("cost 100% of cap, %s %d")
    assert [r.getMessage() for r in caplog.records if r.name == "nua"] == ["cost 100% of cap, %s %d"]

"""Model prompts, one module per pipeline step, each part a module-level str constant.

extract (SYSTEM, EXTRACT, REPAIR), consolidate (SYSTEM, CONSOLIDATE), verdict (SYSTEM, VERDICT), arbiter (SYSTEM, ARBITER), ask
(SYSTEM, MAP, REDUCE) and rollup (SYSTEM, WEEKLY, BOOTSTRAP, OUTPUT). Placeholders in single braces
are filled with str.format_map (`nua.extract.fill`); a literal brace is written doubled.
`prompt_version` in config/pipeline.yml covers extract, consolidate, verdict and arbiter.

Nothing here names a market: the domain comes from config/domain.yml through `domain_values`, whose
keys every caller merges into the values it fills a prompt with ({company}, {about}, {market},
{exploring}, {scope_line}, {scope_party}, {scope_segment}, {roles}, {institution_examples},
{bot_name}). `{about}` is derived from the market; the reader is always "the team".
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nua.config import Config


def domain_values(config: "Config") -> dict[str, str]:
    """The placeholders config/domain.yml fills in every prompt."""
    d = config.domain
    return {
        "company": d.company, "about": f"a team deciding what to build for {d.market}", "market": d.market,
        "exploring": d.exploring,
        "scope_line": d.scope.get("line", ""), "scope_party": d.scope.get("party", ""),
        "scope_segment": d.scope.get("segment", ""),
        "roles": "; ".join(f"{k} ({v})" if v else k for k, v in d.roles.items()),
        "institution_examples": d.institution_examples, "bot_name": d.bot_name,
    }

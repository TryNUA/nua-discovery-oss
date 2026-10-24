"""The Slack app, created from a manifest in two clicks.

`render_manifest` builds the same app as docs/slack-app-manifest.yml (the manual fallback) for one
receiver URL and bot name. `create_app_url` turns it into Slack's prefilled create link,
`https://api.slack.com/apps?new_app=1&manifest_json=<url-encoded JSON>` ("Sharing manifests" in
https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests): open it, pick the
workspace, Create. `nua slack-manifest` prints both.

Bot scopes, from what nua/slackio.py and nua/slack_ask.py call (the manual manifest explains each).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import quote

import yaml

from nua.setup import receiver

CREATE_URL = "https://api.slack.com/apps?new_app=1&manifest_json="
BOT_SCOPES = ["app_mentions:read", "channels:history", "channels:read", "chat:write", "groups:history",
              "groups:read"]
DESCRIPTION = "Reads every customer discovery call, posts briefs and answers questions with verified quotes."


def render_manifest(receiver_url: str, bot_name: str, app_name: str | None = None, *,
                    events: bool = True) -> dict:
    """The Slack app manifest for this receiver. `events=False` leaves out Event Subscriptions, for
    when Slack refuses to create the app because the receiver does not answer yet; turn them on
    once it is deployed (docs/self-hosting.md, step 9)."""
    base = receiver.normalize(receiver_url)
    settings: dict = {}
    if events:
        settings["event_subscriptions"] = {"request_url": f"{base}/slack/events", "bot_events": ["app_mention"]}
    settings.update({
        "interactivity": {"is_enabled": True, "request_url": f"{base}/slack/interact"},
        "org_deploy_enabled": False,
        "socket_mode_enabled": False,
        "token_rotation_enabled": False,
    })
    return {
        "display_information": {"name": app_name or bot_name, "description": DESCRIPTION,
                                "background_color": "#2c2d30"},
        "features": {"bot_user": {"display_name": bot_name, "always_online": False}},
        "oauth_config": {"scopes": {"bot": list(BOT_SCOPES)}},
        "settings": settings,
    }


def manifest_yaml(receiver_url: str, bot_name: str, app_name: str | None = None, *, events: bool = True) -> str:
    return yaml.safe_dump(render_manifest(receiver_url, bot_name, app_name, events=events),
                          sort_keys=False, allow_unicode=True)


def create_app_url(manifest: dict) -> str:
    """Slack's create-app link with the manifest prefilled."""
    return CREATE_URL + quote(json.dumps(manifest, separators=(",", ":")), safe="")


# ---------- `nua slack-manifest` ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--receiver-url", help="default: terraform output, else project_id and region in "
                                          "terraform.tfvars plus gcloud")
    p.add_argument("--app-name", help="the app's name in Slack (default: bot_name in config/domain.yml)")
    p.add_argument("--no-events", action="store_true",
                   help="leave out Event Subscriptions (add them after the receiver is deployed)")


def command(args: argparse.Namespace, root: Path) -> int:
    from nua import config as config_mod

    url = args.receiver_url or receiver.receiver_url(root)
    if not url:
        print(f"error: no receiver URL yet. {receiver.HOW_TO}", flush=True)
        return 1
    bot_name = config_mod.load(root).domain.bot_name
    manifest = render_manifest(url, bot_name, args.app_name, events=not args.no_events)
    print(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), end="")
    print()
    print("Create the app in one click (pick the workspace, then Create):")
    print(f"  {create_app_url(manifest)}")
    print()
    print("Then: Install to Workspace, and copy into .env (never into tfvars):")
    print("  SLACK_BOT_TOKEN       OAuth & Permissions > Bot User OAuth Token (xoxb-...)")
    print("  SLACK_SIGNING_SECRET  Basic Information > App Credentials > Signing Secret")
    print(f"Invite @{bot_name} to your briefs and ops channels (config/pipeline.yml `channels`).")
    if not args.no_events:
        print("If Slack refuses the events URL because the receiver is not deployed yet, re-run with "
              "--no-events and turn Event Subscriptions on after deploy (docs/self-hosting.md, step 9).")
    return 0

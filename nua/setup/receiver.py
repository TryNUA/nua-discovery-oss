"""The receiver's public URL, before or after it is deployed.

Cloud Run URLs are deterministic (`https://nua-receiver-<project number>.<region>.run.app`), so the
Slack app and the GitHub App can be set up before Terraform creates the receiver. `receiver_url`
tries, in order: `terraform output -raw receiver_url` (once infra/terraform is initialized), then
`project_id` and `region` from infra/terraform/terraform.tfvars plus the project number from gcloud.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

TF_DIR = Path("infra") / "terraform"
SERVICE = "nua-receiver"
DEFAULT_REGION = "us-west1"
# Values copied from terraform.tfvars.example that mean "not filled in yet".
EXAMPLE_PROJECT = "my-discovery-project"

HOW_TO = ("Pass --receiver-url https://nua-receiver-<project number>.<region>.run.app, or run "
          "`bash infra/bootstrap.sh --project <id>` first so terraform.tfvars has your project_id "
          "and region (the project number comes from `gcloud projects describe <id>`).")


def url_for(project_number: str | int, region: str) -> str:
    return f"https://{SERVICE}-{project_number}.{region}.run.app"


def normalize(url: str) -> str:
    """`host` or `https://host/` -> `https://host`. Refuses anything that is not https."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = f"https://{url}"
    if not url.startswith("https://") or not url[len("https://"):]:
        raise ValueError(f"receiver URL must be https: {url}")
    return url


def tfvars_value(text: str, key: str) -> str | None:
    """The string value of `key = "..."` in a tfvars file, ignoring commented lines."""
    m = re.search(rf'^[ \t]*{re.escape(key)}[ \t]*=[ \t]*"([^"]*)"', text, re.M)
    return m.group(1) if m else None


def _run(cmd: list[str], cwd: Path | None = None) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def from_terraform(root: Path) -> str | None:
    tf = Path(root) / TF_DIR
    if not (tf / ".terraform").is_dir():
        return None
    out = _run(["terraform", "output", "-raw", "receiver_url"], cwd=tf)
    return out if out and out.startswith("https://") and "\n" not in out else None


def from_tfvars(root: Path) -> str | None:
    path = Path(root) / TF_DIR / "terraform.tfvars"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    project = tfvars_value(text, "project_id")
    if not project or project == EXAMPLE_PROJECT:
        return None
    region = tfvars_value(text, "region") or DEFAULT_REGION
    number = _run(["gcloud", "projects", "describe", project, "--format=value(projectNumber)"])
    return url_for(number, region) if number and number.isdigit() else None


def receiver_url(root: Path) -> str | None:
    """The receiver URL from Terraform or tfvars plus gcloud, or None (then show HOW_TO)."""
    return from_terraform(root) or from_tfvars(root)

# Self-hosting

The full production setup: calls flow in from your recorder by webhook, get digested on Google Cloud, land as commits in your private GitHub repo, and show up as briefs in Slack with buttons and a bot you can ask. With the fast path below it takes about an hour, most of it waiting on builds and applies. Every secret ends up in one gitignored `.env` file and in Secret Manager, nowhere else.

Try the [local quickstart](local-quickstart.md) first if you have not. It uses the same repo and config.

Commands below assume you run them from the root of your repo clone unless a step says `cd`. Replace `my-discovery-project`, `us-west1` and `owner/my-discovery` with yours.

**Prefer a click-through setup?** `uv run python -m nua setup` opens a local setup page that walks the same steps in your browser.

## Fast path

Three commands do what used to be most of this guide: `nua github-app` registers the GitHub App through GitHub's manifest flow, `nua slack-manifest` gives you a one-click Slack create link, and `infra/bootstrap.sh` runs every GCP step. Each is safe to re-run: what is done is skipped. The [detailed steps](#accounts-and-keys-checklist) below are the manual reference and explain what each one does.

You need `git`, [`uv`](https://docs.astral.sh/uv/), [`gcloud`](https://cloud.google.com/sdk/docs/install), [Terraform](https://developer.hashicorp.com/terraform/install) 1.9+ and `curl`, a GCP billing account, a Slack workspace where you can install apps, a paid-tier [Gemini API key](https://aistudio.google.com/apikey) and a recorder (Granola Business or Circleback).

1. **Your private repo and config.** Make a private copy of this repo ([step 0](#step-0-make-your-own-private-repo)), then describe your discovery ([step 1](#step-1-describe-your-discovery); set `timezone` before your first call is processed):

   ```sh
   uv sync
   uv run python -m nua init
   git add config && git commit -m "Configure for our discovery" && git push
   ```

2. **Log in twice** (gcloud and Terraform use separate credentials, [step 2](#step-2-gcp-project-and-logins)):

   ```sh
   gcloud auth login
   gcloud auth application-default login
   ```

3. **First bootstrap run.** It checks your tools and logins, creates the project and links billing when you ask, enables the two bootstrap APIs, creates the state bucket, runs `terraform init`, writes `terraform.tfvars`, and prints your receiver URL. On a first run it then stops, listing what it still needs: the GitHub App and the keys in `.env`.

   ```sh
   bash infra/bootstrap.sh --project my-discovery-project --region us-west1 \
     --create-project --billing-account XXXXXX-XXXXXX-XXXXXX     # drop these two for an existing project
   ```

   `gcloud billing accounts list` shows your billing account id. `--dry-run` prints every command and runs nothing.

4. **GitHub App, one click.** Opens GitHub with the app filled in (webhook to your receiver, Contents read and write, Push events, private). Name it, click **Create**, then install it on your repo when the terminal shows the link. It writes the private key and webhook secret to `.env` and the app id, slug and installation id to `terraform.tfvars`, and never prints a secret.

   ```sh
   uv run python -m nua github-app --repo owner/my-discovery
   ```

   For a repo in an organization the app is created under that organization (`--owner` picks another one); you need to be one of its owners.

5. **Slack app, two clicks.** Prints the manifest for your receiver and `bot_name` and a link that opens Slack with it prefilled: pick the workspace, **Create**, then **Install to Workspace**.

   ```sh
   uv run python -m nua slack-manifest
   ```

   Copy **OAuth & Permissions > Bot User OAuth Token** to `SLACK_BOT_TOKEN` and **Basic Information > Signing Secret** to `SLACK_SIGNING_SECRET` in `.env`. Create your briefs and ops channels and invite the bot ([step 3](#step-3-slack-app)). If Slack refuses the events URL because the receiver is not up yet, run it with `--no-events` and turn Event Subscriptions on after the next step ([step 9](#step-9-slack-urls-and-the-github-webhook)).

6. **Keys in `.env`.** Add `GEMINI_API_KEY` and your recorder's API keys ([step 5](#step-5-fill-in-env)). Leave the recorder's signing secret empty for now. With Circleback, list the people in `circleback_people` in `terraform.tfvars`.

7. **Second bootstrap run.** Same command, no flags needed now (the project is in `terraform.tfvars`). It shows each `terraform plan` and asks before applying (`--yes` skips the question): stage 1, then the secrets from `.env`, the first image, stage 2, and a health check. At the end it prints the webhook URLs.

   ```sh
   bash infra/bootstrap.sh
   ```

8. **Recorder.** Create the Granola webhook ([step 8a](#8a-granola)) or each person's Circleback automation ([step 8b](#8b-circleback)), put its signing secret in `.env`, and run `bash infra/bootstrap.sh` again: it pushes the new secret and restarts both services so they read it.

9. **Check Slack and GitHub** ([step 9](#step-9-slack-urls-and-the-github-webhook)): the events URL shows **Verified** in Slack, and the GitHub App's failed first ping gets a 200 on **Redeliver**.

10. **Go live** ([step 10](#step-10-go-live)): set `ingest_cutoff` in `config/pipeline.yml` and push, then `schedules_on = true` in `terraform.tfvars` and run `bash infra/bootstrap.sh` once more.

Then [verify it works](#verifying-it-works). Shipping new code later is `infra/deploy.sh`, not the bootstrap ([Day 2](#day-2)).

## Overview

```
Recorder webhook ─────┐                    Cloud Run: nua-receiver (public, scales to zero)
GitHub App push ──────┤                    verifies every request, writes new transcripts,
Slack clicks/@mention ┼─────────────────>  answers Slack within 3 s, enqueues work
Cloud Scheduler ──────┘                                │
                                                       v
                                           Cloud Tasks queue `nua` (one task at a time)
                                                       │
                                                       v
                                           Cloud Run: nua-processor (private)
                                           clone, run the task, commit, push, post to Slack
                                                       │
                         ┌─────────────────────────────┼──────────────────────┐
                         v                             v                      v
                 your private GitHub repo      Slack #discovery-calls    Gemini API (and
                 (transcripts, digests,        #discovery-ops            Anthropic, OpenAI
                  beliefs, indexes)                                       if you add them)
```

| Piece | What it is | Where it is defined |
|---|---|---|
| `nua-receiver` | Public Cloud Run service. Routes: `/ingest/granola`, `/ingest/circleback/<person>`, `/github`, `/slack/interact`, `/slack/events`, `/sweep`, `/rollup`, `/healthz` | `infra/terraform/cloud_run.tf`, `service/main.py` |
| `nua-processor` | Private Cloud Run service. Runs one task at a time from the queue: process a call, backfill, apply a belief change, backcheck, reindex, roll-up, sweep, answer a question | same |
| Queue `nua` | Cloud Tasks, 1 concurrent dispatch, 5 attempts | `tasks.tf` |
| Scheduler jobs | `sweep` (2am daily), `sweep-granola` (hourly at :30), `rollup` (Mondays 8am), `rollup-bootstrap` (never scheduled; run by hand), in `time_zone` from `terraform.tfvars` (default `America/Los_Angeles`) | `scheduler.tf` |
| Secret Manager | 9 secrets by default; Terraform creates them empty, `infra/push-secrets.sh` fills them | `secrets.tf` |
| Service accounts | `nua-receiver`, `nua-processor`, `nua-tasks`, `nua-scheduler`, `nua-build` | `service_accounts.tf` |
| Artifact Registry repo `nua` | The image; keeps the 10 newest | `artifact_registry.tf` |
| Bucket `<project>-nua-build` | Source uploads for Cloud Build | same |

Config in `config/*.yml` is read from the repo on every task. Code (including prompts) ships in the image.

## Accounts and keys checklist

| What | Why | Cost | Where |
|---|---|---|---|
| GitHub account | Hosts your private instance repo and the GitHub App | free | [github.com](https://github.com) |
| GCP project with billing | Runs the receiver, processor, queue, scheduler and secrets | a few dollars a month | [console.cloud.google.com](https://console.cloud.google.com) |
| Gemini API key, paid tier | Every model role in the default config | $0.25 to $2 per call | [Google AI Studio](https://aistudio.google.com/apikey) |
| Anthropic and OpenAI keys (optional) | Cross-provider models, see [Choosing models](#choosing-models) | per use | [console.anthropic.com](https://console.anthropic.com), [platform.openai.com](https://platform.openai.com) |
| A recorder: **Granola** or **Circleback**, or both | Where calls come from. Pick one per person; a team can mix them | | |
| Granola: a Business workspace and its workspace API key | Granola's API and webhooks exist only on Business and up. One workspace key reads the whole Team space | per seat, see Granola's pricing | Granola workspace settings (admin) |
| Circleback: an account per person who records, with automations and an API key | Each person's webhook sends their tagged calls; their API key lets the sweep catch misses and find calendar start times | per person, see Circleback's pricing | [circleback.ai](https://circleback.ai), Settings > API keys |
| Slack workspace where you can install apps | Briefs, buttons, roll-ups, the `@Nua Discovery` bot | free | [api.slack.com/apps](https://api.slack.com/apps) |
| GitHub App on your repo | Lets the pipeline commit, and sends push webhooks for dropped transcripts and change requests | free | GitHub Settings > Developer settings > GitHub Apps |
| Tools on your machine | `git`, [`uv`](https://docs.astral.sh/uv/), [`gcloud`](https://cloud.google.com/sdk/docs/install), [Terraform](https://developer.hashicorp.com/terraform/install) 1.9+, `openssl`, `curl` | free | |

You end up with these values in `.env` (every name is in `.env.example`). `nua github-app` writes the two GitHub ones for you:

| Variable | From |
|---|---|
| `GEMINI_API_KEY` (and `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) | model providers |
| `GRANOLA_API_KEY`, `GRANOLA_WEBHOOK_SECRET` | Granola only: workspace key; the webhook's `whsec_...` secret (step 8a) |
| `CIRCLEBACK_API_KEY_<PERSON>`, `CIRCLEBACK_SIGNING_SECRET_<PERSON>` | Circleback only, per person (`ALEX` for the `alex` key in `config/people.yml`): their `cb_...` API key; their automation's signing secret (step 8b) |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | Slack app (step 3) |
| `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET` | GitHub App (step 4) |

The non-secret GitHub App values (`github_app_id`, `github_app_installation_id`, `github_app_slug`) and `repo_slug` go in `infra/terraform/terraform.tfvars`; `nua github-app` writes them too.

## Step 0. Make your own private repo

Your instance repo accumulates transcripts, digests and beliefs: real conversations with real people. It **must be private**.

Either click **Use this template** on GitHub and choose **Private**, or clone and push to a new private repo:

```sh
git clone https://github.com/<upstream-owner>/nua-discovery.git my-discovery
cd my-discovery
git remote rename origin upstream          # pull later updates with: git pull upstream main
gh repo create owner/my-discovery --private --source=. --push
```

The bot will commit to `main` all day, so set this once in every clone you work from:

```sh
git config pull.rebase true
```

and `git pull` before you push.

## Step 1. Describe your discovery

`uv run python -m nua init` asks for these and writes them, starting blank or from an example market (`nua init --example insurance` or `freight-brokerage`, see `examples/`). It keeps the files' comments, and writes `time_zone` into `infra/terraform/terraform.tfvars` when that file exists. To do it by hand, edit and commit these, as in the [local quickstart](local-quickstart.md#4-describe-your-discovery):

- `config/domain.yml`: `company`, `market` (who you sell to) and `exploring`. Optional: scope fields, roles, `bot_name`.
- `config/people.yml`: everyone who runs calls. `email` must be the address their calls are recorded under. `slack_id` (optional) shows their first name on belief changes made from Slack.
- `config/seats.yml`: optional seed seats; the list grows from the calls.
- `config/pipeline.yml`: `internal_email_domains` (your company domain), `channels` if you want other channel names. Leave `ingest_cutoff: null` for now.
- `timezone` in `config/pipeline.yml` (default `America/Los_Angeles`): the IANA time zone your team works in, such as `Europe/London` or `America/New_York`. Call-id dates, `ingested_at` stamps, a date-only `ingest_cutoff` and every 'today' are read in it. Set it before your first call is processed: changing it later does not rename existing call ids.
- `config/entities.yml`: starts empty. Add company and product names the transcripts garble as you find them.

```sh
git add config && git commit -m "Configure for our discovery" && git push
```

## Step 2. GCP project and logins

`infra/bootstrap.sh` does this step (`--create-project`, `--billing-account`) and prints the receiver URL; it checks both logins and tells you which one to renew. By hand:

Install `gcloud` and Terraform, then log in **twice**. They are separate credentials: `gcloud` commands use the first, Terraform uses the second (Application Default Credentials). Both expire; when a command suddenly fails with an auth error, run the matching one again.

```sh
gcloud auth login
gcloud auth application-default login
```

Create the project, link billing, and enable the two APIs Terraform needs before it can do anything else (it reads the project number through Cloud Resource Manager):

```sh
PROJECT_ID=my-discovery-project
REGION=us-west1
gcloud projects create ${PROJECT_ID}
gcloud billing accounts list
gcloud billing projects link ${PROJECT_ID} --billing-account=XXXXXX-XXXXXX-XXXXXX
gcloud config set project ${PROJECT_ID}
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com --project=${PROJECT_ID}
```

Work out your receiver URL now. Cloud Run URLs are deterministic, so you can configure Slack and GitHub before the receiver exists:

```sh
PROJECT_NUMBER=$(gcloud projects describe ${PROJECT_ID} --format='value(projectNumber)')
echo "https://nua-receiver-${PROJECT_NUMBER}.${REGION}.run.app"
```

Call that `RECEIVER_URL` below. `terraform output receiver_url` prints the same thing later, and `nua slack-manifest` and `nua github-app` work it out themselves from `project_id` and `region` in `terraform.tfvars` (or take `--receiver-url`).

## Step 3. Slack app

1. Create two channels: one for briefs, roll-ups and belief notices (default `#discovery-calls`) and one for errors, Re-run buttons and sweep catches (`#discovery-ops`). They may be private. Any names work: put yours under `channels` in `config/pipeline.yml`. A third, staging channel (`channels.test`, default `#discovery-test`) is optional and only used with `slack_test_mode`.
2. Run `uv run python -m nua slack-manifest` and open the link it prints: Slack's create page opens with the manifest filled in for your receiver and `bot_name`. Pick the workspace and **Create**. By hand instead: open [docs/slack-app-manifest.yml](slack-app-manifest.yml), replace both `RECEIVER_URL` placeholders with your receiver host (no `https://` twice), change the names if you changed `bot_name`, and paste it at [api.slack.com/apps](https://api.slack.com/apps) > **Create New App** > **From a manifest**.
3. Slack cannot verify the events URL until the receiver is deployed. If it refuses to create the app because of that, run `nua slack-manifest --no-events` (or delete the `event_subscriptions` block), create the app, and add it in step 9.
4. **Install to Workspace**. Copy **OAuth & Permissions > Bot User OAuth Token** (`xoxb-...`) to `SLACK_BOT_TOKEN`.
5. Copy **Basic Information > App Credentials > Signing Secret** to `SLACK_SIGNING_SECRET`.
6. Invite the bot to each channel: `/invite @Nua Discovery` in both (and the staging channel if you use one), and any channel where people should be able to ask it things.

Bot scopes the code needs (already in the manifest): `chat:write`, `channels:read`, `groups:read`, `channels:history`, `groups:history`, `app_mentions:read`. If you ever change scopes, Slack asks you to **reinstall** the app; until you do, the new scopes do not apply.

## Step 4. GitHub App

A GitHub App, not a personal token: installation tokens are short-lived and belong to no one person.

**One click:** `uv run python -m nua github-app --repo owner/my-discovery`. It runs GitHub's [manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest): a local page posts the settings below to GitHub, you name the app and click **Create**, GitHub hands back the app id, private key and webhook secret, and the command writes them to `.env` (key on one line, as point 5 below needs) and `terraform.tfvars`. It then prints the install link, waits while you install the app on the repo, and writes `github_app_installation_id`. No secret is printed. For an organization's repo it creates the app under the organization; `--owner` picks a different account, `--name` the app name, `--no-browser` prints the local URL instead of opening it.

**By hand:** go to **Settings > Developer settings > GitHub Apps > New GitHub App** (under your organization's settings if the repo is in an org).

| Setting | Value |
|---|---|
| GitHub App name | anything unique, e.g. `acme-discovery-bot`. Its URL name is the **slug** |
| Homepage URL | your repo's URL |
| Webhook: Active | **checked** |
| Webhook URL | `https://RECEIVER_URL/github` |
| Webhook secret | `openssl rand -hex 32`; put the same value in `GITHUB_WEBHOOK_SECRET` |
| Repository permissions: Contents | **Read and write** |
| Repository permissions: Metadata | Read-only (always on) |
| Subscribe to events | **Push** (appears once Contents is set) |
| Where can this GitHub App be installed? | Only on this account |

Then:

1. **Create GitHub App**. Note the **App ID** on its General page.
2. Note the **slug**: the last part of its public URL, `https://github.com/apps/<slug>`. Its commits show as `<slug>[bot]`, and the receiver ignores pushes from that login so it never reacts to its own commits.
3. **Generate a private key**. A `.pem` file downloads.
4. **Install App** > your account > **Only select repositories** > your instance repo. The installation id is the number at the end of the page URL (`.../settings/installations/<id>`).
5. Put the private key in `.env` on **one line**, with each line break written as a literal `\n`, and no quotes:

   ```sh
   awk 'NF {sub(/\r/, ""); printf "%s\\n", $0}' ~/Downloads/<app>.private-key.pem
   ```

   Paste the output after `GITHUB_APP_PRIVATE_KEY=`. The code turns `\n` back into line breaks. `push-secrets.sh` reads only the first line of a value and sends it exactly as written, so a multi-line value or surrounding quotes would break the key.

GitHub pings the webhook when you create the app. That first delivery fails because the receiver does not exist yet; that is fine.

## Step 5. Fill in `.env`

```sh
cp .env.example .env      # nua github-app already made it if you ran that
```

Fill in `GEMINI_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, and your recorder's API keys:

- Granola: `GRANOLA_API_KEY` (the workspace key). Leave `GRANOLA_WEBHOOK_SECRET` empty until step 8a.
- Circleback: for each person who records, `CIRCLEBACK_API_KEY_<PERSON>` from their Circleback **Settings > API keys** (`cb_...`, shown once). Leave `CIRCLEBACK_SIGNING_SECRET_<PERSON>` empty until step 8b.

Leave the recorder you do not use empty, and the Anthropic and OpenAI keys empty unless you use them. An empty key is never called: the hourly Granola sweep simply does nothing on a Circleback-only team. Write every value bare: no quotes, no trailing comments.

`.env` is gitignored. On Cloud Run the services never read it: their settings come from Terraform and Secret Manager.

## Step 6. Terraform, stage 1

`bash infra/bootstrap.sh` does steps 6 and 7 in order, shows each plan and asks before applying, and skips whatever is already done. It stops with a list when `terraform.tfvars` or `.env` is missing something. The manual commands:

Create the state bucket once, by hand (the state cannot live in a bucket Terraform manages):

```sh
gcloud storage buckets create gs://${PROJECT_ID}-tfstate --project=${PROJECT_ID} --location=${REGION} \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://${PROJECT_ID}-tfstate --versioning
```

Init and fill in the inputs:

```sh
cd infra/terraform
terraform init -backend-config="bucket=${PROJECT_ID}-tfstate" -backend-config="prefix=nua"
cp terraform.tfvars.example terraform.tfvars
```

In `terraform.tfvars` set `project_id`, `region`, `repo_slug` (`owner/my-discovery`), `github_app_id`, `github_app_installation_id`, `github_app_slug`, and optionally `billing_account` for a $25 a month budget alert (`monthly_budget_usd`). If anyone records on Circleback, list their person keys: `circleback_people = ["alex", "sam"]`. Leave `image = ""` and `schedules_on = false`. Set `time_zone` in `terraform.tfvars` to the same value as `timezone` in `config/pipeline.yml`. The 2am sweep and the Monday 8am roll-up run in this zone. No secret ever goes in this file. (`bootstrap.sh` copies `timezone` into `time_zone` for you and warns when they differ.)

```sh
terraform plan
terraform apply
```

Stage 1 creates the APIs, service accounts and IAM, empty secrets, the Artifact Registry repo, the Cloud Build account and source bucket, and the queue. If it fails saying the Cloud Tasks or Cloud Scheduler service agent does not exist yet, wait a minute and apply again.

Stage 1 must exist before the first build: `deploy.sh` builds as `nua-build` and uploads to `<project>-nua-build`, both created here. The bootstrap keeps this order.

## Step 7. Secrets, image, stage 2

Back at the repo root, push the secret values. Dry run first; it prints one word per secret and never a value:

```sh
cd ../..
bash infra/push-secrets.sh --dry-run
bash infra/push-secrets.sh
```

With Circleback, pass the same people as `circleback_people`, every time you run it: `CIRCLEBACK_PEOPLE="alex sam" bash infra/push-secrets.sh`.

`set` means it added your value, `placeholder` means `.env` had none so it stored the word "placeholder" (so Cloud Run can start), `kept` means the secret already had a value, `missing` means stage 1 is not applied. `granola-webhook-secret`, `circleback-signing-<person>`, `anthropic-api-key` and `openai-api-key` usually show `placeholder` at this point. The code treats a `placeholder` value as unset.

Build the first image:

```sh
PROJECT_ID=${PROJECT_ID} bash infra/deploy.sh --build-only
```

It prints `image = "us-west1-docker.pkg.dev/<project>/nua/nua:<tag>"`. Put that line in `terraform.tfvars` (the bootstrap writes it for you), then:

```sh
cd infra/terraform
terraform plan
terraform apply
terraform output
```

Stage 2 creates the two Cloud Run services, the processor's invoker grant and the four Scheduler jobs (all paused). `terraform output` lists every URL you need next: `receiver_url`, `granola_webhook_url`, `github_webhook_url`, `slack_interactivity_url`, `slack_events_url`, and `circleback_webhook_urls` (one per person) with Circleback.

Check the receiver is up:

```sh
curl https://RECEIVER_URL/healthz
```

## Step 8. Connect your recorder

Do 8a, 8b or both, depending on who records on what.

### 8a. Granola

In Granola (workspace admin):

- Everyone who runs discovery calls is in the one Business workspace.
- A discovery call is shared to the **Team** space; everything else stays in My notes. The workspace key reads the Team space plus any space with "Allow Granola API access" on, so turn that off on every other space (it defaults to on for new ones).
- Leave transcript auto-deletion off. Granola keeps no audio.
- The Granola iOS app saves notes to your oldest workspace; leave older workspaces or move those notes.

Create the webhook in the Granola desktop app (Business or Enterprise plan):

1. **Settings > Connectors > Webhooks > Set up a webhook.**
2. Choose the Team space notes and the events `note.generated` and `note.access_granted` (`note.edited` is optional: a resumed note that got longer becomes a new version).
3. Endpoint URL: `https://RECEIVER_URL/ingest/granola`.
4. Create the webhook and copy its signing secret (starts with `whsec_`). It is shown only once.

Or create it with the API and the workspace key; the signing secret is in the response, shown once:

```sh
curl -X POST https://public-api.granola.ai/v1/webhook-endpoints \
  -H "Authorization: Bearer <workspace key>" -H "Content-Type: application/json" \
  -d '{"url": "https://RECEIVER_URL/ingest/granola", "scopes": ["workspace"], "events": ["note.generated", "note.access_granted"]}'
```

Put the `whsec_...` secret in `.env` as `GRANOLA_WEBHOOK_SECRET` and run `bash infra/bootstrap.sh`: it pushes the secret and restarts the services. By hand, push it and start a new receiver revision so the running service reads it (a running instance keeps the secret value it started with):

```sh
bash infra/push-secrets.sh
gcloud run services update nua-receiver --region=${REGION} --update-labels=secrets-refreshed=$(date +%s)
```

Terraform ignores template labels, so this causes no drift.

### 8b. Circleback

Do this once per person in `circleback_people`, signed in to their own Circleback account. Circleback has no owner field in its webhook, so each person gets their own URL (`/ingest/circleback/<person>`) and their own signing secret.

1. **Create the tag.** Make a tag called exactly `discovery`. Only meetings with it are ingested; everything else (internal, investor, personal calls) is ignored by the receiver and the sweep.
2. **Create the automation.** **Automations > New automation**:
   - Condition: the meeting has the tag `discovery`.
   - Action: **Send webhook request**.
   - Endpoint URL: that person's URL from `terraform output circleback_webhook_urls`, e.g. `https://RECEIVER_URL/ingest/circleback/alex`.
   - Meeting outcomes: include the **transcript**, notes and attendees. Without the transcript the receiver has nothing to digest and leaves the call for the sweep.
   - Copy the **signing secret** it shows into `.env` as `CIRCLEBACK_SIGNING_SECRET_ALEX` (the person key in upper case).
   - **Done**, name it, **Save**.
3. **Push the secret and restart both services** so they read the new values. `bash infra/bootstrap.sh` does both (it reads `circleback_people` from `terraform.tfvars`); by hand:

   ```sh
   CIRCLEBACK_PEOPLE="alex sam" bash infra/push-secrets.sh
   gcloud run services update nua-receiver --region=${REGION} --update-labels=secrets-refreshed=$(date +%s)
   gcloud run services update nua-processor --region=${REGION} --update-labels=secrets-refreshed=$(date +%s)
   ```

4. **Check it.** Circleback's **Send test request** on the automation step should get a 200: the receiver acknowledges it and, since a test meeting has no `discovery` tag (and before step 10 there is no `ingest_cutoff`), ignores it. A 503 means the signing secret in Secret Manager does not match the automation's: push it again and restart the receiver.

Day to day, tag each discovery call `discovery` in Circleback, before or after it ends. The receiver gets it by webhook, matches the calendar event for the real start time (Circleback's `createdAt` is not the start), and writes `transcripts/circleback_<id>.md`. If a tag is added after the automation already ran, or a webhook is lost, the nightly sweep finds the tagged meeting with that person's API key. Calls with someone outside your `internal_email_domains` that nobody tagged are posted once to `#discovery-ops` as a reminder.

## Step 9. Slack URLs and the GitHub webhook

In your Slack app at [api.slack.com/apps](https://api.slack.com/apps), check all three. Each one fails silently when missed:

1. **Interactivity & Shortcuts**: on, Request URL `https://RECEIVER_URL/slack/interact`, then **Save Changes**. Without it every button click and form is dropped.
2. **Event Subscriptions**: on, Request URL `https://RECEIVER_URL/slack/events` (Slack should show **Verified**; click Retry if it was created before the receiver existed), **Subscribe to bot events**: `app_mention`, then **Save Changes**.
3. **Reinstall** the app if Slack shows a banner asking you to (it does after scope or event changes).

In the GitHub App settings, check the webhook is **Active**, the URL is `https://RECEIVER_URL/github`, **Push** is subscribed, and the secret equals `GITHUB_WEBHOOK_SECRET`. **Advanced > Recent deliveries** shows each delivery and the receiver's answer; you can **Redeliver** the failed ping to check it now gets a 200.

## Step 10. Go live

1. Set the go-live time in `config/pipeline.yml`. Recorder items from before it are ignored, and while it is `null` every recorder webhook is acknowledged and ignored:

   ```yaml
   ingest_cutoff: 2026-11-02T09:00:00-08:00
   ```

   Also confirm `internal_email_domains` and the `email` of everyone in `config/people.yml`. Commit and push. Config changes need no deploy.
2. Switch the schedules on. In `terraform.tfvars` set `schedules_on = true`, then `terraform plan` (it should show only `sweep`, `sweep-granola` and `rollup` going from paused to running) and `terraform apply`, or run `bash infra/bootstrap.sh`, which shows the same plan and asks. Never pause or resume jobs with gcloud: Terraform owns that, and the next apply puts it back.
3. Backfill your past calls: add their exports to `transcripts/` (flat, not in a subfolder) with a git push, or the GitHub web upload (up to 100 files at a time). A push that adds more than 5 transcripts becomes one backfill batch with a summary and fix-up list in `#discovery-calls`; dropped files never get a per-call brief. Correct unknown dates and firms in `config/overrides.yml` and push; that triggers a reindex.
4. Once the backfill is digested, run the bootstrap roll-up. It posts up to 40 proposed Beliefs and Watching items to `#discovery-calls`, each with Approve / Edit / Reject:

   ```sh
   gcloud scheduler jobs run rollup-bootstrap --location=${REGION}
   ```

   If gcloud refuses because the job is paused, resume it, run it and pause it again (`gcloud scheduler jobs resume|run|pause rollup-bootstrap --location=${REGION}`). `uv run python -m nua rollup --bootstrap` on your laptop only previews: it posts nothing, so it has no buttons.
5. Approve the proposals you agree with. Each approval writes `beliefs.md` and schedules one batched backcheck against every past call.

## Verifying it works

| Where | What you should see |
|---|---|
| A test call | Record a short Granola note after the cutoff and share it to the Team space, or a Circleback meeting tagged `discovery`. A call where every listed attendee is internal gets a stub and no brief, so use a solo note (only you listed) or a call with someone outside your domain. A brief should appear in `#discovery-calls` within a few minutes |
| `#discovery-ops` | Errors with **Re-run** buttons, sweep catches, notices about transcripts in subfolders. Quiet is good |
| GitHub | Commits by `<slug>[bot]`: `Add transcripts/granola_...md` (or `circleback_...md`), then `process_call: digest ...` |
| Cloud Run logs | Logs Explorer with `resource.type="cloud_run_revision" resource.labels.service_name="nua-processor"` (or `nua-receiver`), or `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="nua-processor"' --limit=50 --project=${PROJECT_ID}` |
| Cloud Tasks | `gcloud tasks list --queue=nua --location=${REGION}`: empty once work is done. A task stuck retrying shows up here |
| Scheduler | `gcloud scheduler jobs list --location=${REGION}`: three jobs `ENABLED`, `rollup-bootstrap` `PAUSED` |
| Slack bot | `@Nua Discovery list beliefs`, then a real question. The answer ends with `read N/N digests` |
| Terraform | `terraform plan` shows no changes, even after code deploys |

Optional, for staging: set `slack_test_mode = true` in `terraform.tfvars` and apply, and every Slack post goes to `channels.test` (default `#discovery-test`; create it and invite the bot). With `channels.test` empty, test mode refuses to run rather than post to a real channel. Set it back to `false` and apply to go live.

## Day 2

| Task | How |
|---|---|
| Deploy code (including prompt changes) | From a clean, up-to-date checkout: `PROJECT_ID=${PROJECT_ID} bash infra/deploy.sh` (`--dry-run` prints the commands first). It builds, pushes and points both services at the new image. No Terraform run, no tfvars change |
| Change config (`config/*.yml`) | Commit and push. Every task reads config from the repo |
| Change infrastructure | Edit the `.tf` files or `terraform.tfvars`, `terraform plan`, read it, `terraform apply`. Never change Terraform-owned settings with gcloud: the next apply reverts them |
| Rotate a secret | Update `.env` and run `bash infra/bootstrap.sh` (pushes it and restarts the services when anything changed). By hand: `bash infra/push-secrets.sh`, then start a new revision of each service that reads it: `gcloud run services update nua-receiver --region=${REGION} --update-labels=secrets-refreshed=$(date +%s)` (and `nua-processor` for model keys, `GRANOLA_API_KEY`, Circleback API keys, the GitHub key, the Slack token). Disable the old version in Secret Manager once the new one works |
| Change models | Edit the model roles in `config/pipeline.yml`, bump `prompt_version`, commit and push. Add any new provider's key to `.env`, push it and start a new processor revision. Set `reprocess_stale: true` to re-digest old calls, up to 40 a night |
| Add a teammate | Add them to `config/people.yml` (and their domain to `internal_email_domains` if new), add them to the Granola workspace (or add their key to `circleback_people`, apply, and do step 8b for them), invite them to the Slack channels, give them read access to the repo |
| Backfill more calls | Push the exports into `transcripts/`. After a large one, run `rollup-bootstrap` again if you want fresh proposals |
| Pause everything scheduled | `schedules_on = false`, apply. Webhooks keep working |
| Stop ingesting | Set `ingest_cutoff: null` and push: recorder webhooks are acknowledged and ignored |
| Costs | Infra is a few dollars a month: Cloud Run scales to zero, Tasks and Scheduler sit in free tiers, Secret Manager is about $0.54 a month for the 9 secrets, Artifact Registry costs cents with the keep-10 cleanup. Models are the real cost: $0.25 to $2 per call and per Slack question. `cost_cap_usd_per_task` in `config/pipeline.yml` caps one task. Set `billing_account` in tfvars for an emailed budget alert (default $25 a month; it never stops anything) |
| Slack clicks fail after quiet spells | Keep one receiver instance warm: `min_instance_count = 1` for the receiver in `cloud_run.tf`, apply. Roughly $10 to $40 a month |

More detail on each Terraform file, stages and day 2 operations: [infra/README.md](../infra/README.md).

## Choosing models

Model roles are in `config/pipeline.yml`. Specs are `<provider>:<model id>`; providers are `anthropic`, `openai` and `gemini`.

| Role | Does | Wants |
|---|---|---|
| `extractors` (two) | Read each call and pull findings with quotes. The first is primary: its who-fields win and it is the fallback when the other is down | Strong, careful readers. Also run the belief verdicts |
| `arbiter` | Consolidates the two extractors' findings and settles verdict disagreements | A third, independent judge |
| `ask_map` | Reads digests in batches for a question, and routes Slack mentions | Cheap and fast |
| `ask_reduce` | Writes the answer | Strong writer |
| `rollup` | Weekly themes and proposals | Strong, long context |

**Default: Gemini only.** One key runs everything. Two different Gemini models extract and a third arbitrates, so the cross-check still compares independent readers:

```yaml
extractors:
  - gemini:gemini-3.1-pro-preview
  - gemini:gemini-3.8-flash
arbiter: gemini:gemini-3.7-flash
ask_map: gemini:gemini-3.1-flash-lite
ask_reduce: gemini:gemini-3.1-pro-preview
rollup: gemini:gemini-3.1-pro-preview
```

**Recommended upgrade: cross-provider.** Models from different providers miss different things, so two providers extracting and a third arbitrating catch more:

```yaml
extractors:
  - anthropic:claude-sonnet-5
  - openai:gpt-6-sol
arbiter: gemini:gemini-3.1-pro-preview
ask_map: openai:gpt-6-luna
ask_reduce: anthropic:claude-sonnet-5
rollup: gemini:gemini-3.1-pro-preview
```

Validate a choice with the bake-off before you switch. It scores each model as extractor (finding recall against the hand labels, first-pass quote match rate) and as judge (verdict accuracy), and writes a report to `docs/research/bakeoff-<date>.md`:

```sh
uv run python -m nua bakeoff --models anthropic:claude-sonnet-5,openai:gpt-6-sol,gemini:gemini-3.1-pro-preview
```

It runs on the labeled fixtures in `tests/labels/` (P&C insurance calls) and stops at `--cap-usd` (default $25). For a fairer test in your market, add a few of your own calls with labels following `tests/labels/README.md`.

Whenever model ids change, bump `prompt_version`. Model ids move fast; check them against each provider's model list.

## Optional: MCP server (experimental)

Built and tested but never run in production.

- **MCP server** (a claude.ai, Claude Code or Codex connector): create a GitHub or Google OAuth app, set `deploy_mcp = true`, `mcp_auth` and `mcp_oauth_client_id`, apply, add `MCP_OAUTH_CLIENT_SECRET` and `MCP_JWT_SIGNING_KEY` and run `DEPLOY_MCP=1 bash infra/push-secrets.sh`, apply again if the service did not start, and deploy with `SERVICES="nua-processor nua-receiver nua-mcp"`. Connector URL: `terraform output mcp_connector_url`.

Details: [infra/README.md](../infra/README.md), "Day 2".

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `terraform apply` fails with an IAM policy error about `allUsers`, or "domain restricted sharing" | Google Workspace organizations created since 2024 enforce Domain restricted sharing, which refuses `allUsers` grants. The receiver is already made public with Cloud Run's `invoker_iam_disabled = true` instead of an `allUsers` grant, so no action is needed. If you see the error, something added an `allUsers` member: remove it |
| `deploy.sh` fails: Cloud Build permission denied, or the Compute Engine default service account is missing roles | New projects run Cloud Build as the Compute Engine default account with no permissions. Terraform creates `nua-build` and the bucket `<project>-nua-build`, and `deploy.sh` uses them. Apply stage 1 before the first build |
| Terraform cannot read the project, or fails on the project number | Enable the Cloud Resource Manager API first: `gcloud services enable cloudresourcemanager.googleapis.com` |
| Terraform: "could not find default credentials", or token expired | Terraform uses Application Default Credentials, separate from `gcloud auth login`. Run `gcloud auth application-default login`. Both logins expire; renew whichever one fails |
| Granola sweep returns 400 on timestamps | Granola's API accepts only UTC timestamps ending in `Z`. The client already formats them that way; if you change `nua/ingest/granola.py`, keep it |
| A call after go-live was ignored as "before the cutoff" | Granola creates a note when a meeting is opened ahead of time. The cutoff already compares the later of the note's creation and its calendar start. Check `ingest_cutoff` has an offset and is the time you meant |
| Circleback calls never arrive | Search the receiver logs for `circleback`. `ignored: not tagged discovery`: add the exact tag. `ignored: no transcript in payload`: include the transcript in the automation's outcomes. `ignored: created before ingest_cutoff` or `ingest_cutoff not set`: see step 10. `circleback ingest failed for <person>` with `did not verify` (a 503): the person is missing from `circleback_people`, or their signing secret was not pushed or the receiver not restarted (step 8b) |
| Every recorder webhook is ignored | `ingest_cutoff` is `null` or unreadable. Set it and push |
| Container fails to start with a permission error on a Python file | A file saved with owner-only permissions on your machine. The Dockerfile already runs `chmod -R a+rX` on the code; if you edit the Dockerfile, keep that line |
| Buttons do nothing | Interactivity is off or its Request URL is missing: Slack drops every click without an error. Set `.../slack/interact` and click **Save Changes** |
| `@Nua Discovery` gets no reply | Event Subscriptions off, URL not verified, `app_mention` not subscribed, the app not reinstalled after the change, or the bot not invited to that channel. All four must be right |
| Bot cannot post to a channel (`not_in_channel`, `channel_not_found`) | Invite the bot. Check the names in `channels` in `config/pipeline.yml` match yours |
| Dropped transcripts or change requests never get processed | GitHub App webhook inactive, Push not subscribed, wrong URL, or a secret mismatch (the receiver answers 401). Check **Recent deliveries**. GitHub never redelivers a failed webhook on its own; the nightly sweep picks up transcripts with no digest and leftover requests |
| Transcripts in a subfolder are skipped | The pipeline reads `transcripts/` flat. Move them up; `#discovery-ops` names them |
| `git push` rejected again and again | The bot commits to `main` constantly. `git config pull.rebase true`, then `git pull` before every push |
| A code change has no effect | Code ships only with `infra/deploy.sh`. Config changes in `config/*.yml` need nothing; Terraform changes need plan and apply |
| A Scheduler run logged a 429 | The receiver runs at most 1 instance, and a cold-start collision can refuse one Scheduler call. Harmless: the next sweep covers it. Raise the receiver's `max_instance_count` to 2 in `cloud_run.tf` if it happens often |
| A new secret value is not picked up | Running instances keep the value they started with. Start a new revision (Day 2, Rotate a secret) |
| `push-secrets.sh` says `missing` | The secret does not exist yet. Apply stage 1 (and match `CIRCLEBACK_PEOPLE` and `DEPLOY_MCP` to your tfvars) |
| The GitHub key fails to load | The value in `.env` must be one line with literal `\n`, no quotes. `nua github-app` writes it that way; see step 4 to do it by hand |
| `bootstrap.sh` stops at "Inputs" | It lists what is missing. GitHub App ids: `uv run python -m nua github-app --repo owner/my-discovery`. Slack keys: `uv run python -m nua slack-manifest`, create and install the app, copy the two values. Then run it again |
| `bootstrap.sh` says a plan "needs a yes" | It runs without a terminal (CI, a pipe). Run it in a terminal, or pass `--yes` once you have read the plan |
| `bootstrap.sh` says tfvars and `--project` disagree | `terraform.tfvars` already names another project. Fix whichever is wrong; it never switches projects on its own |
| `nua github-app` waits forever for the install | Install the app on the repo's owner (the link it prints), with the repo selected. Or stop it, and set `github_app_installation_id` to the number at the end of the installation page URL |
| `nua github-app` or `nua slack-manifest` has no receiver URL | Run `bash infra/bootstrap.sh --project <id>` once so `terraform.tfvars` has `project_id` and `region`, or pass `--receiver-url` |
| A task failed | `#discovery-ops` has the error and a **Re-run** button. After the last of 5 attempts a task stops retrying; a call gets a `failed` stub until re-run |

## Security notes

- **The repo must be private.** It holds transcripts of real conversations. Anyone with read access sees all of them; anyone with write access can drop transcripts and file belief changes.
- **Every public route checks a signature or token.** Granola: Standard Webhooks signature with the `whsec_` secret. Circleback: HMAC per person. GitHub: `X-Hub-Signature-256`. Slack: the signing secret (requests older than 5 minutes are refused). Scheduler: a Google OIDC token from `nua-scheduler` addressed to the receiver URL. A bad signature gets a 401 (recorder routes answer 503 so the sender retries). The processor is private: only `nua-tasks` may call it.
- **Who can trigger what.** Anyone in a channel the bot is in can ask it questions (each costs model calls) and press any button, including Approve and Retire; the change is recorded under their Slack handle. There is no allowlist, so keep the bot in channels limited to your team. Change requests pushed to `requests/` must name a person in `config/people.yml`.
- **Slack Connect.** Do not add the bot to a shared channel with another company: their members could ask it about your calls and see verbatim quotes.
- **Secrets never touch Terraform.** No secret is in a `.tf` file, `terraform.tfvars` or Terraform state. Terraform creates empty secrets and grants each service read access to only the secrets it mounts; `push-secrets.sh` pipes values from `.env` to Secret Manager without printing them.
- **Model providers see your transcripts.** Use paid API tiers whose terms exclude training on your data (the Gemini key should be on a billed project for this reason).
- **Recording consent.** Many places require every party's consent to record a call. A one-line disclosure at the start of each call is cheap. This is not legal advice.

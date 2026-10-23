# Infrastructure

The GCP side of Nua Discovery (for AWS, see [docs/self-hosting-aws.md](../docs/self-hosting-aws.md) and `infra/aws/`). For the whole production setup in order (private repo, Slack app, GitHub App, Granola webhook, go-live), follow [docs/self-hosting.md](../docs/self-hosting.md). This file is the reference for what Terraform creates and how to operate it.

**Quickest route:** `bash infra/bootstrap.sh --project <id>` runs the whole first-time setup below and can be re-run at any time (see "First-time setup").

Examples below use `PROJECT_ID=my-discovery-project`, region `us-west1` (the default) and repo `owner/my-discovery`; use yours.

Three scripts and the Terraform config, kept apart on purpose:

- **`terraform/`** creates and changes the GCP infrastructure: APIs, service accounts and IAM, Secret Manager secret containers, the Artifact Registry repo, the Cloud Tasks queue, the Cloud Run services and the Cloud Scheduler jobs. Read the `.tf` files, run `terraform plan`, and you see exactly what exists.
- **`deploy.sh`** ships code: it builds `service/Dockerfile` with Cloud Build, pushes the image to Artifact Registry and points the existing Cloud Run services at it. It never runs Terraform.
- **`push-secrets.sh`** copies secret values from your `.env` to Secret Manager, printing one word per secret and never a value.
- **`bootstrap.sh`** is the first-time setup in one re-runnable command: it checks tools and logins, prepares the project and the state bucket, runs `terraform init`, plans and (after a yes) applies both stages, and calls the other two scripts in between. Each step is skipped when already done.

Terraform ignores the services' image (and the client, revision name, template labels and annotations that a gcloud deploy sets), so `terraform plan` after a deploy shows no drift, and an apply never rolls a service back to an older image. Everything else on the services is Terraform's: change it in the `.tf` files, not with gcloud, or the next apply reverts it.

No secret value is ever in a `.tf` file, in `terraform.tfvars` or in Terraform state. Terraform creates empty secrets and grants who may read them; `push-secrets.sh` adds the values from your local `.env`.

## What each file creates

| File | Creates | Why |
|---|---|---|
| `versions.tf` | Terraform and `hashicorp/google` pins, GCS backend (bucket and prefix given at `init`) | Reproducible runs; shared state |
| `variables.tf` | Inputs, with checks (MCP needs an OAuth client id unless `mcp_auth = "none"`) | One place for every setting |
| `locals.tf` | Names, the deterministic service URLs, env vars and secret mounts per service | Shared by the other files; one map drives both secret mounts and secret grants |
| `apis.tf` | 11 `google_project_service` | Run, Tasks, Scheduler, Secret Manager, Artifact Registry, Cloud Build, IAM, IAM Credentials, Storage, Logging, Billing Budgets. Left enabled on destroy |
| `service_accounts.tf` | `nua-receiver`, `nua-processor`, `nua-tasks`, `nua-scheduler`, `nua-build` (and `nua-mcp` with `deploy_mcp`); `roles/cloudtasks.enqueuer` for each enqueuer; actAs on `nua-tasks` for each enqueuer; token creator for the Tasks and Scheduler service agents | Least privilege: one identity per service, Tasks calls the processor as `nua-tasks`, Scheduler signs as `nua-scheduler`, Cloud Build runs as `nua-build` |
| `secrets.tf` | One secret container per secret, and `roles/secretmanager.secretAccessor` per secret per reading service | Each service reads only the secrets it mounts |
| `artifact_registry.tf` | Docker repo `nua` (keeps the 10 newest images, deletes the rest after 7 days); the build source bucket `<project>-nua-build` (private, tarballs deleted after 7 days); `nua-build` may push to `nua`, read that bucket and write logs | Where `deploy.sh` builds and pushes, without the Compute Engine default account (newer projects give it no permissions) |
| `tasks.tf` | Queue `nua`: 1 concurrent dispatch, 5 attempts, backoff 60s to 1800s | One task at a time, retries spaced out |
| `cloud_run.tf` | `nua-processor` (private, 2 CPU, 2Gi, 1800s, concurrency 1, 0 to 1 instances, invokable only by `nua-tasks`) and `nua-receiver` (public, 1 CPU, 1Gi, 30s, concurrency 40, 0 to 1 instances, CPU only during requests, startup CPU boost) | The pipeline |
| `mcp.tf` | Only with `deploy_mcp`: the sign-in bucket (`<project>-nua-mcp-oauth`, private, nua-mcp is object admin) and `nua-mcp` (gen2, bucket mounted at `/oauth` as uid 10001, 300s, 0 to 1 instances; public only while sign-in is on, via `invoker_iam_disabled`) | The MCP server; experimental, off by default |
| `scheduler.tf` | `sweep` (2am daily), `sweep-granola` (hourly at :30), `rollup` (Mondays 8am), `rollup-bootstrap` (never scheduled), all in `time_zone` (default `America/Los_Angeles`, must match `timezone` in config/pipeline.yml), POST with an OIDC token for the receiver | Paused until `schedules_on = true` (go-live) |
| `budget.tf` | With `billing_account` set: a monthly budget (`monthly_budget_usd`, default $25) on this project, emailing billing admins at 50%, 90%, 100% and a forecast over 100% | Catches anything running far more often than it should; it never stops anything |
| `outputs.tf` | The receiver URL and the URLs to paste into Granola, the GitHub App and Slack (Circleback and MCP when on) | `nua slack-manifest` and `nua github-app` read `receiver_url` from here once Terraform is initialized |
| `terraform.tfvars.example` | Example inputs | Copy to `terraform.tfvars` (gitignored) |

Circleback secrets (`circleback-key-<person>`, `circleback-signing-<person>`) and env vars exist only for the people in `circleback_people`. MCP's account, grants, two secrets, bucket and service exist only with `deploy_mcp = true`.

### Stages

`image` gates the Cloud Run services and the Scheduler jobs. A service cannot start without an image and without values in the secrets it mounts, so a first setup runs in two applies:

- **Stage 1**, `image = ""`: APIs, service accounts and IAM, secret containers and grants, the registry and the queue. With the defaults (`deploy_mcp = false`, `circleback_people = []`) that is 11 APIs, 5 service accounts, 2 enqueuer grants, 2 actAs grants, 2 service agent grants, 9 secrets, 12 secret grants (6 for the receiver, 6 for the processor), 1 registry, 1 build source bucket, 3 build grants and 1 queue.
- **Stage 2**, `image` set: adds 2 Cloud Run services, 1 invoker grant (Tasks on the processor) and 4 Scheduler jobs. The receiver is public through `invoker_iam_disabled`, not an `allUsers` grant. Google Workspace organizations created since 2024 enforce domain restricted sharing, which refuses `allUsers` grants, so this works in those projects too.

### Service URLs

Cloud Run gives every service a deterministic URL, `https://<service>-<project number>.<region>.run.app` ([docs](https://cloud.google.com/run/docs/triggering/https-request)). `locals.tf` computes them from the project number, so the services carry `PROCESSOR_URL`, `RECEIVER_URL` and `MCP_BASE_URL` in their first revision, and the Scheduler jobs use the receiver URL as target and OIDC audience, with no dependency cycle.

## Monthly cost

All small:

- **Receiver:** scales to zero and has CPU only while a request is open, so it costs only for the seconds it serves webhooks and clicks: cents a month.
- **Processor:** billed per task while it runs (2 CPU, 2Gi). At 20 calls a week, a few dollars at most. The model API calls it makes are the real cost (SPEC "Costs").
- **Cloud Tasks and Cloud Scheduler:** inside the free tiers (the first 1 million task operations, and 3 Scheduler jobs per billing account; a 4th job is $0.10 a month).
- **Secret Manager:** about $0.06 per active secret version a month, so about $0.54 for the 9 default secrets, plus access operations, which are negligible.
- **Artifact Registry:** $0.10 per GB a month past the free 0.5 GB. The cleanup policy keeps the 10 newest images.
- **Cloud Build:** the free daily build minutes cover a deploy.
- **State bucket and MCP bucket:** a few cents.

## First-time setup

### With bootstrap.sh

```sh
bash infra/bootstrap.sh --project my-discovery-project --region us-west1   # --dry-run first to see every command
```

| Step | Skipped when |
|---|---|
| Check `gcloud`, `terraform`, `uv`, `curl`, `gcloud auth login` and `gcloud auth application-default login` | never (it stops with the fix) |
| Create the project (`--create-project`), link billing (`--billing-account <id>`) | the project exists; billing is on |
| Enable Cloud Resource Manager and Service Usage, print the receiver URL | both are on |
| Create `gs://<project>-tfstate` (versioned) and `terraform init` against it | the bucket exists; `.terraform` already points at it |
| Copy `terraform.tfvars.example` to `terraform.tfvars`, set `project_id`, `region`, and `time_zone` from `timezone` in `config/pipeline.yml` (warning when they differ) | it exists and has them |
| Stop if `repo_slug` or a GitHub App id is unset in tfvars, or `.env` lacks the Gemini, Slack or GitHub keys | all set (`nua github-app` and `nua slack-manifest` fill most of them) |
| Stage 1: `terraform plan`, ask, apply | `image` is already set, or the plan has no changes |
| `push-secrets.sh` with `CIRCLEBACK_PEOPLE` and `DEPLOY_MCP` read from tfvars | never (it adds only changed values) |
| `deploy.sh --build-only`, write `image` into tfvars | `image` is set |
| Stage 2: `terraform plan`, ask, apply | the plan has no changes |
| Restart the services (`--update-labels=secrets-refreshed=...`) | no secret changed, or the services did not exist before this run |
| `curl <receiver>/healthz`, print the webhook URLs and next steps | never |

`--yes` applies without asking (each plan is still printed). Without a terminal it refuses to apply unless `--yes` is given. Re-run it after adding a recorder's signing secret, after changing `circleback_people`, or to switch the schedules on: it does only what changed. It never deploys new code over a running image; that is `deploy.sh`.

### By hand

Needs a project with billing linked, Terraform 1.9 or newer, and `gcloud` logged in as a project owner twice: `gcloud auth login` (for gcloud) and `gcloud auth application-default login` (for Terraform). They are separate credentials and both expire. From the repo root.

1. **State bucket, once, by hand** (not Terraform: the state cannot live in a bucket it manages):

   ```sh
   PROJECT_ID=my-discovery-project
   gcloud storage buckets create gs://${PROJECT_ID}-tfstate --project=${PROJECT_ID} --location=us-west1 --uniform-bucket-level-access --public-access-prevention && gcloud storage buckets update gs://${PROJECT_ID}-tfstate --versioning
   ```

   Enable the Cloud Resource Manager API first (`gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com --project=${PROJECT_ID}`): Terraform needs it to read the project number.

2. **Init and inputs:**

   ```sh
   cd infra/terraform
   terraform init -backend-config="bucket=${PROJECT_ID}-tfstate" -backend-config="prefix=nua"
   cp terraform.tfvars.example terraform.tfvars    # fill in (repo_slug = "owner/my-discovery", the GitHub App ids); leave image = ""
   ```

3. **Stage 1:** `terraform plan`, read it, then `terraform apply`. If it says the Cloud Tasks or Cloud Scheduler service agent does not exist yet, wait a minute and apply again (the agents appear shortly after their API is enabled).

4. **Secret values,** from your `.env`, without printing them. From the repo root:

   ```sh
   bash infra/push-secrets.sh --dry-run   # what it would do
   bash infra/push-secrets.sh             # push
   ```

   It reads each value from `.env` and pipes it straight to Secret Manager, printing only one word per secret: `set`, `unchanged`, `placeholder` (not in `.env` yet, so Cloud Run can still start), `kept` (already has a value) or `missing` (run stage 1 first). Re-running it adds nothing unless a value changed. `granola-webhook-secret` gets a placeholder until the Granola webhook exists (step 8); so do `anthropic-api-key` and `openai-api-key` until you have those keys. With Circleback or MCP on, pass `CIRCLEBACK_PEOPLE="alex sam"` (person keys from `config/people.yml`) or `DEPLOY_MCP=1` to match your tfvars. Write `.env` values bare (no quotes) and the GitHub App private key on one line with literal `\n`: the script sends the first line of each value exactly as written.

5. **First image:** `PROJECT_ID=${PROJECT_ID} bash infra/deploy.sh --build-only`. It builds as `nua-build`, stages the source in `<project>-nua-build` (both from stage 1, so new projects never need the Compute Engine default account, which has no permissions there), and prints the image.

6. **Stage 2:** set `image = "<that image>"` in `terraform.tfvars`, then `terraform plan` and `terraform apply`. This creates the services and the Scheduler jobs, paused until step 9.

7. **Outputs:** `terraform output`.

8. **Point the outside world at the receiver:**
   - Granola webhook, created with the workspace API key (the signing secret is shown once):

     ```sh
     curl -X POST https://public-api.granola.ai/v1/webhook-endpoints \
       -H "Authorization: Bearer <workspace key>" -H "Content-Type: application/json" \
       -d '{"url": "<granola_webhook_url>", "scopes": ["workspace"], "events": ["note.generated", "note.access_granted"]}'
     ```

     then add its `whsec_...` secret to `granola-webhook-secret` and start a new receiver revision so it reads it (see "Rotate a secret").
   - GitHub App push webhook: `github_webhook_url`, Active, Push event subscribed, secret equal to `GITHUB_WEBHOOK_SECRET`.
   - Slack app Interactivity Request URL: `slack_interactivity_url`, then click Save Changes. Without it every button click is dropped silently.
   - Slack app Event Subscriptions: turn on, Request URL `slack_events_url`, subscribe to the bot event `app_mention`, save, then reinstall the app when Slack asks (it adds the `app_mentions:read` scope). Invite the bot to any channel it should answer in.
   - Circleback (when on): one automation per person at `circleback_webhook_urls`, tag `discovery`, outcomes Transcript, Notes, Attendees.
   - MCP (experimental, when on): `mcp_connector_url` in the connector, `mcp_oauth_callback_url` in the OAuth app.

9. **Go-live checks, then switch the schedules on.** Before this step every Scheduler job is paused. Confirm first:
   - the Granola webhook, GitHub webhook and Slack Interactivity URL from step 8 are set
   - `ingest_cutoff` is set in `config/pipeline.yml`

   Then set `schedules_on = true` in `terraform.tfvars`, `terraform plan` (it should show only the three jobs going from paused to running) and `terraform apply`. That runs `sweep` (2am), `sweep-granola` (hourly at :30) and `rollup` (Mondays 8am). To stop them, set it back to `false` and apply. Do not pause or resume jobs with gcloud: Terraform owns `paused`, and the next apply puts it back.

   `rollup-bootstrap` stays paused. Run it by hand after a backfill: `gcloud scheduler jobs run rollup-bootstrap --location=us-west1` (if gcloud refuses because it is paused, resume it, run it, pause it again).

   The receiver runs at most one instance, so a Scheduler call that lands during a cold start can get a 429 now and then. It is harmless (the next sweep covers it); raise the receiver's `max_instance_count` to 2 in `cloud_run.tf` if it happens often.

## Day 2

- **What needs what:** code changes (including prompts) need `deploy.sh`; config changes in `config/*.yml` need nothing, since every task reads them from the repo; Terraform changes need plan and apply.
- **Deploy new code:** `PROJECT_ID=<project> bash infra/deploy.sh` (`--dry-run` prints the commands first). It builds, pushes and runs `gcloud run services update <service> --image=...` for `nua-processor` and `nua-receiver`; with MCP on, `SERVICES="nua-processor nua-receiver nua-mcp"`. Terraform needs no run. There is no need to update `image` in `terraform.tfvars`: it is used only if a service is ever recreated.
- **Change infrastructure:** edit the `.tf` files or `terraform.tfvars`, `terraform plan`, read it, `terraform apply`. Env var changes start a new revision on the image the service already runs.
- **Staging:** `slack_test_mode = true` sets `NUA_SLACK_TEST=1` on every service (every Slack post goes to `#discovery-test`); set it back to `false` and apply to go live.
- **Rotate a secret:** update `.env` and run `bash infra/bootstrap.sh` (pushes it, restarts the services). By hand: add a version (`gcloud secrets versions add <name> --data-file=-`), then start a new revision so running instances read it, e.g. `gcloud run services update nua-receiver --region=us-west1 --update-labels=secrets-refreshed=$(date +%s)` (Terraform ignores template labels). Disable the old version once the new one works.
- **Receiver too slow for Slack clicks:** if clicks after a quiet spell fail often, keep one instance warm: in `cloud_run.tf` set the receiver's `min_instance_count = 1` (optionally `cpu_idle = false`), then plan and apply. One warm instance costs roughly $10 to $40 a month depending on CPU allocation.
- **Enable Circleback later:** add the people to `circleback_people`, apply, add their two secrets' values, start new revisions of the receiver and processor (or apply again after the values exist), then point their automations at `circleback_webhook_urls`.
- **Enable MCP later:** create the OAuth app, set `deploy_mcp = true`, `mcp_auth` and `mcp_oauth_client_id`, apply (creates the account, grants, bucket and secret containers, and the service too if `image` is set), add the two `mcp-` secret values, then apply again if the service failed to start for want of them. Deploy code to it with `SERVICES="nua-processor nua-receiver nua-mcp"`.
- **Switch MCP sign-in off:** set `mcp_auth = "none"`, plan and apply. The service stops being public in the same update that turns sign-in off.
- **Turn Circleback or MCP off again:** remove the people or set `deploy_mcp = false` and apply. Their secrets and the sign-in bucket are kept in the project (`deletion_policy = "ABANDON"`), just no longer managed. To turn them back on later, import what exists before applying, e.g. an `import { to = google_secret_manager_secret.secret["mcp-jwt-signing-key"]  id = "projects/<project>/secrets/mcp-jwt-signing-key" }` block, or delete them by hand if their values are not needed.

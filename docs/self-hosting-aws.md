# Self-hosting on AWS

The same production setup as [docs/self-hosting.md](self-hosting.md), on AWS instead of Google Cloud: calls flow in from your recorder by webhook, get digested on AWS, land as commits in your private GitHub repo, and show up as briefs in Slack with buttons and a bot you can ask. The code, config, Slack app, GitHub App and recorder setup are identical; only the infrastructure differs. Plan on about an hour, most of it waiting on the first build and the applies. Every secret ends up in one gitignored `.env` file and in Secrets Manager, nowhere else.

Pick one cloud. Running both against one repo would process every call twice.

Commands below assume you run them from the root of your repo clone. Replace `us-west-2` and `owner/my-discovery` with yours. The MCP server (experimental, see [the GCP guide](self-hosting.md#optional-mcp-server-experimental)) is GCP only and is not part of the AWS stack.

**Prefer a click-through setup?** `uv run python -m nua setup` opens a local setup page; pick AWS there.

## Fast path

You need `git`, [`uv`](https://docs.astral.sh/uv/), the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [Terraform](https://developer.hashicorp.com/terraform/install) 1.10+, `zip` and `curl`, an AWS account you can administer, a Slack workspace where you can install apps, a paid-tier [Gemini API key](https://aistudio.google.com/apikey) and a recorder (Granola Business or Circleback). No Docker: images build on AWS CodeBuild.

1. **Your private repo and config**, exactly as on GCP ([step 0](self-hosting.md#step-0-make-your-own-private-repo), [step 1](self-hosting.md#step-1-describe-your-discovery)):

   ```sh
   uv sync
   uv run python -m nua init
   git add config && git commit -m "Configure for our discovery" && git push
   ```

2. **Log in to AWS** ([step 2](#step-2-aws-account-and-login)): `aws configure`, or `aws sso login` with `AWS_PROFILE` set. `aws sts get-caller-identity` should print your account.

3. **Inputs.** `cp infra/aws/terraform/terraform.tfvars.example infra/aws/terraform/terraform.tfvars`, set `region` and `repo_slug`, and with Circleback list `circleback_people`. `cp .env.example .env` and add `GEMINI_API_KEY` and your recorder's API key ([step 3](#step-3-fill-in-env)). Leave the GitHub and Slack values empty for now: they need the receiver URL, which on AWS exists only after the first deploy.

4. **First bootstrap run.** It checks your tools and login, creates the state bucket, runs `terraform init`, applies stage 1 (it shows each plan and asks first), pushes the secrets from `.env`, builds the image on CodeBuild (about 5 minutes), applies stage 2 and prints the receiver URL:

   ```sh
   bash infra/aws/bootstrap.sh --region us-west-2
   ```

   `--dry-run` prints every command and runs nothing; `--yes` applies without asking.

5. **GitHub App and Slack app**, with that URL ([step 5](#step-5-github-app-and-slack-app)):

   ```sh
   uv run python -m nua github-app --repo owner/my-discovery --receiver-url https://<id>.lambda-url.us-west-2.on.aws
   uv run python -m nua slack-manifest --receiver-url https://<id>.lambda-url.us-west-2.on.aws
   ```

   Put `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` in `.env`, create the channels and invite the bot ([GCP step 3](self-hosting.md#step-3-slack-app)).

6. **Second bootstrap run.** `bash infra/aws/bootstrap.sh` again: it copies the GitHub App ids into the AWS tfvars, pushes the new secrets, applies, and restarts the receiver and the worker so they read them. Then redeliver the GitHub App's failed first ping.

7. **Recorder** ([step 6](#step-6-connect-your-recorder)): the Granola webhook or each person's Circleback automation, its signing secret in `.env`, and `bash infra/aws/bootstrap.sh` once more.

8. **Go live** ([step 8](#step-8-go-live)): `ingest_cutoff` in `config/pipeline.yml`, then `schedules_on = true` in the tfvars and one more bootstrap run.

Then [verify it works](#verifying-it-works). New code ships with `infra/aws/deploy.sh`, not the bootstrap ([Day 2](#day-2)).

## Overview

```
Recorder webhook ─────┐                    Lambda: nua-receiver (public Function URL, scales to zero)
GitHub App push ──────┤                    verifies every request, writes new transcripts,
Slack clicks/@mention ┴─────────────────>  answers Slack within 3 s, enqueues work, wakes the worker
                                                       │
EventBridge Scheduler ──────────────────────────────>  v
(sweeps, roll-up)                          SQS queue nua-tasks (+ nua-tasks-dlq), names in DynamoDB
                                                       │
                                                       v
                                           ECS Fargate: nua-worker (0 or 1 task, no inbound access)
                                           clone, run the task, commit, push, post to Slack, then
                                           scale itself to zero when the queue is empty
                                                       │
                         ┌─────────────────────────────┼──────────────────────┐
                         v                             v                      v
                 your private GitHub repo      Slack #discovery-calls    Gemini API (and
                 (transcripts, digests,        #discovery-ops            Anthropic, OpenAI
                  beliefs, indexes)                                       if you add them)
```

Each GCP piece and its AWS counterpart:

| GCP ([self-hosting.md](self-hosting.md)) | AWS | Where it is defined |
|---|---|---|
| Cloud Run `nua-receiver` (public) | Lambda `nua-receiver` from the same image, behind a Function URL (auth NONE; every route checks its own signature) and the [Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter) | `infra/aws/terraform/receiver.tf` |
| Cloud Tasks queue `nua` | SQS standard queue `nua-tasks`, dead-letter queue `nua-tasks-dlq`, DynamoDB table `nua-task-names` for task names | `queue.tf`, `nua/tasks.py` (`SqsQueue`) |
| Cloud Run `nua-processor` (private, called by Cloud Tasks) | ECS Fargate service `nua-worker` in cluster `nua-cluster`, 0 or 1 task, pulling from SQS (`python -m service.worker`). Tasks run up to 30 minutes, past Lambda's 15 | `worker.tf`, `service/worker.py` |
| Cloud Scheduler jobs, OIDC to the receiver | EventBridge Scheduler schedules in group `nua-schedules`, sending the task straight to SQS | `schedules.tf` |
| Secret Manager `granola-api-key`, ... | Secrets Manager `nua/granola-api-key`, ... (same names under `nua/`) | `secrets.tf` |
| Artifact Registry `nua` (keep 10) | ECR `nua-images` (keep 10) | `build.tf` |
| Cloud Build, bucket `<project>-nua-build` | CodeBuild `nua-build`, bucket `nua-build-<account>-<region>` | `build.tf` |
| Service accounts | One IAM role per component: `nua-receiver`, `nua-worker`, `nua-worker-execution`, `nua-scheduler`, `nua-build` | each file |
| Cloud Logging | CloudWatch log groups `/aws/lambda/nua-receiver`, `/ecs/nua-worker`, `/aws/codebuild/nua-build` (30 days) | each file |
| Billing budget | AWS Budgets `nua-monthly` (whole account) | `budget.tf` |
| GCS state bucket | S3 state bucket `nua-tfstate-<account>-<region>` (versioned, native S3 locking) | `infra/aws/bootstrap.sh` |
| (Google network) | VPC `nua-vpc` with two public subnets; the worker gets a public IP and a security group with no inbound rule, no NAT gateway | `network.tf` |

Every resource is named `nua-...` and tagged `app = "nua-discovery"` and `stack = "nua"` (the `stack` variable), so a teardown can be checked with the Resource Groups Tagging API.

Scripts in `infra/aws/`:

| Script | Does |
|---|---|
| `bootstrap.sh` | The whole setup, re-runnable; `--destroy` the whole teardown |
| `deploy.sh` | Ships code: CodeBuild, then the new image to the receiver and a new worker deployment. `--build-only`, `--restart`, `--dry-run` |
| `push-secrets.sh` | `.env` to Secrets Manager, never printing a value. `--dry-run` |
| `enqueue.sh` | Queues one task by hand and wakes the worker (the AWS `gcloud scheduler jobs run`) |

Config in `config/*.yml` is read from the repo on every task. Code (including prompts) ships in the image.

## Accounts and keys checklist

Everything in the [GCP checklist](self-hosting.md#accounts-and-keys-checklist) except the GCP project, plus:

| What | Why | Cost | Where |
|---|---|---|---|
| An AWS account, ideally one just for this | Runs everything; the budget alert covers the whole account | a few dollars a month ([Costs](#costs)) | [aws.amazon.com](https://aws.amazon.com) |
| A login that can create IAM roles, Lambda, ECS, VPC, SQS, DynamoDB, Secrets Manager, ECR, CodeBuild, S3, EventBridge Scheduler, CloudWatch, Application Auto Scaling and Budgets | Setup and teardown. `AdministratorAccess` is simplest for the person who runs Terraform; the running services get their own narrow roles | | IAM Identity Center or IAM |
| Tools on your machine | `git`, `uv`, AWS CLI v2, Terraform 1.10+, `zip`, `curl` | free | |

The `.env` values are the same as on GCP. The non-secret values (`repo_slug`, `github_app_id`, `github_app_installation_id`, `github_app_slug`) go in `infra/aws/terraform/terraform.tfvars`.

## Step 0 and step 1

Make your own private repo and describe your discovery exactly as in [GCP step 0](self-hosting.md#step-0-make-your-own-private-repo) and [step 1](self-hosting.md#step-1-describe-your-discovery). Set `timezone` in `config/pipeline.yml`, and the same zone as `time_zone` in the AWS tfvars (the schedules run on it).

## Step 2. AWS account and login

Any way the AWS CLI can log in works, and Terraform uses the same credentials (one login, not two as on GCP):

```sh
aws configure                      # access keys, or
aws sso login --profile my-admin   # IAM Identity Center, then: export AWS_PROFILE=my-admin
aws sts get-caller-identity        # prints the account and who you are
```

Pick a region close to you that has everything here (any major one does): `us-west-2`, `us-east-1`, `eu-west-1`, ... Pass it as `--region` or set `region` in the tfvars.

## Step 3. Fill in `.env`

As in [GCP step 5](self-hosting.md#step-5-fill-in-env). On a first run only `GEMINI_API_KEY` and your recorder's API key matter: everything empty is stored as `placeholder`, which the code treats as unset, so the stack starts without it. The Slack and GitHub values come in step 5, the recorder's signing secret in step 6.

`.env` is gitignored. On AWS the receiver and worker never read it: their settings come from Terraform and Secrets Manager.

## Step 4. Bootstrap: state, stage 1, secrets, image, stage 2

```sh
cp infra/aws/terraform/terraform.tfvars.example infra/aws/terraform/terraform.tfvars   # set region, repo_slug
bash infra/aws/bootstrap.sh --region us-west-2
```

It runs these, skipping whatever is done already:

1. **Tools and login:** `aws`, `terraform` (1.10 or newer), `zip`, `curl`, and `aws sts get-caller-identity`.
2. **State bucket** `nua-tfstate-<account>-<region>`: versioned, public access blocked, tagged. The state cannot live in a bucket Terraform manages, so the script makes it. Locking uses S3's own conditional writes (`use_lockfile = true`, Terraform 1.10+): there is no DynamoDB lock table.
3. **`terraform init`** against that bucket, key `nua/terraform.tfstate`.
4. **Inputs:** `terraform.tfvars` from the example if missing, `region` written in, and the GitHub App ids copied from `infra/terraform/terraform.tfvars` when `nua github-app` wrote them there. It stops only without `repo_slug`.
5. **Stage 1** (`image = ""`): VPC, roles, the empty secrets, ECR repo, CodeBuild project and bucket, the queue, DLQ and names table, the cluster, the schedule group. It shows the plan and asks.
6. **Secrets:** `infra/aws/push-secrets.sh` (below).
7. **First image:** `infra/aws/deploy.sh --build-only` zips `pyproject.toml`, `uv.lock`, `nua/` and `service/`, uploads it to the build bucket and runs CodeBuild, which builds `service/Dockerfile` and pushes `nua-images:<tag>` and `nua-images:live`. The image goes into the tfvars as `image = "..."`.
8. **Stage 2:** the Lambda function and Function URL, the worker's task definition, service, scaling and alarms, and the four schedules (all disabled). It shows the plan and asks.
9. **Health:** `curl <receiver URL>/healthz`, then the URLs to give Slack, GitHub and your recorder.

On a re-run, a secret that changed restarts the receiver and the worker (`deploy.sh --restart`) so they read it.

**The same by hand,** from the repo root (account `123456789012`):

```sh
aws s3api create-bucket --bucket nua-tfstate-123456789012-us-west-2 --region us-west-2 \
  --create-bucket-configuration LocationConstraint=us-west-2      # leave this line out in us-east-1
aws s3api put-bucket-versioning --bucket nua-tfstate-123456789012-us-west-2 --versioning-configuration Status=Enabled
terraform -chdir=infra/aws/terraform init -backend-config="bucket=nua-tfstate-123456789012-us-west-2" \
  -backend-config="key=nua/terraform.tfstate" -backend-config="region=us-west-2"
terraform -chdir=infra/aws/terraform plan -out=stage1.tfplan && terraform -chdir=infra/aws/terraform apply stage1.tfplan
bash infra/aws/push-secrets.sh --dry-run && bash infra/aws/push-secrets.sh
bash infra/aws/deploy.sh --build-only         # prints image = "..."; put it in terraform.tfvars
terraform -chdir=infra/aws/terraform plan -out=stage2.tfplan && terraform -chdir=infra/aws/terraform apply stage2.tfplan
terraform -chdir=infra/aws/terraform output
```

`push-secrets.sh` reads each value from `.env` and pipes it to Secrets Manager (through `file:///dev/stdin`, never a command line), printing one word per secret: `set`, `unchanged`, `placeholder` (not in `.env` and no value yet, so the worker can start), `kept` (already has a value) or `missing` (apply stage 1 first). It takes `circleback_people` from the tfvars, or `CIRCLEBACK_PEOPLE="alex sam"`. Write `.env` values bare and the GitHub App key on one line with literal `\n`, as on GCP.

Stage 1 must exist before the first build: `deploy.sh` uses the CodeBuild project, bucket and image repo it creates.

## Step 5. GitHub App and Slack app

On GCP the receiver URL is known before the receiver exists; on AWS a Function URL is random, so these come after step 4. Take the URL from the end of the bootstrap output or `terraform -chdir=infra/aws/terraform output -raw receiver_url`. It looks like `https://abcdefgh1234.lambda-url.us-west-2.on.aws` and stays the same for the life of the function (a destroy and re-create gives a new one).

- **GitHub App:** [GCP step 4](self-hosting.md#step-4-github-app), with `--receiver-url`:

  ```sh
  uv run python -m nua github-app --repo owner/my-discovery --receiver-url https://<id>.lambda-url.us-west-2.on.aws
  ```

  It writes the key and webhook secret to `.env` and the ids to `infra/terraform/terraform.tfvars` (the GCP file); the next bootstrap run copies them into the AWS tfvars. By hand, the webhook URL is `<receiver URL>/github`.
- **Slack app:** [GCP step 3](self-hosting.md#step-3-slack-app), with `uv run python -m nua slack-manifest --receiver-url https://<id>.lambda-url.us-west-2.on.aws`. The receiver is up, so Slack can verify the events URL right away.

Then run `bash infra/aws/bootstrap.sh` again. It pushes the new secrets, applies the ids (the receiver and the worker pick up new settings on their next start) and restarts both. In the GitHub App, **Advanced > Recent deliveries > Redeliver** the failed first ping: it should get a 200.

## Step 6. Connect your recorder

Exactly [GCP step 8a (Granola)](self-hosting.md#8a-granola) or [8b (Circleback)](self-hosting.md#8b-circleback), with two differences:

- The URLs are `<receiver URL>/ingest/granola` and `<receiver URL>/ingest/circleback/<person>` (`terraform -chdir=infra/aws/terraform output` lists them).
- Instead of `gcloud run services update ... --update-labels=secrets-refreshed=...`, push and restart with `bash infra/aws/bootstrap.sh` (or `bash infra/aws/push-secrets.sh && bash infra/aws/deploy.sh --restart`).

With Circleback, `circleback_people` in the AWS tfvars creates each person's two secrets; apply before pushing their values.

## Step 7. Slack URLs and the GitHub webhook

Check the three Slack settings and the GitHub webhook as in [GCP step 9](self-hosting.md#step-9-slack-urls-and-the-github-webhook), with your Function URL as `RECEIVER_URL`. Each fails silently when missed.

## Step 8. Go live

As [GCP step 10](self-hosting.md#step-10-go-live), with these commands:

1. Set `ingest_cutoff` in `config/pipeline.yml`, commit and push. Until then every recorder webhook is acknowledged and ignored.
2. Set `schedules_on = true` in `infra/aws/terraform/terraform.tfvars` and run `bash infra/aws/bootstrap.sh` (or `terraform plan` and `apply`). The plan should show only `nua-sweep`, `nua-sweep-granola` and `nua-rollup` going from `DISABLED` to `ENABLED`. Never enable or disable schedules in the console: Terraform owns that, and the next apply puts it back.
3. Backfill your past calls by pushing exports into `transcripts/`, as on GCP.
4. Once the backfill is digested, run the bootstrap roll-up. EventBridge Scheduler has no "run now", and `nua-rollup-bootstrap` is never enabled, so queue it by hand:

   ```sh
   bash infra/aws/enqueue.sh rollup '{"bootstrap": true}'
   ```

5. Approve the proposals you agree with in `#discovery-calls`.

## Verifying it works

| Where | What you should see |
|---|---|
| A test call | As on GCP: a short solo Granola note after the cutoff, shared to the Team space, or a Circleback meeting tagged `discovery`. A brief appears in `#discovery-calls` within a few minutes; the first task after a quiet spell waits a minute or two for the worker to start |
| `#discovery-ops` | Errors with **Re-run** buttons, sweep catches. Quiet is good |
| GitHub | Commits by `<slug>[bot]`: `Add transcripts/...`, then `process_call: digest ...` |
| Receiver logs | `aws logs tail /aws/lambda/nua-receiver --follow --region us-west-2` |
| Worker logs | `aws logs tail /ecs/nua-worker --follow --region us-west-2`: `worker started`, `task process_call ended: ok=True`, then `idle for 120s; service set to 0 tasks, exiting` |
| Queue | `aws sqs get-queue-attributes --queue-url $(terraform -chdir=infra/aws/terraform output -raw queue_url) --attribute-names All --region us-west-2`: the three `ApproximateNumberOfMessages*` are 0 once work is done |
| Dead-letter queue | The same for `dead_letter_queue_url`: always 0. A message there is one whose worker died five times (see Troubleshooting) |
| Worker service | `aws ecs describe-services --cluster nua-cluster --services nua-worker --query 'services[0].[desiredCount,runningCount]' --region us-west-2`: `[0, 0]` when idle, `[1, 1]` while working |
| Schedules | `aws scheduler list-schedules --group-name nua-schedules --query 'Schedules[].[Name,State]' --output text --region us-west-2`: three `ENABLED` after go-live, `nua-rollup-bootstrap` `DISABLED` |
| Slack bot | `@Nua Discovery list beliefs`, then a real question. The answer ends with `read N/N digests` |
| Terraform | `terraform -chdir=infra/aws/terraform plan` shows no changes, even after code deploys |

Optional, for staging: set `slack_test_mode = true` and apply, and every Slack post goes to `channels.test` (default `#discovery-test`). With `channels.test` empty, test mode refuses to run.

## How it behaves on AWS

**Receiver on Lambda.** The image runs uvicorn exactly as on Cloud Run; the Lambda Web Adapter extension (copied into the image from `public.ecr.aws/awsguru/aws-lambda-adapter` at a pinned version, inert anywhere else) forwards each invocation to it once `/healthz` answers. Three Lambda rules matter, and the receiver already meets them:

- **Nothing runs after the response.** Lambda freezes the environment the moment it answers. The receiver does all its work (verify, write the transcript, enqueue, lock a Slack message) before it answers, as Cloud Run's request-only CPU already required. It starts no background task or thread.
- **Only `/tmp` is writable.** The clone lives in `/tmp/nua-repo` (2 GB of `/tmp`), and `HOME=/tmp`.
- **Secrets load once, at cold start.** `NUA_AWS_SECRETS` lists `ENV_VAR=secret-name` pairs; `nua/settings.py` (`load_aws_secrets`) reads them in one `BatchGetSecretValue` call before the app starts, and a missing secret fails the start. The worker gets its secrets from ECS directly.

**Slack's 3 seconds.** A cold start is the image start, uvicorn and that one Secrets Manager call: about 1.5 to 3 seconds at one full vCPU (`receiver_memory_mb = 1769`). A click after a long quiet spell can still miss now and then (Slack shows an error and the person clicks again), as on GCP. If it happens often, set `receiver_keep_warm = true`: an empty invocation every 5 minutes keeps an environment started, for cents a month. This is the AWS counterpart of GCP's `min_instance_count = 1`. Provisioned concurrency would guarantee a warm environment, but costs about $15 to $20 a month and needs a published version and an alias that every deploy moves, so it is not built in.

**The queue.** How Cloud Tasks' behaviour maps to SQS:

| Cloud Tasks | AWS |
|---|---|
| One task at a time | One worker task, one message at a time |
| 5 attempts, backoff 60s doubling to 1800s | The worker reads the attempt from SQS's receive count and, on a failure, sets the message's visibility timeout to the same backoff. The fifth attempt always ends the task (the runner reports it to `#discovery-ops` with a Re-run button, as on GCP) |
| Dispatch deadline 1800s | Visibility timeout 2100s, above the runner's 25-minute budget plus the clone. The runner stops a task at its budget either way |
| Task names block a duplicate for about an hour | A conditional put in DynamoDB `nua-task-names` holds each name for its delay plus an hour; a second enqueue returns False ("Already queued" in Slack, one answer per Slack retry) |
| `schedule_time` (any delay) | `DelaySeconds`, at most 15 minutes. Longer delays carry a `not_before` time and the worker sends the message back until then. The default delays (15-minute duplicate wait, 5-minute backcheck debounce) fit |
| Retries exhausted | The message is deleted after the final report. The dead-letter queue (`maxReceiveCount` 5) only catches a message whose worker was killed every time |

**The worker and its cold start.** The ECS service runs 0 or 1 task. Every enqueue (receiver or worker) sets its desired count to 1, so a call or a question starts work at once; the worker sets it back to 0 and exits after `worker_idle_seconds` (default 120) with nothing visible, in flight or delayed on the queue, and takes the work back if something arrived while it scaled in. Two CloudWatch alarms back this up: waiting messages for a minute start the worker (this is how the schedules wake it, since they write to SQS directly), and an empty queue for 30 minutes stops it. A start takes **a minute or two**: Fargate places the task and pulls the image. So the first brief after a quiet spell comes a minute or two later than on GCP, and later tasks in the same burst start at once. A deploy stops the old worker before it starts a new one, so there are never two.

**Schedules bypass the receiver.** On GCP, Cloud Scheduler POSTs to `/sweep` and `/rollup` with a Google OIDC token and the receiver enqueues `sweep {"granola_only": ...}` or `rollup {"bootstrap": ...}`. On AWS each schedule sends that same task to SQS as the `nua-scheduler` role. The receiver's `/sweep` and `/rollup` routes still exist but refuse every call, since there is no Google token to check.

**Code and Terraform do not fight.** `deploy.sh` sets the receiver's image with the CLI and Terraform ignores `image_uri`. The worker runs the `live` tag, which every build moves; `deploy.sh` starts a new ECS deployment so the service resolves it again. Everything else is Terraform's: change it in the `.tf` files, not in the console.

## Day 2

| Task | How |
|---|---|
| Deploy code (including prompt changes) | From a clean, up-to-date checkout: `bash infra/aws/deploy.sh` (`--dry-run` prints the commands first). About 5 minutes of CodeBuild, then the receiver's new image and a new worker deployment. No Terraform run, no tfvars change |
| Change config (`config/*.yml`) | Commit and push. Every task reads config from the repo |
| Change infrastructure | Edit the `.tf` files or `terraform.tfvars`, `terraform -chdir=infra/aws/terraform plan`, read it, `apply`. An env var change gives the receiver fresh environments and the worker a new task definition |
| Rotate a secret | Update `.env`, `bash infra/aws/push-secrets.sh`, then `bash infra/aws/deploy.sh --restart`: fresh receiver environments, and a new worker deployment (a task the worker was running at that moment is retried later). Secrets Manager keeps the previous value as `AWSPREVIOUS` |
| Change models | Edit `config/pipeline.yml` and bump `prompt_version`, as on GCP. A new provider's key: `.env`, `push-secrets.sh`, `deploy.sh --restart` |
| Add a teammate | As on GCP; with Circleback, add them to `circleback_people`, apply, push their secrets, and do [step 8b](self-hosting.md#8b-circleback) for them |
| Run a task now | `bash infra/aws/enqueue.sh sweep`, `... rollup '{"bootstrap": true}'`, `... reindex` |
| Pause everything scheduled | `schedules_on = false`, apply. Webhooks keep working |
| Stop ingesting | `ingest_cutoff: null` in `config/pipeline.yml`, push |
| Slack clicks fail after quiet spells | `receiver_keep_warm = true`, apply |
| Worker too slow or out of memory | Raise `worker_cpu` and `worker_memory_mb` (valid Fargate pairs: 1024 with 2048 to 8192, 2048 with 4096 to 16384), apply |
| Switch to arm64 (about 20% cheaper) | Set `architecture = "arm64"` before the first build: the build host, the image, the function and the task must all agree. To switch a running stack, tear it down and bootstrap again (the function's architecture and its image cannot change one at a time) |
| A message in the dead-letter queue | Read it (`aws sqs receive-message --queue-url <dead_letter_queue_url>`), fix the cause, and queue the task again with `enqueue.sh`; then delete it from the DLQ |

### Checking a change offline

None of these touch AWS:

```sh
cd infra/aws/terraform
terraform fmt -check
terraform init -backend=false && terraform validate
terraform test                       # stage 1, stage 2 and go-live against a mocked AWS provider
cd ../../..
uv run pytest -q tests/unit/test_aws.py tests/unit/test_infra_aws.py
```

## Costs

Prices are us-west-2 on-demand list prices, without free tier, as of 2026; check the AWS pricing pages for your region. Model calls are billed by the model providers, not AWS, and are the real cost: $0.25 to $2 per call and per Slack question, capped per task by `cost_cap_usd_per_task`.

**Idle, before go-live (schedules off):** about **$4 a month**.

| Item | Monthly |
|---|---|
| Secrets Manager: $0.40 per secret, 9 by default (plus 2 per Circleback person) | $3.60 |
| ECR storage: $0.10 per GB, the 10 kept images share most layers (about 1 GB) | about $0.10 |
| CloudWatch alarms: 5 alarm metrics at $0.10 (free within the 10-alarm free tier) | $0 to $0.50 |
| S3 (state, build sources), DynamoDB on demand, SQS, EventBridge Scheduler, CloudWatch Logs at this volume | cents |
| Lambda, Fargate, CodeBuild, VPC, internet gateway | $0 while idle |

**Live, schedules on:** about **$7 a month** plus calls. The hourly Granola sweep starts the worker 720 times a month; each start costs about 4 to 5 minutes of Fargate (pull, sweep, 2 idle minutes): about 55 hours a month at $0.054 an hour (1 vCPU, 2 GB, x86, plus the public IP) is about **$3**. arm64 makes it about $2.40; a shorter `worker_idle_seconds` makes it less.

**Per call:** a `process_call` keeps the worker up about 10 to 15 minutes (start, the task, the idle wait): about **$0.01** of Fargate. The receiver's Lambda time and the SQS, DynamoDB and Secrets Manager calls are fractions of a cent. A deploy is about 5 build minutes, $0.025 (CodeBuild's free tier covers 100 minutes a month).

**Compared with GCP:** a little more at idle, mostly Secrets Manager ($0.40 a secret against Google's $0.06). `receiver_keep_warm` adds cents; provisioned concurrency would add $15 to $20; a NAT gateway (not used) would add about $33.

Set `budget_emails` for the `nua-monthly` budget alert (default $25 a month; it emails, it never stops anything). It covers the whole account, so an account of its own makes it a budget for this stack.

## Teardown

```sh
bash infra/aws/bootstrap.sh --destroy                         # everything Terraform made; keeps the state bucket
bash infra/aws/bootstrap.sh --destroy --delete-state-bucket   # and the state bucket, so nothing is left
```

It asks you to type the stack name, shows the destroy plan and asks again (`--yes` skips both), then:

- `terraform destroy` removes every resource, including their data: the build bucket and ECR repo empty themselves (`force_destroy`, `force_delete`), every `nua/` secret is deleted at once with its value (`recovery_window_in_days = 0`, so keep your `.env`), and the log groups go because Terraform declares them.
- It lists anything still tagged `app = nua-discovery` and `stack = nua` (Resource Groups Tagging API), IAM roles named `nua-...` and log groups under the three prefixes. Expect only the state bucket, unless you deleted it. The tagging API can show a just-deleted resource for a few minutes: run `--destroy` again to re-check.
- AWS's own service-linked roles (`AWSServiceRoleForECS`, `AWSServiceRoleForApplicationAutoScaling_ECSService`) stay: they are free and shared by the whole account.
- `image` is cleared in the tfvars, so a later bootstrap starts from stage 1 with a fresh build. The Function URL will be new: update Slack, GitHub and your recorder.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `terraform apply` fails creating the ECS service or the scaling target with a service-linked role error | The first ECS or Application Auto Scaling use in an account creates its service-linked role, which can take a moment. Run the bootstrap (or the apply) again |
| `deploy.sh`: build `FAILED` | `aws logs tail /aws/codebuild/nua-build --since 1h`. A `toomanyrequests` pull error comes from a registry rate limit; the base image already comes from ECR Public, so wait and retry. An `exec format error` later means the image and `architecture` disagree |
| Receiver answers 502 or `Internal Server Error` on every route | The start failed: `aws logs tail /aws/lambda/nua-receiver`. `could not read secrets` names the secret: run `push-secrets.sh` (a secret with no value at all fails the start; `placeholder` is fine) |
| Routes that read the repo answer 503 | The GitHub App ids or key are not set yet (step 5), or the installation does not cover the repo. The logs name the missing variable |
| `/sweep` or `/rollup` answers 401 | Expected on AWS: schedules write to SQS directly. Use `enqueue.sh` to run one now |
| A call takes minutes to show up | The worker was at zero: a start takes a minute or two. If nothing happens for 10 minutes, check `desiredCount` (Verifying) and the worker logs; a task that cannot start shows the reason in `aws ecs describe-services ... --query 'services[0].events[:5]'` (often `ResourceInitializationError: unable to pull secrets`: a secret has no value, run `push-secrets.sh`) |
| The worker starts and stops every few minutes with nothing to do | A message it cannot finish is cycling: check the worker logs for the task and `#discovery-ops`. After five receives it lands in the dead-letter queue and the cycle ends |
| A message in the dead-letter queue | Its worker died five times running it (out of memory, or killed by a deploy). Raise `worker_memory_mb` if the logs show the task killed, then queue it again with `enqueue.sh` |
| Slack clicks fail after quiet spells | Cold starts past 3 seconds. `receiver_keep_warm = true` |
| `reserved concurrency` error on apply | `receiver_max_concurrency` needs an account concurrency quota above 100; new accounts start at 10. Leave it `null`, or ask AWS for a higher quota |
| A new secret value is not picked up | The receiver and the worker keep the values they started with. `bash infra/aws/deploy.sh --restart` |
| `push-secrets.sh` says `missing` | Apply stage 1, and add Circleback people to `circleback_people` before pushing their secrets |
| A code change has no effect | Code ships only with `infra/aws/deploy.sh` |
| `terraform plan` wants to change the schedules after you used the console | Terraform owns `state`. Use `schedules_on`, apply |
| Recorder, Slack and GitHub symptoms | Same causes as on GCP: [Troubleshooting](self-hosting.md#troubleshooting) |

## Security notes

Everything in the [GCP security notes](self-hosting.md#security-notes) holds (private repo, every public route checks a signature, who can trigger what, Slack Connect, model providers see your transcripts, recording consent), with these AWS specifics:

- **The Function URL is public by design** (auth NONE), as the Cloud Run receiver is. Every route verifies its sender's signature; `/sweep` and `/rollup` refuse everything. The resource policy allows `lambda:InvokeFunction` only for calls through the URL, so the function cannot be invoked directly by another account.
- **The worker accepts no connection.** Its security group has no inbound rule; it only calls out. Only the receiver, the worker itself and the scheduler role may send to the queue.
- **Least privilege per component.** The receiver may send to the queue, write task names, set the worker's desired count and read its own six secrets (plus Circleback's). The worker's task role may use the queue and the names table and scale its own service, and its execution role may pull the image, write its logs and read its own six secrets. Model keys never reach the receiver. CodeBuild may read the build bucket and push to the one repo. Each role trusts only its AWS service, with `aws:SourceAccount` where the service supports it.
- **Secrets never touch Terraform.** Terraform creates empty secrets; `push-secrets.sh` pipes values from `.env` through stdin. No value is in a `.tf` file, the tfvars or the state. The state bucket is still worth protecting: it names every resource. It is private and versioned.
- **Encryption at rest** everywhere by default: Secrets Manager and SQS with AWS managed keys, S3 with SSE-S3, DynamoDB with AWS owned keys.

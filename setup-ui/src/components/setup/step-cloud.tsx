"use client";

import { useState } from "react";

import { api, type Cloud, type DeployInfo, type TfValue } from "@/lib/api";
import { Switch } from "@/components/ui/switch";
import { Text } from "@/components/ui/text";
import { ChoiceCards, Field, Note, Section, useSave } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";

type FieldSpec = { key: string; label: string; hint: string; placeholder?: string; mono?: boolean };

const GCP_FIELDS: FieldSpec[] = [
  { key: "project_id", label: "Project id", hint: "An existing project, or a new id for the bootstrap to create.", placeholder: "acme-discovery", mono: true },
  { key: "region", label: "Region", hint: "Where Cloud Run and the queue live.", placeholder: "us-west1", mono: true },
  { key: "billing_account", label: "Billing account", hint: "For the budget alert, and to link a new project. gcloud billing accounts list shows it.", placeholder: "XXXXXX-XXXXXX-XXXXXX", mono: true },
  { key: "monthly_budget_usd", label: "Monthly budget alert (USD)", hint: "Emails billing admins as spend approaches it.", placeholder: "25" },
];

const AWS_FIELDS: FieldSpec[] = [
  { key: "region", label: "Region", hint: "Where Lambda, ECS and the queue live.", placeholder: "us-west-2", mono: true },
  { key: "architecture", label: "Architecture", hint: "x86_64 or arm64 (about 20% cheaper). Pick before the first build.", placeholder: "x86_64", mono: true },
  { key: "budget_emails", label: "Budget alert emails", hint: "Comma separated. Empty: no alert. The budget covers the whole account.", placeholder: "alex@acme.dev" },
  { key: "monthly_budget_usd", label: "Monthly budget alert (USD)", hint: "", placeholder: "25" },
];

const REPO_FIELDS: FieldSpec[] = [
  { key: "repo_slug", label: "Your private repo", hint: "owner/name on GitHub. It must be private: it holds real conversations.", placeholder: "acme/discovery", mono: true },
  { key: "repo_branch", label: "Branch", hint: "", placeholder: "main", mono: true },
];

function asText(v: TfValue | undefined): string {
  if (Array.isArray(v)) return v.join(", ");
  if (v === null || v === undefined) return "";
  return String(v);
}

export function StepCloud({ deploy, nav, onSaved }: { deploy: DeployInfo; nav: StepNav; onSaved: () => void }) {
  const [cloud, setCloud] = useState<Cloud>(deploy.cloud);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(deploy.fields).map(([k, v]) => [k, asText(v)])),
  );
  const [slackTest, setSlackTest] = useState(Boolean(deploy.fields.slack_test_mode));
  const [warm, setWarm] = useState(Boolean(deploy.fields.receiver_keep_warm));
  const [receiverUrl, setReceiverUrl] = useState(deploy.receiver_url_override);
  const save = useSave();
  const fields = cloud === "gcp" ? GCP_FIELDS : AWS_FIELDS;

  function submit() {
    const out: Record<string, TfValue> = {};
    for (const f of [...fields, ...REPO_FIELDS]) {
      const v = (values[f.key] || "").trim();
      if (f.key === "budget_emails") out[f.key] = v.split(/[,\s]+/).filter(Boolean);
      else if (f.key === "monthly_budget_usd") out[f.key] = v || "25";
      else if (v || f.key === "billing_account") out[f.key] = v;
    }
    out.slack_test_mode = slackTest;
    if (cloud === "aws") out.receiver_keep_warm = warm;
    save
      .run(
        () => api<DeployInfo>("deploy", "PUT", { cloud, fields: out, receiver_url: receiverUrl.trim() }),
        (r) => `Wrote ${r.tfvars_path}`,
      )
      .then((r) => {
        if (r) onSaved();
      });
  }

  function changeCloud(next: string) {
    setCloud(next as Cloud);
    save
      .run(() => api<DeployInfo>("deploy", "PUT", { cloud: next }), (r) => `Switched to ${next.toUpperCase()}: ${r.tfvars_path}`)
      .then((r) => {
        if (r) onSaved();
        else setCloud(deploy.cloud);
      });
  }

  return (
    <StepFrame
      nav={nav}
      title="Cloud"
      intro="Where the receiver and processor run. Everything here is a non-secret input for terraform.tfvars."
      onSave={submit}
      saving={save.saving}
      problems={save.problems}
    >
      <Section title="Provider">
        <ChoiceCards
          label="Cloud"
          value={cloud}
          onChange={changeCloud}
          choices={[
            { value: "gcp", title: "Google Cloud", description: "Cloud Run, Cloud Tasks, Scheduler and Secret Manager." },
            {
              value: "aws",
              title: "AWS",
              description: deploy.aws_ready
                ? "Lambda, ECS Fargate, SQS, EventBridge and Secrets Manager."
                : "Not in this checkout yet. Pull the latest code.",
              disabled: !deploy.aws_ready,
            },
          ]}
        />
        <Text size="xs" color="faint">
          Saved to{" "}
          <Text size="xs" mono color="faint">
            {deploy.tfvars_path}
          </Text>
          . Switching keeps the other cloud&apos;s file as it is.
        </Text>
      </Section>
      <Section
        title={cloud === "gcp" ? "Project" : "Account"}
        description={
          cloud === "aws" ? "The account is whichever one your AWS CLI is logged in to (step 9 checks it)." : undefined
        }
      >
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {fields.map((f) => (
            <Field
              key={`${cloud}-${f.key}`}
              id={`tf-${f.key}`}
              label={f.label}
              hint={f.hint}
              placeholder={f.placeholder}
              mono={f.mono}
              value={values[f.key] || ""}
              onChange={(v) => setValues({ ...values, [f.key]: v })}
            />
          ))}
        </div>
      </Section>
      <Section title="Repo">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {REPO_FIELDS.map((f) => (
            <Field
              key={f.key}
              id={`tf-${f.key}`}
              label={f.label}
              hint={f.hint}
              placeholder={f.placeholder}
              mono={f.mono}
              value={values[f.key] || ""}
              onChange={(v) => setValues({ ...values, [f.key]: v })}
            />
          ))}
        </div>
      </Section>
      <Section title="Schedules and options">
        <div className="flex flex-col gap-3">
          <Field
            id="tf-time-zone"
            label="Time zone for the scheduled jobs"
            value={deploy.timezone}
            onChange={() => undefined}
            disabled
            mono
            hint="Mirrors the time zone in step 2. Change it there."
          />
          <div className="flex items-center gap-3">
            <Switch id="slack-test" checked={slackTest} onCheckedChange={setSlackTest} />
            <label htmlFor="slack-test" className="cursor-pointer">
              <Text size="sm">Staging: send every Slack post to the test channel</Text>
            </label>
          </div>
          {cloud === "aws" && (
            <div className="flex items-center gap-3">
              <Switch id="keep-warm" checked={warm} onCheckedChange={setWarm} />
              <label htmlFor="keep-warm" className="cursor-pointer">
                <Text size="sm">Keep one receiver warm (cents a month, fewer slow Slack clicks)</Text>
              </label>
            </div>
          )}
        </div>
      </Section>
      <Section
        title="Receiver URL"
        description="Usually found for you: from Terraform, or on GCP from the project number. Paste it only if the next steps cannot find it."
      >
        <Field
          id="receiver-url"
          label="Receiver URL (optional)"
          mono
          placeholder="https://nua-receiver-123456789.us-west1.run.app"
          value={receiverUrl}
          onChange={setReceiverUrl}
        />
        {cloud === "aws" && (
          <Note
            title="On AWS the URL exists only after the first deploy."
            description="Run the deploy command in step 9 once, then come back for the GitHub and Slack apps, then run it again."
          />
        )}
      </Section>
    </StepFrame>
  );
}

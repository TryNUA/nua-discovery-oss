"use client";

import { useState } from "react";

import { api, refreshLive, useApi, type Cloud, type DeployInfo, type ReceiverInfo, type TfValue } from "@/lib/api";
import { Switch } from "@/components/ui/switch";
import { Text } from "@/components/ui/text";
import { ChoiceCards, Field, Note, Section } from "@/components/setup/common";
import { useDraft, useDraftFields, useFlush, useProblems, useQuietSet, useSaver, type FieldProblem } from "@/components/setup/store";

function useCloudDrafts(): Set<string> {
  return new Set(Object.keys(useDraftFields("cloud")));
}
import { StepFrame, type StepNav } from "@/components/setup/frame";
import {
  AWS_ARCH,
  AWS_BUDGET,
  AWS_REGION,
  AWS_SIGN_IN,
  GCP_BILLING,
  GCP_BUDGET,
  GCP_PROJECT,
  GCP_REGION,
  GCP_SIGN_IN,
  HowTo,
  RECEIVER_GUIDE,
  REPO_GUIDE,
  type HowToGuide,
} from "@/components/setup/howto";

type FieldSpec = { key: string; label: string; hint: string; placeholder?: string; mono?: boolean; guide?: HowToGuide };

const GCP_FIELDS: FieldSpec[] = [
  { key: "project_id", label: "Project id", hint: "An existing project, or a new id for the bootstrap to create.", placeholder: "acme-discovery", mono: true, guide: GCP_PROJECT },
  { key: "region", label: "Region", hint: "Where Cloud Run and the queue live.", placeholder: "us-west1", mono: true, guide: GCP_REGION },
  { key: "billing_account", label: "Billing account", hint: "For the budget alert, and to link a new project.", placeholder: "XXXXXX-XXXXXX-XXXXXX", mono: true, guide: GCP_BILLING },
  { key: "monthly_budget_usd", label: "Monthly budget alert (USD)", hint: "An email at 50%, 90% and 100%. It never stops anything.", placeholder: "25", guide: GCP_BUDGET },
];

const AWS_FIELDS: FieldSpec[] = [
  { key: "region", label: "Region", hint: "Where Lambda, ECS and the queue live.", placeholder: "us-west-2", mono: true, guide: AWS_REGION },
  { key: "architecture", label: "Architecture", hint: "x86_64 or arm64 (about 20% cheaper). Pick before the first build.", placeholder: "x86_64", mono: true, guide: AWS_ARCH },
  { key: "budget_emails", label: "Budget alert emails", hint: "Comma separated. Empty: no alert. The budget covers the whole account.", placeholder: "alex@acme.dev", guide: AWS_BUDGET },
  { key: "monthly_budget_usd", label: "Monthly budget alert (USD)", hint: "An email at 50%, 90% and 100%. It never stops anything.", placeholder: "25" },
];

const REPO_FIELDS: FieldSpec[] = [
  { key: "repo_slug", label: "Your private repo", hint: "owner/name on GitHub. It must be private: it holds real conversations.", placeholder: "acme/discovery", mono: true, guide: REPO_GUIDE },
  { key: "repo_branch", label: "Branch", hint: "main unless you changed it.", placeholder: "main", mono: true },
];

function asText(v: TfValue | undefined): string {
  if (Array.isArray(v)) return v.join(", ");
  if (v === null || v === undefined) return "";
  return String(v);
}

/** The receiver URL the next steps will use right now, and where it came from. */
function ReceiverNow() {
  const { data, isLoading } = useApi<ReceiverInfo>("receiver");
  if (isLoading || !data) {
    return (
      <Text size="sm" color="faint">
        Looking up the receiver URL...
      </Text>
    );
  }
  if (!data.url) {
    return <Note title="Not known yet" description={data.how_to} variant="warn" />;
  }
  return (
    <div className="flex flex-col gap-1 rounded-md border border-rule bg-bg-raised shadow-xs px-4 py-3 min-w-0">
      <Text size="sm" color="muted">
        We will use
      </Text>
      <Text size="sm" mono className="break-all">
        {data.url}
      </Text>
      {data.detail && (
        <Text size="xs" color="faint">
          {data.detail}
        </Text>
      )}
    </div>
  );
}

export function StepCloud({ deploy, nav }: { deploy: DeployInfo; nav: StepNav }) {
  const step = "cloud" as const;
  const [cloud] = useDraft<Cloud>(step, "cloud", () => deploy.cloud);
  // each cloud keeps its own draft, so switching back and forth never loses input
  const [values, setValues] = useDraft<Record<string, string>>(step, `values:${cloud}`, () =>
    Object.fromEntries(Object.entries(deploy.fields).map(([k, v]) => [k, asText(v)])),
  );
  const [warm, setWarm] = useDraft(step, `warm:${cloud}`, () => Boolean(deploy.fields.receiver_keep_warm));
  const flush = useFlush(step);
  const drafts = useCloudDrafts();
  const [awsProfile, setAwsProfile] = useDraft(step, "awsProfile", () => deploy.aws_profile || "");
  const [receiverUrl, setReceiverUrl] = useDraft(step, "receiverUrl", () => deploy.receiver_url_override);
  const quiet = useQuietSet(step);
  const { error } = useProblems(step);
  const [switching, setSwitching] = useState(false);
  const fields = cloud === "gcp" ? GCP_FIELDS : AWS_FIELDS;

  useSaver(step, () => {
    const out: Record<string, TfValue> = {};
    for (const f of [...fields, ...REPO_FIELDS]) {
      const v = (values[f.key] || "").trim();
      if (f.key === "budget_emails") out[f.key] = v.split(/[,\s]+/).filter(Boolean);
      else if (f.key === "monthly_budget_usd") out[f.key] = v || "25";
      else if (v || f.key === "billing_account") out[f.key] = v;
    }
    if (cloud === "aws") out.receiver_keep_warm = warm;
    return api<DeployInfo & { problems: FieldProblem[] }>("deploy", "PUT", {
      cloud,
      fields: out,
      receiver_url: receiverUrl.trim(),
      ...(cloud === "aws" ? { aws_profile: awsProfile.trim() } : {}),
    }).then((r) => {
      refreshLive();
      return r.problems || [];
    });
  });

  function changeCloud(next: string) {
    // first finish saving this cloud's fields, then switch and load the other cloud's tfvars
    setSwitching(true);
    flush()
      .then(() => api<DeployInfo>("deploy", "PUT", { cloud: next }))
      .then((r) => {
        if (!drafts.has(`values:${next}`)) {
          quiet(`values:${next}`, Object.fromEntries(Object.entries(r.fields).map(([k, v]) => [k, asText(v)])));
        }
        if (!drafts.has(`warm:${next}`)) quiet(`warm:${next}`, Boolean(r.fields.receiver_keep_warm));
        quiet("cloud", next);
        refreshLive();
      })
      .catch(() => undefined)
      .finally(() => setSwitching(false));
  }

  return (
    <StepFrame
      nav={nav}
      title="Cloud"
      intro="Where the receiver and processor run. Everything here is a non-secret input for terraform.tfvars."
    >
      <Section title="Provider">
        <ChoiceCards
          label="Cloud"
          value={cloud}
          onChange={(next) => {
            if (!switching && next !== cloud) changeCloud(next);
          }}
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
          cloud === "aws"
            ? "The account is whichever one your AWS CLI signs in to. The checklist in step 6 shows it."
            : "Sign in to gcloud on this machine first. The checklist in step 6 checks both sign-ins."
        }
      >
        {cloud === "gcp" && <HowTo guide={GCP_SIGN_IN} label="How do I sign in?" />}
        {cloud === "aws" && (
          <div className="flex flex-col gap-2">
            <Field
              id="aws-profile"
              label="AWS profile (optional)"
              mono
              placeholder="default"
              hint="The profile in ~/.aws/config to use. Empty: the default credentials."
              value={awsProfile}
              error={error("aws_profile")}
              onChange={setAwsProfile}
            />
            <HowTo guide={AWS_SIGN_IN} label="How do I sign in?" />
          </div>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {fields.map((f) => (
            <div key={`${cloud}-${f.key}`} className="flex flex-col gap-2 min-w-0">
              <Field
                id={`tf-${f.key}`}
                label={f.label}
                hint={f.hint}
                placeholder={f.placeholder}
                mono={f.mono}
                value={values[f.key] || ""}
                error={error(`fields.${f.key}`)}
                onChange={(v) => setValues({ ...values, [f.key]: v })}
              />
              {f.guide && <HowTo guide={f.guide} />}
            </div>
          ))}
        </div>
      </Section>
      <Section title="Repo">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {REPO_FIELDS.map((f) => (
            <div key={f.key} className="flex flex-col gap-2 min-w-0">
              <Field
                id={`tf-${f.key}`}
                label={f.label}
                hint={f.hint}
                placeholder={f.placeholder}
                mono={f.mono}
                value={values[f.key] || ""}
                error={error(`fields.${f.key}`)}
                onChange={(v) => setValues({ ...values, [f.key]: v })}
              />
              {f.guide && <HowTo guide={f.guide} />}
            </div>
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
            hint="Mirrors the time zone in step 1. Change it there."
          />
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
        description="The public address every webhook points to: Slack, GitHub, Granola and Circleback."
      >
        <ReceiverNow />
        <HowTo guide={RECEIVER_GUIDE} />
        <Field
          id="receiver-url"
          label="Override (optional)"
          mono
          placeholder="https://nua-receiver-123456789.us-west1.run.app"
          hint="Paste a URL only to override the one above. Save to apply."
          value={receiverUrl}
          error={error("receiver_url")}
          onChange={setReceiverUrl}
        />
      </Section>
    </StepFrame>
  );
}

"use client";

import { useId, useState } from "react";
import { ChevronDown, Info } from "lucide-react";

import { cn } from "@/lib/cn";
import { List, ListItem } from "@/components/ui/list";
import { Text } from "@/components/ui/text";
import { CodeBlock, TextLink } from "@/components/setup/common";

export type HowToGuide = {
  /** Plan or role needed first, shown as a highlighted line. */
  requires?: string;
  steps: React.ReactNode[];
  note?: string;
  link?: { href: string; label: string };
};

function Code({ children }: { children: React.ReactNode }) {
  return (
    <Text size="xs" mono className="px-1 py-px rounded-xs bg-bg-inset">
      {children}
    </Text>
  );
}

/** The numbered steps of a guide, in a sunk panel. */
export function GuideSteps({ guide }: { guide: HowToGuide }) {
  return (
    <div className="flex flex-col gap-3 rounded-md border border-rule bg-bg-sunk px-4 py-3">
      {guide.requires && (
        <Text size="xs" weight="medium" className="self-start rounded-full bg-warn-soft text-warn-ink px-2 py-0.5">
          {guide.requires}
        </Text>
      )}
      <List as="ol" gap="md">
        {guide.steps.map((step, i) => (
          <ListItem key={`step-${i}`} align="start" gap="md">
            <Text size="sm" mono color="faint" className="w-5 flex-none text-right">
              {i + 1}.
            </Text>
            <div className="min-w-0 flex-1 font-sans text-sm text-ink-2 leading-relaxed">{step}</div>
          </ListItem>
        ))}
      </List>
      {guide.note && (
        <Text size="xs" color="faint" leading="relaxed">
          {guide.note}
        </Text>
      )}
      {guide.link && (
        <Text size="xs" color="faint">
          <TextLink href={guide.link.href}>{guide.link.label}</TextLink>
        </Text>
      )}
    </div>
  );
}

/** An info button that opens the vendor steps inline, under the field it explains. */
export function HowTo({ guide, label = "How do I get this?" }: { guide: HowToGuide; label?: string }) {
  const [open, setOpen] = useState(false);
  const panel = useId();
  return (
    <div className="flex flex-col">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panel}
        onClick={() => setOpen((v) => !v)}
        className="self-start inline-flex items-center gap-1.5 rounded-sm text-accent-ink cursor-pointer focus-visible:outline-none focus-visible:ring-nua"
      >
        <Info className="w-3.5 h-3.5" aria-hidden />
        <Text size="xs" weight="medium" color="accent">
          {label}
        </Text>
        <ChevronDown className={cn("w-3.5 h-3.5 transition-transform duration-200", open && "rotate-180")} aria-hidden />
      </button>
      <div
        id={panel}
        className={cn(
          "grid transition-[grid-template-rows,opacity] duration-300 ease-[cubic-bezier(0.23,1,0.32,1)] motion-reduce:transition-none",
          open && "grid-rows-[1fr] opacity-100",
          !open && "grid-rows-[0fr] opacity-0",
        )}
        inert={!open}
      >
        <div className="overflow-hidden min-h-0">
          <div className="mt-2">
            <GuideSteps guide={guide} />
          </div>
        </div>
      </div>
    </div>
  );
}

function Url({ url }: { url: string | null }) {
  if (!url) return <>the receiver URL plus this path, shown here once the receiver URL is known.</>;
  return (
    <div className="pt-1">
      <CodeBlock text={url} />
    </div>
  );
}

export const GRANOLA_KEY: HowToGuide = {
  requires: "Granola Business or Enterprise plan, workspace admin",
  steps: [
    "You need a Granola Business or Enterprise plan, and you must be a workspace admin.",
    "Open the Granola desktop app. Click your workspace name in the bottom-left corner, then Settings.",
    "Go to Connectors > Workspace API keys and click Create new key.",
    "Copy the key and paste it here.",
  ],
  note:
    "Use a workspace key, not a personal one: it never expires and is not tied to anyone's account. It reads the Team space, plus any space with Allow Granola API access on. That toggle is on by default for new spaces; turn it off on spaces that are not for discovery calls (open the space > Integrations > Granola API).",
  link: { href: "https://docs.granola.ai/help-center/sharing/integrations/granola-api", label: "Granola API docs" },
};

export function granolaWebhook(receiver: string | null): HowToGuide {
  return {
    requires: "Granola Business or Enterprise plan. Do this after the first deploy.",
    steps: [
      "In the Granola desktop app: Settings > Connectors > Webhooks > Set up a webhook.",
      <>
        Choose the Team space notes and the events <Code>note.generated</Code> and <Code>note.access_granted</Code>{" "}
        (<Code>note.edited</Code> is optional).
      </>,
      <>
        Endpoint URL: <Url url={receiver ? `${receiver}/ingest/granola` : null} />
      </>,
      <>
        Create the webhook and copy its signing secret (starts with <Code>whsec_</Code>). It is shown only once.
      </>,
    ],
  };
}

export const CIRCLEBACK_KEY: HowToGuide = {
  steps: [
    "Each person signs in to their own Circleback account.",
    <>
      Open <TextLink href="https://circleback.ai/settings?tab=api-access">Settings &gt; API keys</TextLink> and create a
      key.
    </>,
    "Copy it and paste it in their row here. Keys only see that person's meetings.",
  ],
  link: { href: "https://circleback.ai/docs/support/platform/circleback-api", label: "Circleback API docs" },
};

export function circlebackSecret(receiver: string | null, person: string): HowToGuide {
  return {
    requires: "Do this after the first deploy, signed in as this person.",
    steps: [
      <>
        In Circleback, create a tag named exactly <Code>discovery</Code> (only meetings with it are ingested).
      </>,
      <>
        Open Automations and select New automation. Add the condition: meeting has the tag <Code>discovery</Code>. Select
        Next.
      </>,
      <>
        Select Send webhook request. Enter the endpoint URL:{" "}
        <Url url={receiver ? `${receiver}/ingest/circleback/${person}` : null} />
      </>,
      <>
        In the meeting outcomes to include, make sure the transcript is included (the exact label may differ; if unsure,
        use Send test request and check the JSON has <Code>transcript</Code>).
      </>,
      "Copy the signing secret Circleback shows for the webhook and paste it here.",
      "Select Done, name the automation, and Save.",
    ],
    link: {
      href: "https://circleback.ai/docs/support/automations/export-meeting-data-with-webhooks",
      label: "Circleback webhook docs",
    },
  };
}

export const RECEIVER_GUIDE: HowToGuide = {
  steps: [
    "The receiver is the public address every webhook points to: Slack, GitHub, Granola and Circleback all send to it.",
    <>
      On Google Cloud it is known before the deploy: <Code>{"https://nua-receiver-<project number>.<region>.run.app"}</Code>.
      This page fills it in from your project id, using{" "}
      <Code>{"gcloud projects describe <project> --format='value(projectNumber)'"}</Code> for the number.
    </>,
    <>
      On AWS it exists only after the first deploy. Then{" "}
      <Code>terraform -chdir=infra/aws/terraform output -raw receiver_url</Code> prints it, and this page reads it the
      same way.
    </>,
    "Paste a URL below only to override what the page found.",
  ],
};

export const SLACK_TOKEN: HowToGuide = {
  steps: [
    <>
      Open your Slack app at <TextLink href="https://api.slack.com/apps">api.slack.com/apps</TextLink>.
    </>,
    "Go to OAuth & Permissions and select Install to Workspace (or Reinstall to Workspace), then Allow.",
    <>
      Under OAuth Tokens, copy the Bot User OAuth Token. It starts with <Code>xoxb-</Code>.
    </>,
  ],
  link: { href: "https://docs.slack.dev/app-management/quickstart-app-settings", label: "Slack app settings docs" },
};

export const SLACK_SECRET: HowToGuide = {
  steps: [
    <>
      Open your Slack app at <TextLink href="https://api.slack.com/apps">api.slack.com/apps</TextLink>.
    </>,
    "Go to Basic Information > App Credentials > Signing Secret, select Show, and copy it.",
  ],
  link: { href: "https://docs.slack.dev/authentication/verifying-requests-from-slack", label: "Slack signing secret docs" },
};

// ---------- step 3: cloud fields (checked against the GCP, AWS and GitHub docs) ----------

export const GCP_SIGN_IN: HowToGuide = {
  steps: [
    <>
      Install the <TextLink href="https://cloud.google.com/sdk/docs/install">gcloud CLI</TextLink>.
    </>,
    <>
      Run <Code>gcloud auth login</Code>, then <Code>gcloud auth application-default login</Code>. Terraform uses the
      second one.
    </>,
    "Both sign-ins expire. When the checklist in step 6 flags one, run that command again.",
  ],
};

export const GCP_PROJECT: HowToGuide = {
  steps: [
    <>
      Open the <TextLink href="https://console.cloud.google.com/projectcreate">new project page</TextLink>{" "}in the Google
      Cloud console (or Manage resources &gt; Create project). Or skip this: the deploy command can create it, with
      &quot;Create the project and link billing&quot; on in step 6.
    </>,
    "Give it a name. The project ID is generated from it; you can edit it while creating the project. 6 to 30 characters: lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen. It is permanent and cannot be reused.",
    <>
      Already have one: run <Code>gcloud projects list</Code> and copy the PROJECT_ID column.
    </>,
  ],
  link: {
    href: "https://cloud.google.com/resource-manager/docs/creating-managing-projects",
    label: "Creating and managing projects",
  },
};

export const GCP_REGION: HowToGuide = {
  steps: [
    <>
      Pick the Cloud Run region closest to your team, e.g. <Code>us-west1</Code> (Oregon), <Code>us-central1</Code>{" "}
      (Iowa), <Code>us-east1</Code> (South Carolina), <Code>europe-west2</Code> (London), <Code>europe-west1</Code>{" "}
      (Belgium), <Code>asia-northeast1</Code> (Tokyo).
    </>,
    "It is part of the receiver URL, so changing it later means a redeploy and new webhook URLs everywhere.",
  ],
  link: { href: "https://cloud.google.com/run/docs/locations", label: "Cloud Run locations" },
};

export const GCP_BILLING: HowToGuide = {
  steps: [
    <>
      Open <TextLink href="https://console.cloud.google.com/billing">Billing</TextLink> in the console. The My billing
      accounts list has a Billing account ID column.
    </>,
    <>
      Copy the ID: 18 characters like <Code>010101-F0FFF0-10XX01</Code>. Or run <Code>gcloud billing accounts list</Code>{" "}
      and copy the ACCOUNT_ID column.
    </>,
    "Why: Cloud Run needs billing on the project, the deploy command links it when it creates a new project, and the budget alert is set on it.",
  ],
  link: { href: "https://cloud.google.com/billing/docs/how-to/find-billing-account-id", label: "Find your billing account ID" },
};

export const GCP_BUDGET: HowToGuide = {
  steps: [
    "A monthly budget on this project's spend. Google emails the billing account's admins and users at 50%, 90% and 100% of it, and when spend is forecast to pass 100%.",
    "It only emails. It never stops or caps anything.",
    "It needs the billing account above; without one there is no alert. Infra is a few dollars a month; model calls are capped per task in config/pipeline.yml.",
  ],
};

export const AWS_SIGN_IN: HowToGuide = {
  steps: [
    <>
      IAM Identity Center (SSO): in a terminal run <Code>aws configure sso</Code>. It asks for a session name, your
      access portal start URL (the <Code>https://...awsapps.com/start</Code> link you sign in with), the SSO region (the
      Region your IAM Identity Center is in; the access portal shows it under Access keys) and the registration scope
      (keep the default).
    </>,
    "Sign in in the browser, pick the account and an admin role such as AdministratorAccess, then name the profile. Enter that name in the AWS profile field.",
    <>
      When it expires: <Code>{"aws sso login --profile <name>"}</Code>.
    </>,
    <>
      Access keys instead: in the AWS console choose your user name (top right) &gt; Security credentials &gt; Create
      access key, then run <Code>{"aws configure --profile <name>"}</Code> and paste the key and secret.
    </>,
    <>
      Check it: <Code>{"aws sts get-caller-identity --profile <name>"}</Code>. The deploy needs admin rights: it creates
      IAM roles, Lambda, ECS, SQS, Secrets Manager, CodeBuild and S3 resources.
    </>,
  ],
  link: { href: "https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html", label: "AWS CLI and IAM Identity Center" },
};

export const AWS_REGION: HowToGuide = {
  steps: [
    <>
      Pick the region closest to your team, e.g. <Code>us-west-2</Code> (Oregon), <Code>us-east-1</Code> (N. Virginia),{" "}
      <Code>us-east-2</Code> (Ohio), <Code>eu-west-1</Code> (Ireland), <Code>eu-west-2</Code> (London),{" "}
      <Code>ap-northeast-1</Code> (Tokyo).
    </>,
    "Changing it later means a teardown and redeploy, and a new receiver URL.",
  ],
};

export const AWS_ARCH: HowToGuide = {
  steps: [
    <>
      <Code>x86_64</Code> (default) or <Code>arm64</Code>. arm64 is about 20% cheaper per second on Lambda and Fargate.
    </>,
    "Pick before the first build; switching later means a teardown.",
  ],
};

export const AWS_BUDGET: HowToGuide = {
  steps: [
    "A monthly cost budget. AWS emails these addresses when actual spend passes 50%, 90% and 100% of it, and when forecast spend passes 100%. Anyone can receive it.",
    "It covers the whole AWS account, not just this stack. It only emails; it never stops anything. Empty: no alert.",
  ],
};

export const REPO_GUIDE: HowToGuide = {
  steps: [
    <>
      On GitHub open the <TextLink href="https://github.com/TryNUA/nua-discovery-oss">Nua Discovery repo</TextLink>{" "}and
      choose Use this template &gt; Create a new repository.
    </>,
    "Pick the owner, a name, and Private. Private is required: it will hold transcripts of real conversations. Then Create repository from template.",
    "Enter it here as owner/name. Branch: main unless you changed it.",
  ],
};

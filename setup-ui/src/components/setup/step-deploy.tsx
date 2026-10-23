"use client";

import { useState } from "react";
import { CircleCheck, CircleDashed, RefreshCw } from "lucide-react";

import { useApi, type Check, type Checks } from "@/lib/api";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { List, ListItem } from "@/components/ui/list";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Text } from "@/components/ui/text";
import { CodeBlock, Note, Section } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";

function CheckRow({ check }: { check: Check }) {
  return (
    <ListItem align="start" gap="lg" className="py-2.5 border-b border-rule-2 last:border-b-0">
      {check.ok && <CircleCheck className="w-4 h-4 mt-0.5 flex-none text-success" aria-label="ok" />}
      {!check.ok && <CircleDashed className="w-4 h-4 mt-0.5 flex-none text-ink-4" aria-label="missing" />}
      <div className="flex flex-col gap-0.5 min-w-0">
        <Text size="sm" weight="medium" className={cn(!check.ok && "text-ink-2")}>
          {check.label}
        </Text>
        {check.detail && (
          <Text size="xs" color="faint" mono className="break-all">
            {check.detail}
          </Text>
        )}
        {!check.ok && check.fix && (
          <Text size="xs" color="muted" leading="relaxed">
            {check.fix}
          </Text>
        )}
      </div>
    </ListItem>
  );
}

export function Checklist({ data, isValidating, onRefresh }: { data?: Checks; isValidating: boolean; onRefresh: () => void }) {
  const done = data ? data.checks.filter((c) => c.ok).length : 0;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3">
        {data && (
          <Text size="sm" color="muted">
            {done} of {data.checks.length} ready
          </Text>
        )}
        <Button variant="secondary" size="sm" onClick={onRefresh} loading={isValidating && Boolean(data)} className="ml-auto">
          <RefreshCw className="w-3.5 h-3.5" />
          Check again
        </Button>
      </div>
      {!data && (
        <div className="flex items-center gap-2 py-4">
          <Spinner size="sm" />
          <Text size="sm" color="faint">
            Checking tools, logins, files and the receiver...
          </Text>
        </div>
      )}
      {data && (
        <List gap="none" className="rounded-md border border-rule bg-bg-raised shadow-xs px-4">
          {data.checks.map((c) => (
            <CheckRow key={c.id} check={c} />
          ))}
        </List>
      )}
    </div>
  );
}

export function StepDeploy({ nav }: { nav: StepNav }) {
  const { data, mutate, isValidating } = useApi<Checks>("checks");
  const [create, setCreate] = useState(false);
  const gcp = data?.cloud !== "aws";
  const command = create && data?.deploy.command_create ? data.deploy.command_create : data?.deploy.command;

  return (
    <StepFrame
      nav={nav}
      title="Deploy"
      intro="Run one command in a terminal at the repo root. It checks first and skips what is done, so run it again after any fix."
    >
      <Section
        title="The command"
        description="It shows each terraform plan and asks before applying. This page does not run it."
      >
        {command && <CodeBlock text={command} />}
        {gcp && data?.deploy.command_create && (
          <div className="flex items-center gap-3">
            <Switch id="create-project" checked={create} onCheckedChange={setCreate} />
            <label htmlFor="create-project" className="cursor-pointer">
              <Text size="sm">Create the project and link billing (first run, new project only)</Text>
            </label>
          </div>
        )}
        {data?.deploy.note && <Note title="Heads up" description={data.deploy.note} variant="warn" />}
        <Text size="sm" color="muted" leading="relaxed">
          {gcp &&
            "The first run creates the state bucket, runs terraform init and prints the receiver URL. It stops if the GitHub App or keys are missing; fill them in and run it again. A full run applies stage 1, pushes the secrets from .env, builds the image, applies stage 2 and checks the receiver."}
          {!gcp &&
            "The first run creates the state bucket, applies stage 1, builds the image with CodeBuild and applies stage 2, then prints the receiver URL. Then create the GitHub and Slack apps (steps 6 and 7) and run it again to push their secrets."}
        </Text>
        <Text size="xs" color="faint">
          Add --dry-run to print every command without running any.{" "}
          {data?.deploy.doc && `Manual reference: ${data.deploy.doc}.`}
        </Text>
      </Section>
      <Section title="Checklist" description="Live from your machine. Nothing here changes your cloud.">
        <Checklist data={data} isValidating={isValidating} onRefresh={() => mutate()} />
      </Section>
    </StepFrame>
  );
}

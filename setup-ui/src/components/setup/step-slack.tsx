"use client";

import { useState } from "react";

import { api, useApi, type EnvStatus, type SlackInfo } from "@/lib/api";
import { List, ListItem } from "@/components/ui/list";
import { Spinner } from "@/components/ui/spinner";
import { Text } from "@/components/ui/text";
import { LinkButton, Note, Section, SecretField, useSave } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";

function Steps({ items }: { items: React.ReactNode[] }) {
  return (
    <List as="ol" gap="md">
      {items.map((item, i) => (
        <ListItem key={`s-${i}`} align="start" gap="md">
          <Text size="sm" mono color="faint" className="w-5 flex-none text-right">
            {i + 1}.
          </Text>
          <Text size="sm" color="muted" leading="relaxed">
            {item}
          </Text>
        </ListItem>
      ))}
    </List>
  );
}

export function StepSlack({ env, nav, onSaved }: { env: EnvStatus; nav: StepNav; onSaved: () => void }) {
  const { data, isLoading } = useApi<SlackInfo>("slack");
  const [values, setValues] = useState<Record<string, string>>({});
  const save = useSave();

  function submit() {
    save
      .run(
        () => api<EnvStatus & { written: string[] }>("env", "PUT", { values }),
        (r) => (r.written.length ? `Wrote ${r.written.join(", ")} to .env (values not shown)` : "Nothing new to write."),
      )
      .then((r) => {
        if (r) {
          setValues({});
          onSaved();
        }
      });
  }

  return (
    <StepFrame
      nav={nav}
      title="Slack app"
      intro="Briefs, buttons, roll-ups and the bot you can ask. Slack opens with the app filled in: pick the workspace, Create, then Install to Workspace."
      onSave={submit}
      saving={save.saving}
      problems={save.problems}
      saveLabel="Save keys"
    >
      <Section title="Create the app">
        {isLoading && (
          <div className="flex items-center gap-2">
            <Spinner size="sm" />
            <Text size="sm" color="faint">
              Looking up the receiver URL...
            </Text>
          </div>
        )}
        {data && !data.create_url && <Note title="No receiver URL yet" description={data.how_to} variant="warn" />}
        {data?.create_url && (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap gap-2">
              <LinkButton href={data.create_url} variant="primary">
                Create the Slack app
              </LinkButton>
              {data.create_url_no_events && (
                <LinkButton href={data.create_url_no_events} variant="outline">
                  Without events
                </LinkButton>
              )}
            </div>
            <Text size="xs" color="faint" leading="relaxed">
              Bot name {data.bot_name}, requests to{" "}
              <Text size="xs" mono color="faint">
                {data.receiver_url}
              </Text>
              . If Slack refuses the events URL because the receiver is not deployed yet, use Without events and turn
              Event Subscriptions on after step 9.
            </Text>
          </div>
        )}
      </Section>
      <Section title="Then">
        <Steps
          items={[
            "Install to Workspace.",
            "Copy OAuth & Permissions > Bot User OAuth Token (xoxb-...) below.",
            "Copy Basic Information > App Credentials > Signing Secret below.",
            `Create ${data?.channels.calls || "#discovery-calls"}, ${data?.channels.ops || "#discovery-ops"} and ${
              data?.channels.test || "#discovery-test"
            }, and invite @${data?.bot_name || "the bot"} to each.`,
          ]}
        />
      </Section>
      <Section title="Keys" description="Written to .env only. They reach the cloud through the deploy command, never tfvars.">
        <div className="flex flex-col gap-4">
          <SecretField
            name="SLACK_BOT_TOKEN"
            label="Bot User OAuth Token"
            required
            isSet={env.keys.SLACK_BOT_TOKEN}
            value={values.SLACK_BOT_TOKEN || ""}
            onChange={(v) => setValues({ ...values, SLACK_BOT_TOKEN: v })}
            hint="starts with xoxb-"
          />
          <SecretField
            name="SLACK_SIGNING_SECRET"
            label="Signing Secret"
            required
            isSet={env.keys.SLACK_SIGNING_SECRET}
            value={values.SLACK_SIGNING_SECRET || ""}
            onChange={(v) => setValues({ ...values, SLACK_SIGNING_SECRET: v })}
          />
        </div>
      </Section>
    </StepFrame>
  );
}

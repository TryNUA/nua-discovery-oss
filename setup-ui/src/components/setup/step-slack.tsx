"use client";

import { api, useApi, type EnvStatus, type Pipeline, type SetupConfig, type SlackInfo } from "@/lib/api";
import { List, ListItem } from "@/components/ui/list";
import { Spinner } from "@/components/ui/spinner";
import { Text } from "@/components/ui/text";
import { Field, LinkButton, Note, Section, SecretField } from "@/components/setup/common";
import { useDraft, useFlush, useProblems, useQuietSet, useSaver, type FieldProblem } from "@/components/setup/store";
import { StepFrame, type StepNav } from "@/components/setup/frame";
import { HowTo, SLACK_SECRET, SLACK_TOKEN } from "@/components/setup/howto";

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

export function StepSlack({ config, env, nav }: { config: SetupConfig; env: EnvStatus; nav: StepNav }) {
  const step = "slack" as const;
  const { data, isLoading, mutate } = useApi<SlackInfo>("slack");
  const [values, setValues] = useDraft<Record<string, string>>(step, "secret:values", () => ({}), { secret: true });
  const [botName, setBotName] = useDraft(step, "botName", () => config.domain.bot_name);
  const [channels, setChannels] = useDraft<Pipeline["channels"]>(step, "channels", () => config.pipeline.channels);
  const quiet = useQuietSet(step);
  const flush = useFlush(step);
  const { error } = useProblems(step);
  const unsavedName = botName.trim() !== config.domain.bot_name;
  // staging (channels.test) is a power-user option set by hand, so it is not part of this list
  const invited = [channels.calls, channels.ops].filter((c) => c && c.trim());

  useSaver(step, () => {
    const body = {
      domain: { bot_name: botName.trim() },
      pipeline: { channels: { calls: channels.calls.trim(), ops: channels.ops.trim() } },
    };
    const secrets = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim()));
    return api<{ problems: FieldProblem[] }>("config", "PUT", body).then((saved) =>
      api<EnvStatus & { written: string[]; problems: FieldProblem[] }>("env", "PUT", { values: secrets }).then((e) => {
        if (e.written.length) {
          quiet("secret:values", Object.fromEntries(Object.entries(values).filter(([k]) => !e.written.includes(k))));
        }
        mutate();
        return [...(saved.problems || []), ...e.problems];
      }),
    );
  });

  return (
    <StepFrame
      nav={nav}
      title="Slack app"
      intro="Briefs, buttons, roll-ups and the bot you can ask. Slack opens with the app filled in: pick the workspace, Create, then Install to Workspace."
      
    >
      <Section title="Bot and channels" description="Create these two channels in Slack. Lowercase, no spaces.">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <Field
            id="bot-name"
            label="Bot name"
            placeholder="Nua Discovery"
            hint="How people @mention it. Empty means Nua Discovery."
            value={botName}
            error={error("domain.bot_name")}
            onChange={setBotName}
          />
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <Field
            id="channel-calls"
            label="Briefs"
            mono
            value={channels.calls}
            error={error("pipeline.channels.calls")}
            onChange={(v) => setChannels({ ...channels, calls: v })}
          />
          <Field
            id="channel-ops"
            label="Errors and re-runs"
            mono
            value={channels.ops}
            error={error("pipeline.channels.ops")}
            onChange={(v) => setChannels({ ...channels, ops: v })}
          />
        </div>
      </Section>
      <Section title="Create the app">
        {unsavedName && <Note title="Saving the new bot name first, so the app gets it." variant="default" />}
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
              Event Subscriptions on after step 6.
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
            `Invite @${data?.bot_name || "the bot"} to ${invited.join(", ").replace(/, ([^,]*)$/, " and $1")}.`,
          ]}
        />
      </Section>
      <Section title="Keys" description="Written to .env only. They reach the cloud through the deploy command, never tfvars.">
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
          <SecretField
            name="SLACK_BOT_TOKEN"
            label="Bot User OAuth Token"
            required
            isSet={env.keys.SLACK_BOT_TOKEN}
            value={values.SLACK_BOT_TOKEN || ""}
            error={error("env.SLACK_BOT_TOKEN")}
            onBlur={flush}
            onChange={(v) => setValues({ ...values, SLACK_BOT_TOKEN: v })}
            hint="starts with xoxb-"
          />
          <HowTo guide={SLACK_TOKEN} />
          </div>
          <div className="flex flex-col gap-2">
          <SecretField
            name="SLACK_SIGNING_SECRET"
            label="Signing Secret"
            required
            isSet={env.keys.SLACK_SIGNING_SECRET}
            value={values.SLACK_SIGNING_SECRET || ""}
            error={error("env.SLACK_SIGNING_SECRET")}
            onBlur={flush}
            onChange={(v) => setValues({ ...values, SLACK_SIGNING_SECRET: v })}
          />
          <HowTo guide={SLACK_SECRET} />
          </div>
        </div>
      </Section>
    </StepFrame>
  );
}

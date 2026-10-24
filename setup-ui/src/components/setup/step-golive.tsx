"use client";

import { api, type EnvStatus, type GoLive } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Text } from "@/components/ui/text";
import { CodeBlock, LinkButton, Note, Section, SecretField } from "@/components/setup/common";
import { useDraft, useFlush, useProblems, useQuietSet, useSaver, type FieldProblem } from "@/components/setup/store";
import { StepFrame, type StepNav } from "@/components/setup/frame";
import { personVar } from "@/components/setup/step-recorder";
import { circlebackSecret, granolaWebhook, GuideSteps } from "@/components/setup/howto";
import { CollapsibleSection } from "@/components/ui/collapsible";

/** "GMT-08:00" style offset of `zone` at instant `ms`, as minutes east of UTC. */
function offsetMinutes(zone: string, ms: number): number {
  const part = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "longOffset" })
    .formatToParts(new Date(ms))
    .find((p) => p.type === "timeZoneName")?.value;
  const m = /GMT([+-])(\d{2}):?(\d{2})?/.exec(part || "");
  if (!m) return 0;
  return (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3] || 0));
}

/** A wall-clock "YYYY-MM-DDTHH:mm" in `zone` as an ISO time with its offset. */
export function toIsoWithOffset(local: string, zone: string): string {
  const [d, t] = local.split("T");
  const [y, mo, da] = d.split("-").map(Number);
  const [h, mi] = (t || "00:00").split(":").map(Number);
  const guess = Date.UTC(y, mo - 1, da, h, mi);
  const offset = offsetMinutes(zone, guess - offsetMinutes(zone, guess) * 60000);
  const sign = offset < 0 ? "-" : "+";
  const abs = Math.abs(offset);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d}T${pad(h)}:${pad(mi)}:00${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

function nowIn(zone: string): string {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: zone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(new Date())
      .map((p) => [p.type, p.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function toLocalInput(cutoff: string | null): string {
  if (!cutoff) return "";
  const m = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?/.exec(cutoff);
  if (!m) return "";
  return `${m[1]}T${m[2] || "00:00"}`;
}

function Recorders({
  live,
  env,
  values,
  setValues,
  flush,
  error,
}: {
  live: GoLive;
  env: EnvStatus;
  values: Record<string, string>;
  setValues: (v: Record<string, string>) => void;
  flush: () => void;
  error: (field: string) => string | undefined;
}) {
  const granola = live.recorder === "granola" || live.recorder === "both";
  const circleback = live.recorder === "circleback" || live.recorder === "both";
  if (!live.receiver_url) {
    return (
      <div className="flex flex-col gap-2">
        <Note
          title="Waiting for the receiver URL"
          description={
            live.cloud === "aws"
              ? "On AWS it appears after the first deploy (step 6). The webhook steps show up here then."
              : "On Google Cloud it comes from your project id and region in step 3 (with gcloud logged in). The webhook steps show up here then."
          }
          variant="warn"
        />
        <div>
          <LinkButton href="#cloud" external={false} variant="outline">
            Go to step 3
          </LinkButton>
        </div>
        <Text size="xs" color="faint">
          The go-live time and schedules below work without it.
        </Text>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-6">
      {granola && (
        <div className="flex flex-col gap-3">
          <Text size="sm" weight="semibold">
            Granola
          </Text>
          <GuideSteps guide={granolaWebhook(live.receiver_url)} />
          <SecretField
            name="GRANOLA_WEBHOOK_SECRET"
            label="Webhook signing secret (whsec_...)"
            required
            isSet={env.keys.GRANOLA_WEBHOOK_SECRET}
            value={values.GRANOLA_WEBHOOK_SECRET || ""}
            error={error("env.GRANOLA_WEBHOOK_SECRET")}
            onBlur={flush}
            onChange={(v) => setValues({ ...values, GRANOLA_WEBHOOK_SECRET: v })}
          />
          <CollapsibleSection title="Or create it with the API" titleSize="sm">
            <div className="flex flex-col gap-2">
              <Text size="sm" color="muted" leading="relaxed">
                Run this in a terminal with GRANOLA_API_KEY set (or paste the key in place of $GRANOLA_API_KEY). The
                signing secret is in the response, shown once.
              </Text>
              <CodeBlock text={live.granola_curl || ""} />
            </div>
          </CollapsibleSection>
        </div>
      )}
      {circleback && (
        <div className="flex flex-col gap-3">
          <Text size="sm" weight="semibold">
            Circleback, once per person
          </Text>
          {live.circleback_people.map((p) => (
            <div key={p} className="flex flex-col gap-3 rounded-md border border-rule bg-bg-raised shadow-xs p-4">
              <Text size="sm" weight="medium">
                {p}
              </Text>
              <GuideSteps guide={circlebackSecret(live.receiver_url, p)} />
              <SecretField
                name={personVar("CIRCLEBACK_SIGNING_SECRET_", p)}
                label={`Signing secret for ${p}`}
                required
                isSet={Boolean(env.keys[personVar("CIRCLEBACK_SIGNING_SECRET_", p)])}
                value={values[personVar("CIRCLEBACK_SIGNING_SECRET_", p)] || ""}
                error={error(`env.${personVar("CIRCLEBACK_SIGNING_SECRET_", p)}`)}
                onBlur={flush}
                onChange={(v) => setValues({ ...values, [personVar("CIRCLEBACK_SIGNING_SECRET_", p)]: v })}
              />
            </div>
          ))}
          {!live.circleback_people.length && (
            <Note title="No one is set to record on Circleback" description="Pick people in step 2." variant="warn" />
          )}
        </div>
      )}
      {!live.recorder && <Note title="Pick a recorder in step 2 first." variant="warn" />}
      {live.recorder && (
        <div className="flex flex-col gap-3">
          <Text size="sm" color="muted" leading="relaxed">
            Then run the deploy command (step 6) again: it pushes the new secrets and restarts the services so they read
            them.
          </Text>
        </div>
      )}
    </div>
  );
}

export function StepGoLive({ live, env, nav }: { live: GoLive; env: EnvStatus; nav: StepNav }) {
  const step = "golive" as const;
  const [cutoff, setCutoff] = useDraft(step, "cutoff", () => toLocalInput(live.ingest_cutoff));
  const [schedules, setSchedules] = useDraft(step, "schedules", () => live.schedules_on);
  const [values, setValues] = useDraft<Record<string, string>>(step, "secret:values", () => ({}), { secret: true });
  const quiet = useQuietSet(step);
  const flush = useFlush(step);
  const { error } = useProblems(step);

  useSaver(step, () => {
    const body = { ingest_cutoff: cutoff ? toIsoWithOffset(cutoff, live.timezone) : null, schedules_on: schedules };
    const secrets = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim()));
    return api<GoLive & { problems: FieldProblem[] }>("golive", "PUT", body).then((g) =>
      api<EnvStatus & { written: string[]; problems: FieldProblem[] }>("env", "PUT", { values: secrets }).then((e) => {
        if (e.written.length) {
          quiet("secret:values", Object.fromEntries(Object.entries(values).filter(([k]) => !e.written.includes(k))));
        }
        return [...(g.problems || []), ...e.problems];
      }),
    );
  });

  return (
    <StepFrame
      nav={nav}
      title="Go live"
      intro="Connect your recorder, pick the moment calls start counting, and switch the schedules on."
    >
      <Section title="Recorder webhooks" description="Do this after the first full deploy, when the receiver answers.">
        <Recorders live={live} env={env} values={values} setValues={setValues} flush={flush} error={error} />
      </Section>
      <Section
        title="Ingest cutoff"
        description="Recorder items from before this time are ignored. While it is empty, every recorder webhook is acknowledged and ignored."
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="cutoff" className="text-ink-2">
            Go-live time ({live.timezone})
          </Label>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              id="cutoff"
              type="datetime-local"
              value={cutoff}
              onChange={(e) => setCutoff(e.target.value)}
              className="w-auto"
            />
            <Button variant="secondary" size="sm" type="button" onClick={() => setCutoff(nowIn(live.timezone))}>
              Now
            </Button>
            {cutoff && (
              <Button variant="ghost" size="sm" type="button" onClick={() => setCutoff("")}>
                Clear
              </Button>
            )}
          </div>
          {error("pipeline.ingest_cutoff") && (
            <Text size="xs" color="danger">
              {error("pipeline.ingest_cutoff")}
            </Text>
          )}
          {cutoff && (
            <Text size="xs" color="faint" mono>
              ingest_cutoff: {toIsoWithOffset(cutoff, live.timezone)}
            </Text>
          )}
          <Text size="xs" color="faint">
            Saved in config/pipeline.yml. Commit and push it: config changes need no deploy.
          </Text>
        </div>
      </Section>
      <Section
        title="Schedules"
        description="The nightly sweep, the hourly Granola sweep and the Monday roll-up. Leave them off until the steps above are done."
      >
        <div className="flex items-center gap-3">
          <Switch id="schedules" checked={schedules} onCheckedChange={setSchedules} />
          <label htmlFor="schedules" className="cursor-pointer">
            <Text size="sm">schedules_on in terraform.tfvars</Text>
          </label>
        </div>
        <Text size="xs" color="faint" leading="relaxed">
          Takes effect when you run the deploy command again; its plan should show only the three jobs going from paused to
          running.
        </Text>
      </Section>
      <Section title="After that">
        <Text size="sm" color="muted" leading="relaxed">
          Backfill past calls by pushing their exports to transcripts/, then run the bootstrap roll-up to get your first
          proposed beliefs. docs/self-hosting.md, step 10, has the details.
        </Text>
      </Section>
    </StepFrame>
  );
}

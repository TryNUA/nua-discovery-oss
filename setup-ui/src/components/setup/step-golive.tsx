"use client";

import { useState } from "react";

import { api, useApi, type EnvStatus, type GoLive } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { List, ListItem } from "@/components/ui/list";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Text } from "@/components/ui/text";
import { CodeBlock, Note, Section, SecretField, useSave } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";
import { personVar } from "@/components/setup/step-recorder";

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

function Numbered({ items }: { items: React.ReactNode[] }) {
  return (
    <List as="ol" gap="md">
      {items.map((item, i) => (
        <ListItem key={`n-${i}`} align="start" gap="md">
          <Text size="sm" mono color="faint" className="w-5 flex-none text-right">
            {i + 1}.
          </Text>
          <div className="flex flex-col gap-2 min-w-0 flex-1">{item}</div>
        </ListItem>
      ))}
    </List>
  );
}

function Recorders({ live, env, onSaved }: { live: GoLive; env: EnvStatus; onSaved: () => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const save = useSave();
  const granola = live.recorder === "granola" || live.recorder === "both";
  const circleback = live.recorder === "circleback" || live.recorder === "both";
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
  if (!live.receiver_url) {
    return <Note title="No receiver URL yet" description="Deploy first (step 9). The webhook URLs show up here then." variant="warn" />;
  }
  return (
    <div className="flex flex-col gap-6">
      {granola && (
        <div className="flex flex-col gap-3">
          <Text size="sm" weight="semibold">
            Granola
          </Text>
          <Numbered
            items={[
              <Text key="a" size="sm" color="muted" leading="relaxed">
                Create the webhook with the workspace key. Run this in a terminal with GRANOLA_API_KEY set (or paste the key in
                place of $GRANOLA_API_KEY). The signing secret is shown once in the response.
              </Text>,
              <CodeBlock key="b" text={live.granola_curl || ""} />,
              <SecretField
                key="c"
                name="GRANOLA_WEBHOOK_SECRET"
                label="Webhook signing secret (whsec_...)"
                required
                isSet={env.keys.GRANOLA_WEBHOOK_SECRET}
                value={values.GRANOLA_WEBHOOK_SECRET || ""}
                onChange={(v) => setValues({ ...values, GRANOLA_WEBHOOK_SECRET: v })}
              />,
            ]}
          />
        </div>
      )}
      {circleback && (
        <div className="flex flex-col gap-3">
          <Text size="sm" weight="semibold">
            Circleback, once per person
          </Text>
          <Text size="sm" color="muted" leading="relaxed">
            Signed in to their own account: make a tag called exactly discovery, then Automations, New automation. Condition:
            has the tag discovery. Action: Send webhook request to their URL below, with the transcript, notes and attendees.
            Copy the signing secret it shows.
          </Text>
          {live.circleback_people.map((p) => (
            <div key={p} className="flex flex-col gap-3 rounded-md border border-rule bg-bg-raised shadow-xs p-4">
              <CodeBlock label={`${p}: endpoint URL`} text={live.circleback_urls?.[p] || ""} />
              <SecretField
                name={personVar("CIRCLEBACK_SIGNING_SECRET_", p)}
                label={`Signing secret for ${p}`}
                required
                isSet={Boolean(env.keys[personVar("CIRCLEBACK_SIGNING_SECRET_", p)])}
                value={values[personVar("CIRCLEBACK_SIGNING_SECRET_", p)] || ""}
                onChange={(v) => setValues({ ...values, [personVar("CIRCLEBACK_SIGNING_SECRET_", p)]: v })}
              />
            </div>
          ))}
          {!live.circleback_people.length && (
            <Note title="No one is set to record on Circleback" description="Pick people in step 4." variant="warn" />
          )}
        </div>
      )}
      {!live.recorder && <Note title="Pick a recorder in step 4 first." variant="warn" />}
      {live.recorder && (
        <div className="flex flex-col gap-3">
          <div>
            <Button onClick={submit} loading={save.saving} variant="secondary">
              Save secrets
            </Button>
          </div>
          {save.problems.map((p) => (
            <Text key={p} size="sm" color="danger">
              {p}
            </Text>
          ))}
          <Text size="sm" color="muted" leading="relaxed">
            Then run the deploy command (step 9) again: it pushes the new secrets and restarts the services so they read
            them.
          </Text>
        </div>
      )}
    </div>
  );
}

export function StepGoLive({ env, nav, onSaved }: { env: EnvStatus; nav: StepNav; onSaved: () => void }) {
  const { data, mutate } = useApi<GoLive>("golive");
  if (!data) {
    return (
      <StepFrame nav={nav} title="Go live" intro="Loading...">
        <div className="py-8">
          <Spinner />
        </div>
      </StepFrame>
    );
  }
  return (
    <GoLiveForm
      key={`${data.ingest_cutoff}-${data.schedules_on}`}
      live={data}
      env={env}
      nav={nav}
      onSaved={() => {
        mutate();
        onSaved();
      }}
    />
  );
}

function GoLiveForm({ live, env, nav, onSaved }: { live: GoLive; env: EnvStatus; nav: StepNav; onSaved: () => void }) {
  const [cutoff, setCutoff] = useState(toLocalInput(live.ingest_cutoff));
  const [schedules, setSchedules] = useState(live.schedules_on);
  const save = useSave();

  function submit() {
    const body = { ingest_cutoff: cutoff ? toIsoWithOffset(cutoff, live.timezone) : null, schedules_on: schedules };
    save
      .run(
        () => api<GoLive & { changed: string[] }>("golive", "PUT", body),
        (r) => (r.changed.length ? `Wrote ${r.changed.join(", ")}` : "Nothing changed on disk."),
      )
      .then((r) => {
        if (r) onSaved();
      });
  }

  return (
    <StepFrame
      nav={nav}
      title="Go live"
      intro="Connect your recorder, pick the moment calls start counting, and switch the schedules on."
      onSave={submit}
      saving={save.saving}
      problems={save.problems}
      saveLabel="Save go-live settings"
    >
      <Section title="Recorder webhooks" description="Do this after the first full deploy, when the receiver answers.">
        <Recorders live={live} env={env} onSaved={onSaved} />
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

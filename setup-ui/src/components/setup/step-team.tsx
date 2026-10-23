"use client";

import { useState } from "react";
import { ChevronDown, Plus, X } from "lucide-react";

import { api, useApi, type Person, type Pipeline, type SetupConfig } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Text } from "@/components/ui/text";
import { changedText, Field, Section, useSave } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";

type Saved = { changed: string[]; notes?: string[]; setup: SetupConfig };
type Row = { key: string; person: Person };

const EMPTY_PERSON: Person = { name: "", email: "", slack_id: "", github_login: "", circleback_key_name: null };

const COLUMNS: { field: keyof Person; label: string; placeholder: string }[] = [
  { field: "name", label: "Name", placeholder: "Alex Rivera" },
  { field: "email", label: "Email their calls are recorded under", placeholder: "alex@acme.dev" },
  { field: "slack_id", label: "Slack member id", placeholder: "U0123ABCD" },
  { field: "github_login", label: "GitHub login", placeholder: "alexr" },
];

export function TimezoneSelect({ value, onChange, id }: { value: string; onChange: (v: string) => void; id: string }) {
  const { data } = useApi<string[]>("timezones");
  const items = (data || []).map((z) => ({ id: z, label: z.replace(/_/g, " ") }));
  return (
    <Combobox
      items={items}
      onSelect={onChange}
      placeholder="Search, e.g. New York"
      emptyText="No such time zone."
      className="w-80"
      trigger={
        <Button id={id} type="button" variant="secondary" className="justify-between w-full sm:w-80 font-normal">
          <Text size="sm">{value || "Pick a time zone"}</Text>
          <ChevronDown className="w-3.5 h-3.5 text-ink-3" />
        </Button>
      }
    />
  );
}

function PeopleEditor({ rows, onChange }: { rows: Row[]; onChange: (rows: Row[]) => void }) {
  function setField(i: number, field: keyof Person, value: string) {
    onChange(rows.map((r, j) => (j === i ? { ...r, person: { ...r.person, [field]: value } } : r)));
  }
  return (
    <div className="flex flex-col gap-3">
      {rows.map((row, i) => (
        <div key={`person-${i}`} className="rounded-md border border-rule bg-bg-raised shadow-xs p-3 flex flex-col gap-3">
          <div className="flex items-end gap-2">
            <div className="flex flex-col gap-1.5 w-40">
              <Label htmlFor={`person-key-${i}`} className="text-ink-2">
                Key
              </Label>
              <Input
                id={`person-key-${i}`}
                value={row.key}
                placeholder="alex"
                className="font-mono"
                onChange={(e) => onChange(rows.map((r, j) => (j === i ? { ...r, key: e.target.value.toLowerCase() } : r)))}
              />
            </div>
            <Text size="xs" color="faint" className="pb-2.5 hidden sm:inline">
              Short lowercase id. Used in prep files and env var names.
            </Text>
            <Button
              variant="ghost-danger"
              iconOnly
              type="button"
              className="ml-auto"
              aria-label={`Remove ${row.person.name || row.key || "person"}`}
              onClick={() => onChange(rows.filter((_, j) => j !== i))}
            >
              <X className="w-4 h-4" />
            </Button>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {COLUMNS.map((c) => (
              <Field
                key={c.field}
                id={`person-${i}-${c.field}`}
                label={c.label}
                placeholder={c.placeholder}
                value={row.person[c.field] || ""}
                onChange={(v) => setField(i, c.field, v)}
              />
            ))}
          </div>
        </div>
      ))}
      <div>
        <Button
          variant="outline"
          size="sm"
          type="button"
          onClick={() => onChange([...rows, { key: "", person: { ...EMPTY_PERSON } }])}
        >
          <Plus className="w-3.5 h-3.5" />
          Add person
        </Button>
      </div>
    </div>
  );
}

export function StepTeam({
  config,
  nav,
  onSaved,
}: {
  config: SetupConfig;
  nav: StepNav;
  onSaved: (s: SetupConfig) => void;
}) {
  const [rows, setRows] = useState<Row[]>(Object.entries(config.people).map(([key, person]) => ({ key, person })));
  const [domains, setDomains] = useState(config.pipeline.internal_email_domains.join(", "));
  const [timezone, setTimezone] = useState(config.pipeline.timezone);
  const [channels, setChannels] = useState<Pipeline["channels"]>(config.pipeline.channels);
  const save = useSave();

  function submit() {
    const people = Object.fromEntries(
      rows.map((r) => [
        r.key.trim(),
        Object.fromEntries(
          Object.entries(r.person)
            // a new person gets circleback-key-<key> from the server; never send it as empty
            .filter(([k, v]) => k !== "circleback_key_name" || (typeof v === "string" && v.trim()))
            .map(([k, v]) => [k, typeof v === "string" && v.trim() ? v.trim() : null]),
        ),
      ]),
    );
    const body = {
      people,
      pipeline: {
        timezone,
        internal_email_domains: domains
          .split(/[,\s]+/)
          .map((d) => d.trim().toLowerCase())
          .filter(Boolean),
        channels,
      },
    };
    save
      .run(() => api<Saved>("config", "PUT", body), (r) => changedText(r.changed, r.notes))
      .then((r) => {
        if (r) onSaved(r.setup);
      });
  }

  return (
    <StepFrame
      nav={nav}
      title="Team"
      intro="Everyone who runs discovery calls, and where the bot posts."
      onSave={submit}
      saving={save.saving}
      problems={save.problems}
    >
      <Section title="People" description="A call with only these people (or your internal domains) is internal and skipped.">
        <PeopleEditor rows={rows} onChange={setRows} />
      </Section>
      <Section title="Internal email domains" description="Comma separated. Attendees on these domains count as your team.">
        <Field id="internal-domains" label="Domains" placeholder="acme.dev" value={domains} onChange={setDomains} />
      </Section>
      <Section
        title="Time zone"
        description="Sets call dates and when the sweep and roll-up run. Set it before your first call is processed."
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="timezone" className="text-ink-2">
            IANA time zone
          </Label>
          <TimezoneSelect id="timezone" value={timezone} onChange={setTimezone} />
        </div>
      </Section>
      <Section
        title="Slack channels"
        description="Create these in Slack and invite the bot once it exists (step 7). Lowercase, no spaces."
      >
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <Field
            id="channel-calls"
            label="Briefs"
            mono
            value={channels.calls}
            onChange={(v) => setChannels({ ...channels, calls: v })}
          />
          <Field
            id="channel-ops"
            label="Errors and re-runs"
            mono
            value={channels.ops}
            onChange={(v) => setChannels({ ...channels, ops: v })}
          />
          <Field
            id="channel-test"
            label="Staging"
            mono
            value={channels.test}
            onChange={(v) => setChannels({ ...channels, test: v })}
          />
        </div>
      </Section>
    </StepFrame>
  );
}

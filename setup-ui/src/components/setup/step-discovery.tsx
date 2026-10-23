"use client";

import { useState } from "react";
import { Plus, X } from "lucide-react";

import { api, type Domain, type SetupConfig } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Text } from "@/components/ui/text";
import { changedText, Field, Section, useSave } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";

type Saved = { changed: string[]; notes?: string[]; setup: SetupConfig };

const KEEP_ROLES = ["expert", "competitor"];
const DEFAULT_ROLES: [string, string][] = [
  ["customer", "would buy or use what you are building"],
  ["intermediary", "sits between the buyer and the seller: a broker, reseller or service provider"],
  ["vendor", "sells data, software or services into the market"],
  ["expert", "consultant, advisor or former executive speaking from experience"],
  ["competitor", "builds something close to what you are building"],
];

const TEXT_FIELDS: { key: keyof Domain; label: string; hint: string; multiline?: boolean; required?: boolean }[] = [
  { key: "company", label: "Company", hint: "As the models should call you.", required: true },
  { key: "team", label: "Who decides", hint: "Who reads the findings and makes the calls, e.g. founders.", required: true },
  { key: "about", label: "About", hint: "One line: who you are and what you are deciding.", multiline: true, required: true },
  { key: "market", label: "Market", hint: "The market in a few words.", required: true },
  { key: "exploring", label: "Exploring", hint: "The problem areas you are exploring.", multiline: true, required: true },
  { key: "interviewees", label: "Interviewees", hint: "Who you interview, as a plain list.", multiline: true, required: true },
  { key: "institution_examples", label: "Example firms", hint: "Kinds of organizations interviewees work at. Optional." },
  { key: "bot_name", label: "Bot name", hint: "The Slack bot's display name, e.g. Nua Discovery." },
];

const SCOPE_FIELDS: { key: "line" | "party" | "segment"; label: string; hint: string }[] = [
  { key: "line", label: "Line", hint: "The product line, workflow or use case a finding is about, with a few examples." },
  { key: "party", label: "Party", hint: "The kind of organization a finding is about, with a few examples." },
  { key: "segment", label: "Segment", hint: "The market segment: company size, region or tier, with a few examples." },
];

function ExamplePicker({
  examples,
  onApplied,
}: {
  examples: string[];
  onApplied: (s: Saved) => void;
}) {
  const [picked, setPicked] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ domain: Domain; seats: string[] } | null>(null);
  const save = useSave();
  function pick(name: string) {
    setPicked(name);
    setPreview(null);
    api<{ domain: Domain; seats: string[] }>(`examples/${name}`)
      .then(setPreview)
      .catch(() => setPreview(null));
  }
  function apply() {
    if (!picked) return;
    save
      .run(() => api<Saved>(`examples/${picked}/apply`, "POST"), (r) => changedText(r.changed))
      .then((r) => {
        if (r) onApplied(r);
      });
  }
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        {examples.map((name) => (
          <Button
            key={name}
            type="button"
            size="sm"
            variant={picked === name ? "primary" : "secondary"}
            onClick={() => pick(name)}
          >
            {name === "blank" && "Blank"}
            {name !== "blank" && name.replace(/-/g, " ")}
          </Button>
        ))}
      </div>
      {preview && picked && (
        <div className="flex flex-col gap-2 rounded-md border border-rule bg-bg-sunk px-4 py-3">
          <Text size="sm" weight="medium">
            {preview.domain.company || "Empty templates"}
          </Text>
          {preview.domain.market && (
            <Text size="sm" color="muted">
              {preview.domain.market}
            </Text>
          )}
          <Text size="xs" color="faint">
            {preview.seats.length} seats, {Object.keys(preview.domain.roles).length} roles. Applying replaces
            config/domain.yml, seats.yml and entities.yml. Your team and pipeline settings stay.
          </Text>
          <div>
            <Button size="sm" onClick={apply} loading={save.saving}>
              Use {picked}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function RolesEditor({ roles, onChange }: { roles: [string, string][]; onChange: (r: [string, string][]) => void }) {
  if (!roles.length) {
    return (
      <div className="flex items-center gap-3 flex-wrap">
        <Text size="sm" color="muted">
          Using the defaults: customer, intermediary, vendor, expert, competitor.
        </Text>
        <Button variant="outline" size="sm" type="button" onClick={() => onChange(DEFAULT_ROLES)}>
          Customize roles
        </Button>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      {roles.map(([id, desc], i) => (
        <div key={`role-${i}`} className="grid grid-cols-[minmax(0,8rem)_minmax(0,1fr)_auto] gap-2 items-center">
          <Input
            aria-label={`Role id ${i + 1}`}
            value={id}
            className="font-mono"
            disabled={KEEP_ROLES.includes(id)}
            onChange={(e) => onChange(roles.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))}
          />
          <Input
            aria-label={`Role description ${i + 1}`}
            value={desc}
            placeholder="What this role means"
            onChange={(e) => onChange(roles.map((r, j) => (j === i ? [r[0], e.target.value] : r)))}
          />
          <Button
            variant="ghost-danger"
            iconOnly
            type="button"
            aria-label={`Remove role ${id}`}
            disabled={KEEP_ROLES.includes(id)}
            onClick={() => onChange(roles.filter((_, j) => j !== i))}
          >
            <X className="w-4 h-4" />
          </Button>
        </div>
      ))}
      <div className="flex items-center gap-3 flex-wrap">
        <Button variant="outline" size="sm" type="button" onClick={() => onChange([...roles, ["", ""]])}>
          <Plus className="w-3.5 h-3.5" />
          Add role
        </Button>
        <Text size="xs" color="faint">
          expert and competitor stay: the prompts use them.
        </Text>
      </div>
    </div>
  );
}

export function StepDiscovery({
  config,
  nav,
  onSaved,
}: {
  config: SetupConfig;
  nav: StepNav;
  onSaved: (s: SetupConfig) => void;
}) {
  const [domain, setDomain] = useState<Domain>(config.domain);
  const [roles, setRoles] = useState<[string, string][]>(Object.entries(config.domain.roles));
  const save = useSave();

  function set<K extends keyof Domain>(key: K, value: Domain[K]) {
    setDomain((d) => ({ ...d, [key]: value }));
  }

  function submit() {
    const body = { domain: { ...domain, roles: Object.fromEntries(roles.filter(([id]) => id.trim())) } };
    save
      .run(() => api<Saved>("config", "PUT", body), (r) => changedText(r.changed, r.notes))
      .then((r) => {
        if (r) onSaved(r.setup);
      });
  }

  return (
    <StepFrame
      nav={nav}
      title="Your discovery"
      intro="What you are researching and who you talk to. Every prompt reads this, so plain and specific beats clever."
      onSave={submit}
      saving={save.saving}
      problems={save.problems}
    >
      <Section
        title="Start from"
        description="Optional. An example fills these fields, the roles and the seat list. Pick one to preview it."
      >
        <ExamplePicker examples={config.examples} onApplied={(r) => onSaved(r.setup)} />
      </Section>
      <Section title="About your discovery">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {TEXT_FIELDS.map((f) => (
            <Field
              key={f.key}
              id={`domain-${f.key}`}
              label={f.label}
              hint={f.hint}
              required={f.required}
              multiline={f.multiline}
              className={f.multiline ? "md:col-span-2" : undefined}
              value={String(domain[f.key] ?? "")}
              onChange={(v) => set(f.key, v as never)}
            />
          ))}
        </div>
      </Section>
      <Section
        title="Scope"
        description="Each finding is scoped so it is never stretched beyond what was said. Describe the three scope fields for your market. The fourth, seat, is step 3."
      >
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {SCOPE_FIELDS.map((f) => (
            <Field
              key={f.key}
              id={`scope-${f.key}`}
              label={f.label}
              hint={f.hint}
              value={domain.scope[f.key]}
              onChange={(v) => set("scope", { ...domain.scope, [f.key]: v })}
            />
          ))}
        </div>
      </Section>
      <Section
        title="Roles"
        description="Where the main person on a call stands relative to what you are building. Ids are short lowercase words."
      >
        <RolesEditor roles={roles} onChange={setRoles} />
      </Section>
    </StepFrame>
  );
}

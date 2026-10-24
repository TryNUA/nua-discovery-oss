"use client";

import { ChevronDown, Plus, X } from "lucide-react";

import { api, useApi, type Domain, type EnvStatus, type Person, type SetupConfig } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { CollapsibleSection } from "@/components/ui/collapsible";
import { Combobox } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { emailFor, inferEmailPattern, personKey, suggestedDomains } from "@/lib/people";
import { Label } from "@/components/ui/label";
import { Text } from "@/components/ui/text";
import { Field, Section, SecretField, StringList, TextLink } from "@/components/setup/common";
import { useDraft, useFlush, useProblems, useQuietSet, useSaver, type FieldProblem } from "@/components/setup/store";
import { StepFrame, type StepNav } from "@/components/setup/frame";

type Saved = { changed: string[]; notes?: string[]; problems?: FieldProblem[]; setup: SetupConfig };
// savedKey: the key in people.yml, never renamed (env var names and Circleback URLs use it); null for a
// new row, whose key comes from its name on save. emailAuto: the email still follows the team's pattern.
// uid: a stable id for the row in this page, so a new person keeps the key their first save gave them.
type Row = { uid: string; savedKey: string | null; person: Person; emailAuto: boolean };

function uid(): string {
  return Math.random().toString(36).slice(2, 10);
}

const EMPTY_PERSON: Person = { name: "", email: "", slack_id: null, github_login: null, circleback_key_name: null };

const KEEP_ROLES = ["expert", "competitor"];
const DEFAULT_ROLES: [string, string][] = [
  ["customer", "would buy or use what you are building"],
  ["intermediary", "sits between the buyer and the seller: a broker, reseller or service provider"],
  ["vendor", "sells data, software or services into the market"],
  ["expert", "consultant, advisor or former executive speaking from experience"],
  ["competitor", "builds something close to what you are building"],
];

const SCOPE_FIELDS: { key: "line" | "party" | "segment"; label: string; hint: string }[] = [
  { key: "line", label: "Line", hint: "The product line, workflow or use case a finding is about." },
  { key: "party", label: "Party", hint: "The kind of organization a finding is about." },
  { key: "segment", label: "Segment", hint: "Company size, region or tier." },
];

const MORE_KEYS = [
  { name: "ANTHROPIC_API_KEY", label: "Anthropic API key", href: "https://console.anthropic.com", where: "console.anthropic.com" },
  { name: "OPENAI_API_KEY", label: "OpenAI API key", href: "https://platform.openai.com", where: "platform.openai.com" },
];

/** Each row's key: saved keys as they are, new ones made from the name and unique. */
function rowKeys(rows: Row[], assigned: Record<string, string> = {}): string[] {
  const fixed = (r: Row) => r.savedKey || assigned[r.uid] || null;
  const taken = rows.map(fixed).filter((k): k is string => Boolean(k));
  return rows.map((r) => {
    const key = fixed(r);
    if (key) return key;
    const made = personKey(r.person.name, taken);
    taken.push(made);
    return made;
  });
}

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

function PeopleEditor({
  rows,
  assigned,
  onChange,
  error,
}: {
  rows: Row[];
  assigned: Record<string, string>;
  onChange: (rows: Row[]) => void;
  error: (field: string) => string | undefined;
}) {
  const keys = rowKeys(rows, assigned);
  function setName(i: number, name: string) {
    // the first other row with a name and an email sets the pattern for the rest
    const source = rows.find((r, j) => j !== i && r.person.name && r.person.email);
    const pattern = source ? inferEmailPattern(source.person.name, source.person.email) : null;
    onChange(
      rows.map((r, j) => {
        if (j !== i) return r;
        const email = r.emailAuto && pattern ? emailFor(name, pattern) || "" : r.person.email;
        return { ...r, person: { ...r.person, name, email } };
      }),
    );
  }
  function setEmail(i: number, email: string) {
    onChange(rows.map((r, j) => (j === i ? { ...r, emailAuto: false, person: { ...r.person, email } } : r)));
  }
  return (
    <div className="flex flex-col gap-3">
      {rows.map((row, i) => (
        <div key={`person-${i}`} className="rounded-md border border-rule bg-bg-raised shadow-xs p-3 flex flex-col gap-2">
          <div className="grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-3 items-end">
            <Field
              id={`person-${i}-name`}
              label="Name"
              placeholder="Alex Rivera"
              value={row.person.name || ""}
              error={error(`people.${keys[i]}.key`)}
              onChange={(v) => setName(i, v)}
            />
            <Field
              id={`person-${i}-email`}
              label="Email their calls are recorded under"
              placeholder="alex@acme.dev"
              value={row.person.email || ""}
              error={error(`people.${keys[i]}.email`)}
              onChange={(v) => setEmail(i, v)}
            />
            <Button
              variant="ghost-danger"
              iconOnly
              type="button"
              aria-label={`Remove ${row.person.name || "person"}`}
              onClick={() => onChange(rows.filter((_, j) => j !== i))}
            >
              <X className="w-4 h-4" />
            </Button>
          </div>
          <CollapsibleSection title="Advanced" titleSize="xs">
            <div className="flex flex-col gap-1">
              <Text size="xs" color="muted">
                Id{" "}
                <Text size="xs" mono>
                  {keys[i]}
                </Text>
              </Text>
              <Text size="xs" color="faint">
                It names their secrets and Circleback URL, so it never changes once saved.
              </Text>
            </div>
          </CollapsibleSection>
        </div>
      ))}
      <div>
        <Button
          variant="outline"
          size="sm"
          type="button"
          onClick={() => onChange([...rows, { uid: uid(), savedKey: null, person: { ...EMPTY_PERSON }, emailAuto: true }])}
        >
          <Plus className="w-3.5 h-3.5" />
          Add person
        </Button>
      </div>
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

export function StepTeam({
  config,
  env,
  nav,
}: {
  config: SetupConfig;
  env: EnvStatus;
  nav: StepNav;
}) {
  const step = "team" as const;
  const [domain, setDomain] = useDraft<Domain>(step, "domain", () => config.domain);
  const [rows, setRows] = useDraft<Row[]>(step, "rows", () =>
    Object.entries(config.people).map(([key, person]) => ({ uid: uid(), savedKey: key, person, emailAuto: !person.email })),
  );
  const [assigned] = useDraft<Record<string, string>>(step, "assigned", () => ({}));
  // until edited, "Also internal" shows the saved domains plus teammates' work domains
  const [domainsEdited, setDomainsEdited] = useDraft<string | null>(step, "domains", () => null);
  const suggested = suggestedDomains(
    rows.map((r) => r.person.email),
    config.personal_domains ?? [],
  ).filter((d) => !config.pipeline.internal_email_domains.includes(d));
  const domains = domainsEdited ?? [...config.pipeline.internal_email_domains, ...suggested].join(", ");
  const [timezone, setTimezone] = useDraft(step, "timezone", () => config.pipeline.timezone);
  const [roles, setRoles] = useDraft<[string, string][]>(step, "roles", () => Object.entries(config.domain.roles));
  const [seats, setSeats] = useDraft<string[]>(step, "seats", () => config.seats);
  const [gemini, setGemini] = useDraft(step, "secret:GEMINI_API_KEY", () => "", { secret: true });
  const [anthropic, setAnthropic] = useDraft(step, "secret:ANTHROPIC_API_KEY", () => "", { secret: true });
  const [openai, setOpenai] = useDraft(step, "secret:OPENAI_API_KEY", () => "", { secret: true });
  const quiet = useQuietSet(step);
  const flush = useFlush(step);
  const { error } = useProblems(step);
  const keys: Record<string, string> = { GEMINI_API_KEY: gemini, ANTHROPIC_API_KEY: anthropic, OPENAI_API_KEY: openai };
  const setKey: Record<string, (v: string) => void> = {
    GEMINI_API_KEY: setGemini,
    ANTHROPIC_API_KEY: setAnthropic,
    OPENAI_API_KEY: setOpenai,
  };

  function set<K extends keyof Domain>(key: K, value: Domain[K]) {
    setDomain({ ...domain, [key]: value });
  }

  useSaver(step, () => {
    const ids = rowKeys(rows, assigned);
    const people = Object.fromEntries(
      rows
        .map((r, i) => [ids[i], r] as const)
        // a row without a name yet is not a person yet
        .filter(([, r]) => (r.person.name || "").trim())
        .map(([id, r]) => [
          id,
          Object.fromEntries(
            Object.entries(r.person)
              // a new person gets circleback-key-<id> from the server; never send it as empty
              .filter(([k, v]) => k !== "circleback_key_name" || (typeof v === "string" && v.trim()))
              .map(([k, v]) => [k, typeof v === "string" && v.trim() ? v.trim() : null]),
          ),
        ]),
    );
    const body = {
      domain: { ...domain, roles: Object.fromEntries(roles.filter(([id]) => id.trim())) },
      people,
      seats: seats.map((x) => x.trim()).filter(Boolean),
      pipeline: {
        timezone,
        internal_email_domains: domains
          .split(/[,\s]+/)
          .map((d) => d.trim().toLowerCase())
          .filter(Boolean),
      },
    };
    const secrets = Object.fromEntries(Object.entries(keys).filter(([, v]) => v.trim()));
    return api<Saved>("config", "PUT", body).then((saved) =>
      api<EnvStatus & { written: string[]; problems: FieldProblem[] }>("env", "PUT", { values: secrets }).then((e) => {
        // a saved key leaves the browser right away
        e.written.forEach((k) => quiet(`secret:${k}`, ""));
        // a new person keeps the key their first save gave them, even if their name changes later
        const fixedNow = Object.fromEntries(
          rows.map((r, i) => [r.uid, ids[i]] as const).filter(([u, id], i) => !rows[i].savedKey && people[id] && !assigned[u]),
        );
        if (Object.keys(fixedNow).length) quiet("assigned", { ...assigned, ...fixedNow });
        return [...(saved.problems || []), ...e.problems];
      }),
    );
  });

  return (
    <StepFrame
      nav={nav}
      title="About your team"
      intro="What you are researching, who runs the calls, and the model key. Every prompt reads this, so plain and specific beats clever."
    >
      <Section title="Your discovery">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Field
            id="domain-company"
            label="Company"
            required
            placeholder="Halyard Labs"
            hint="As the models should call you."
            value={domain.company}
            error={error("domain.company")}
            onChange={(v) => set("company", v)}
          />
          <Field
            id="domain-market"
            label="Market"
            required
            placeholder="mid-size freight brokerages"
            hint="Who you sell to, in a few words."
            value={domain.market}
            error={error("domain.market")}
            onChange={(v) => set("market", v)}
          />
          <Field
            id="domain-exploring"
            label="What you're exploring"
            required
            multiline
            className="md:col-span-2"
            placeholder="carrier onboarding and fraud checks, track and trace, carrier payments and disputes"
            hint="The problem areas you are testing. Findings are judged against these."
            value={domain.exploring}
            error={error("domain.exploring")}
            onChange={(v) => set("exploring", v)}
          />
        </div>
      </Section>
      <Section
        title="People"
        description="Everyone who runs discovery calls. A call with only your team on it is internal and skipped."
      >
        <PeopleEditor rows={rows} assigned={assigned} onChange={setRows} error={error} />
      </Section>
      <Section title="Also internal">
        <Field
          id="internal-domains"
          label="Email domains (optional)"
          placeholder="acme.dev"
          hint="Anyone on these domains counts as your team, even if not listed above. Teammates' own emails always count."
          value={domains}
          error={error("pipeline.internal_email_domains")}
          onChange={setDomainsEdited}
        />
      </Section>
      <Section title="Time zone" description="Sets call dates and when the scheduled jobs run.">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="timezone" className="sr-only">
            Time zone
          </Label>
          <TimezoneSelect id="timezone" value={timezone} onChange={setTimezone} />
          {error("pipeline.timezone") && (
            <Text size="xs" color="danger">
              {error("pipeline.timezone")}
            </Text>
          )}
        </div>
      </Section>
      <Section title="Model key" description="The default config runs every model role on Gemini, so one key is enough.">
        <SecretField
          name="GEMINI_API_KEY"
          label="Gemini API key"
          required
          isSet={env.keys.GEMINI_API_KEY}
          value={gemini}
          error={error("env.GEMINI_API_KEY")}
          onBlur={flush}
          onChange={setGemini}
          hint={
            <>
              get one at <TextLink href="https://aistudio.google.com/apikey">Google AI Studio</TextLink> and turn on
              billing
            </>
          }
        />
        <CollapsibleSection title="Use more providers">
          <div className="flex flex-col gap-4">
            {MORE_KEYS.map((k) => (
              <SecretField
                key={k.name}
                name={k.name}
                label={k.label}
                isSet={env.keys[k.name]}
                value={keys[k.name]}
                error={error(`env.${k.name}`)}
                onBlur={flush}
                onChange={setKey[k.name]}
                hint={
                  <>
                    from <TextLink href={k.href}>{k.where}</TextLink>; only used if config/pipeline.yml names it
                  </>
                }
              />
            ))}
          </div>
        </CollapsibleSection>
      </Section>
      <Section title="Advanced" description="Optional. The defaults work for most teams.">
        <CollapsibleSection title="Scope, roles and seats">
          <div className="flex flex-col gap-6">
            <div className="flex flex-col gap-3">
              <Text size="sm" weight="medium">
                Scope
              </Text>
              <Text size="xs" color="faint">
                Each finding is scoped so it is never stretched beyond what was said. Give a few examples for each.
              </Text>
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
            </div>
            <div className="flex flex-col gap-3">
              <Text size="sm" weight="medium">
                Roles
              </Text>
              <Text size="xs" color="faint">
                Where the main person on a call stands relative to what you are building.
              </Text>
              <RolesEditor roles={roles} onChange={setRoles} />
              {error("domain.roles") && (
                <Text size="xs" color="danger">
                  {error("domain.roles")}
                </Text>
              )}
            </div>
            <Field
              id="institution-examples"
              label="Institution examples"
              placeholder="independent brokerage, enterprise 3PL, regional carrier"
              hint="The kinds of organizations interviewees work at."
              value={domain.institution_examples}
              onChange={(v) => set("institution_examples", v)}
            />
            <div className="flex flex-col gap-3">
              <Text size="sm" weight="medium">
                Seed seats
              </Text>
              <Text size="xs" color="faint">
                Optional. Seats are the roles people speak from; the list builds itself from your calls.
              </Text>
              <StringList
                idPrefix="Seat"
                items={seats}
                onChange={setSeats}
                placeholder="head of operations"
                addLabel="Add seat"
              />
              {error("seats") && (
                <Text size="xs" color="danger">
                  {error("seats")}
                </Text>
              )}
            </div>
          </div>
        </CollapsibleSection>
      </Section>
    </StepFrame>
  );
}

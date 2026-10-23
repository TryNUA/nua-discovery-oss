"use client";

import { useState } from "react";

import { api, type DeployInfo, type EnvStatus, type Recorder, type SetupConfig } from "@/lib/api";
import { Checkbox } from "@/components/ui/checkbox";
import { Text } from "@/components/ui/text";
import { ChoiceCards, Note, Section, SecretField, useSave } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";

const CHOICES = [
  {
    value: "granola",
    title: "Granola",
    description: "One Business workspace key reads the Team space. Webhooks need Granola Business.",
  },
  {
    value: "circleback",
    title: "Circleback",
    description: "Each person who records has their own API key and automation. Tag calls discovery.",
  },
  { value: "both", title: "Both", description: "Some people on Granola, some on Circleback." },
];

export function personVar(prefix: string, person: string) {
  return prefix + person.toUpperCase().replace(/[^A-Z0-9]/g, "_");
}

export function StepRecorder({
  config,
  deploy,
  env,
  nav,
  onSaved,
}: {
  config: SetupConfig;
  deploy: DeployInfo;
  env: EnvStatus;
  nav: StepNav;
  onSaved: () => void;
}) {
  const [recorder, setRecorder] = useState<Recorder>(deploy.recorder || "granola");
  const [people, setPeople] = useState<string[]>(
    Array.isArray(deploy.fields.circleback_people) ? (deploy.fields.circleback_people as string[]) : [],
  );
  const [values, setValues] = useState<Record<string, string>>({});
  const save = useSave();
  const team = Object.entries(config.people);
  const usesGranola = recorder === "granola" || recorder === "both";
  const usesCircleback = recorder === "circleback" || recorder === "both";

  function toggle(key: string, on: boolean) {
    setPeople((p) => (on ? [...p.filter((x) => x !== key), key] : p.filter((x) => x !== key)));
  }

  function submit() {
    const ordered = team.map(([k]) => k).filter((k) => people.includes(k));
    save
      .run(
        () =>
          api<DeployInfo>("deploy", "PUT", {
            recorder,
            fields: usesCircleback ? { circleback_people: ordered } : {},
          }).then(() => api<EnvStatus>("env", "PUT", { values })),
        (r) => {
          const written = (r as EnvStatus & { written: string[] }).written;
          return written.length ? `Wrote ${written.join(", ")} to .env (values not shown)` : "Recorder saved.";
        },
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
      title="Recorder"
      intro="Where your calls come from. Each person records on one of them; a team can mix."
      onSave={submit}
      saving={save.saving}
      problems={save.problems}
    >
      <Section title="Which recorder">
        <ChoiceCards label="Recorder" value={recorder} onChange={(v) => setRecorder(v as Recorder)} choices={CHOICES} />
      </Section>
      {usesCircleback && (
        <Section
          title="Who records on Circleback"
          description="Each gets their own webhook URL and signing secret. Saved as circleback_people in terraform.tfvars."
        >
          <div className="flex flex-col gap-2.5">
            {team.map(([key, p]) => (
              <Checkbox
                key={key}
                label={`${p.name || key} (${key})`}
                checked={people.includes(key)}
                onCheckedChange={(c) => toggle(key, c === true)}
              />
            ))}
            {!team.length && (
              <Text size="sm" color="faint">
                Add your team in step 2 first.
              </Text>
            )}
          </div>
        </Section>
      )}
      <Section
        title="API keys"
        description="The sweep uses these to catch calls a webhook missed. Signing secrets come later, in step 10, once the webhooks exist."
      >
        <div className="flex flex-col gap-4">
          {usesGranola && (
            <SecretField
              name="GRANOLA_API_KEY"
              label="Granola workspace API key"
              required
              isSet={env.keys.GRANOLA_API_KEY}
              value={values.GRANOLA_API_KEY || ""}
              onChange={(v) => setValues({ ...values, GRANOLA_API_KEY: v })}
              hint="Granola workspace settings, as an admin"
            />
          )}
          {usesCircleback &&
            team
              .filter(([key]) => people.includes(key))
              .map(([key, p]) => (
                <SecretField
                  key={key}
                  name={personVar("CIRCLEBACK_API_KEY_", key)}
                  label={`Circleback API key for ${p.name || key}`}
                  required
                  isSet={Boolean(env.keys[personVar("CIRCLEBACK_API_KEY_", key)])}
                  value={values[personVar("CIRCLEBACK_API_KEY_", key)] || ""}
                  onChange={(v) => setValues({ ...values, [personVar("CIRCLEBACK_API_KEY_", key)]: v })}
                  hint="their cb_... key from Circleback Settings > API keys"
                />
              ))}
          {usesCircleback && !people.length && (
            <Note title="Pick at least one person above." variant="warn" />
          )}
        </div>
      </Section>
      {usesGranola && (
        <Section title="Granola workspace">
          <Text size="sm" color="muted" leading="relaxed">
            Share discovery calls to the Team space and turn off Allow Granola API access on every other space. Details in{" "}
            <Text size="sm" mono>
              docs/self-hosting.md
            </Text>
            , step 8a.
          </Text>
        </Section>
      )}
    </StepFrame>
  );
}

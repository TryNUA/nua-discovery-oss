"use client";

import { api, type DeployInfo, type EnvStatus, type Recorder, type SetupConfig } from "@/lib/api";
import { Checkbox } from "@/components/ui/checkbox";
import { Text } from "@/components/ui/text";
import { ChoiceCards, Note, Section, SecretField } from "@/components/setup/common";
import { useDraft, useFlush, useProblems, useQuietSet, useSaver, type FieldProblem } from "@/components/setup/store";
import { StepFrame, type StepNav } from "@/components/setup/frame";
import { CIRCLEBACK_KEY, GRANOLA_KEY, HowTo } from "@/components/setup/howto";

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
}: {
  config: SetupConfig;
  deploy: DeployInfo;
  env: EnvStatus;
  nav: StepNav;
}) {
  const step = "recorder" as const;
  const [recorder, setRecorder] = useDraft<Recorder>(step, "recorder", () => deploy.recorder || "granola");
  const [people, setPeople] = useDraft<string[]>(step, "people", () =>
    Array.isArray(deploy.fields.circleback_people) ? (deploy.fields.circleback_people as string[]) : [],
  );
  const [values, setValues] = useDraft<Record<string, string>>(step, "secret:values", () => ({}), { secret: true });
  const quiet = useQuietSet(step);
  const flush = useFlush(step);
  const { error } = useProblems(step);
  const team = Object.entries(config.people);
  const usesGranola = recorder === "granola" || recorder === "both";
  const usesCircleback = recorder === "circleback" || recorder === "both";

  function toggle(key: string, on: boolean) {
    setPeople(on ? [...people.filter((x) => x !== key), key] : people.filter((x) => x !== key));
  }

  useSaver(step, () => {
    const ordered = team.map(([k]) => k).filter((k) => people.includes(k));
    const secrets = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim()));
    return api<DeployInfo & { problems: FieldProblem[] }>("deploy", "PUT", {
      recorder,
      fields: usesCircleback ? { circleback_people: ordered } : {},
    }).then((d) =>
      api<EnvStatus & { written: string[]; problems: FieldProblem[] }>("env", "PUT", { values: secrets }).then((e) => {
        if (e.written.length) {
          quiet("secret:values", Object.fromEntries(Object.entries(values).filter(([k]) => !e.written.includes(k))));
        }
        return [...(d.problems || []), ...e.problems];
      }),
    );
  });

  return (
    <StepFrame
      nav={nav}
      title="Recorder"
      intro="Where your calls come from. Each person records on one of them; a team can mix."
    >
      <Section
        title="Which recorder"
        description="Which should I pick? Granola if the team shares one Business workspace (one admin key covers everyone). Circleback if each person has their own account. Both is fine."
      >
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
                Add your team in step 1 first.
              </Text>
            )}
          </div>
        </Section>
      )}
      <Section
        title="API keys"
        description="The sweep uses these to catch calls a webhook missed. Signing secrets come later, in step 7, once the webhooks exist."
      >
        <div className="flex flex-col gap-4">
          {usesGranola && (
            <div className="flex flex-col gap-2">
            <SecretField
              name="GRANOLA_API_KEY"
              label="Granola workspace API key"
              required
              isSet={env.keys.GRANOLA_API_KEY}
              value={values.GRANOLA_API_KEY || ""}
              error={error("env.GRANOLA_API_KEY")}
              onBlur={flush}
              onChange={(v) => setValues({ ...values, GRANOLA_API_KEY: v })}
              hint="a workspace key, created by an admin"
            />
            <HowTo guide={GRANOLA_KEY} />
            </div>
          )}
          {usesCircleback &&
            team
              .filter(([key]) => people.includes(key))
              .map(([key, p]) => (
                <div key={key} className="flex flex-col gap-2">
                <SecretField
                  name={personVar("CIRCLEBACK_API_KEY_", key)}
                  label={`Circleback API key for ${p.name || key}`}
                  required
                  isSet={Boolean(env.keys[personVar("CIRCLEBACK_API_KEY_", key)])}
                  value={values[personVar("CIRCLEBACK_API_KEY_", key)] || ""}
                  error={error(`env.${personVar("CIRCLEBACK_API_KEY_", key)}`)}
                  onBlur={flush}
                  onChange={(v) => setValues({ ...values, [personVar("CIRCLEBACK_API_KEY_", key)]: v })}
                  hint="their own key; it only sees their meetings"
                />
                <HowTo guide={CIRCLEBACK_KEY} />
                </div>
              ))}
          {usesCircleback && !people.length && (
            <Note title="Pick at least one person above." variant="warn" />
          )}
        </div>
      </Section>
    </StepFrame>
  );
}

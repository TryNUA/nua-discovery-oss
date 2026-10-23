"use client";

import { useState } from "react";

import { api, type EnvStatus } from "@/lib/api";
import { Text } from "@/components/ui/text";
import { Section, SecretField, TextLink, useSave } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";

const KEYS = [
  {
    name: "GEMINI_API_KEY",
    label: "Gemini API key",
    required: true,
    href: "https://aistudio.google.com/apikey",
    where: "Google AI Studio",
    note: "Turn on billing: on the paid tier Google does not use your prompts, and free-tier limits are too low for a backfill.",
  },
  {
    name: "ANTHROPIC_API_KEY",
    label: "Anthropic API key",
    required: false,
    href: "https://console.anthropic.com",
    where: "console.anthropic.com",
    note: "Only if you switch a model role to Claude in config/pipeline.yml.",
  },
  {
    name: "OPENAI_API_KEY",
    label: "OpenAI API key",
    required: false,
    href: "https://platform.openai.com",
    where: "platform.openai.com",
    note: "Only if you switch a model role to OpenAI in config/pipeline.yml.",
  },
];

export function StepModels({ env, nav, onSaved }: { env: EnvStatus; nav: StepNav; onSaved: () => void }) {
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
      title="Model keys"
      intro="The default config runs every model role on Gemini, so one key is enough. A call costs about $0.25 to $2."
      onSave={submit}
      saving={save.saving}
      problems={save.problems}
      saveLabel="Save keys"
    >
      {KEYS.map((k) => (
        <Section
          key={k.name}
          title={k.label}
          description={
            <>
              Get one at <TextLink href={k.href}>{k.where}</TextLink>. {k.note}
            </>
          }
        >
          <SecretField
            name={k.name}
            label={k.label}
            required={k.required}
            isSet={env.keys[k.name]}
            value={values[k.name] || ""}
            onChange={(v) => setValues({ ...values, [k.name]: v })}
          />
        </Section>
      ))}
      <Section title="Where they go">
        <Text size="sm" color="muted" leading="relaxed">
          Keys are written to .env at the repo root, which is gitignored. The deploy command copies them to your
          cloud&apos;s secret store. This page never shows a saved value again.
        </Text>
      </Section>
    </StepFrame>
  );
}

"use client";

import { useState } from "react";

import { api, type SetupConfig } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Text } from "@/components/ui/text";
import { changedText, Section, StringList, useSave } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";

type Saved = { changed: string[]; setup: SetupConfig };

export function StepSeats({
  config,
  nav,
  onSaved,
}: {
  config: SetupConfig;
  nav: StepNav;
  onSaved: (s: SetupConfig) => void;
}) {
  const [seats, setSeats] = useState<string[]>(config.seats);
  const [loading, setLoading] = useState<string | null>(null);
  const save = useSave();
  const examples = config.examples.filter((e) => e !== "blank");

  function prefill(name: string) {
    setLoading(name);
    api<{ seats: string[] }>(`examples/${name}`)
      .then((ex) => setSeats(ex.seats))
      .catch(() => undefined)
      .finally(() => setLoading(null));
  }

  function submit() {
    const body = { seats: seats.map((s) => s.trim()).filter(Boolean) };
    save
      .run(() => api<Saved>("config", "PUT", body), (r) => changedText(r.changed))
      .then((r) => {
        if (r) {
          setSeats(r.setup.seats);
          onSaved(r.setup);
        }
      });
  }

  return (
    <StepFrame
      nav={nav}
      title="Seats"
      intro="The role someone speaks from on a call, such as head of operations or end user. Evidence for each belief is counted by seat."
      onSave={submit}
      saving={save.saving}
      problems={save.problems}
    >
      <Section
        title="Seat list"
        description="Keep it short (10 to 20) and distinct. Add other as a catch-all: a seat the models name that is not here becomes other."
      >
        <StringList
          idPrefix="Seat"
          items={seats}
          onChange={setSeats}
          placeholder="head of operations"
          addLabel="Add seat"
        />
        {!seats.length && (
          <Text size="sm" color="faint">
            No seats yet. Empty means seats stay as the models name them, which makes counting by seat unreliable.
          </Text>
        )}
      </Section>
      {examples.length > 0 && (
        <Section title="Fill from an example" description="Replaces the list above. Nothing is written until you save.">
          <div className="flex flex-wrap gap-2">
            {examples.map((name) => (
              <Button
                key={name}
                size="sm"
                variant="secondary"
                type="button"
                loading={loading === name}
                onClick={() => prefill(name)}
              >
                {name.replace(/-/g, " ")}
              </Button>
            ))}
          </div>
        </Section>
      )}
    </StepFrame>
  );
}

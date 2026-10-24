"use client";

import { useState } from "react";

import { api, problemsOf, refreshLive, useApi, type ModelsInfo } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { List, ListItem } from "@/components/ui/list";
import { Text } from "@/components/ui/text";
import { Note } from "@/components/setup/common";

const ROLE_LABELS: [keyof ModelsInfo["roles"], string][] = [
  ["extractors", "Extract findings"],
  ["arbiter", "Arbitrate"],
  ["ask_map", "Read for questions"],
  ["ask_reduce", "Write answers"],
  ["rollup", "Weekly roll-up"],
];

function RolesTable({ info }: { info: ModelsInfo }) {
  return (
    <List gap="none" className="rounded-md border border-rule bg-bg-raised px-3">
      {ROLE_LABELS.map(([role, label]) => (
        <ListItem key={role} align="baseline" gap="lg" className="py-1.5 border-b border-rule-2 last:border-b-0 flex-wrap">
          <Text size="xs" color="muted" className="w-36 flex-none">
            {label}
          </Text>
          <Text size="xs" mono className="min-w-0 break-all">
            {Array.isArray(info.roles[role]) ? (info.roles[role] as string[]).join(", ") : String(info.roles[role])}
          </Text>
        </ListItem>
      ))}
    </List>
  );
}

/** Suggests the cross-provider model setup when the keys allow it, and shows the current roles. */
export function ModelsCard() {
  const { data, mutate } = useApi<ModelsInfo>("models");
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  if (!data) return null;
  const { anthropic, openai, gemini } = data.keys;

  function apply(preset: "cross" | "gemini") {
    setBusy(true);
    setProblems([]);
    api<ModelsInfo>("models", "POST", { preset })
      .then((r) => {
        mutate(r, { revalidate: false });
        refreshLive();
      })
      .catch((e) => setProblems(problemsOf(e)))
      .finally(() => setBusy(false));
  }

  return (
    <div className="flex flex-col gap-3">
      {data.preset !== "cross" && anthropic && openai && (
        <div className="flex flex-col gap-3 rounded-md border border-rule bg-bg-raised shadow-xs p-4">
          <Text size="sm" weight="medium">
            You have keys for more than one provider. Models from different providers catch more of each other&apos;s
            misses.
          </Text>
          <Text size="xs" color="faint" leading="relaxed">
            Claude and GPT extract, {gemini ? "Gemini" : "Claude"} arbitrates and writes the weekly roll-up. This rewrites
            the model roles in config/pipeline.yml and bumps prompt_version.
            {!gemini && " Add a Gemini key first to get a third, independent judge."}
          </Text>
          <div>
            <Button size="sm" onClick={() => apply("cross")} loading={busy}>
              Use the cross-provider setup
            </Button>
          </div>
        </div>
      )}
      {data.preset !== "cross" && anthropic !== openai && (
        <Text size="xs" color="faint" leading="relaxed">
          {anthropic && "You have an Anthropic key. Add an OpenAI key too to use the cross-provider setup."}
          {openai && "You have an OpenAI key. Add an Anthropic key too to use the cross-provider setup."}
        </Text>
      )}
      {data.preset !== "gemini" && (
        <div className="flex flex-col gap-2">
          <Text size="xs" color="muted">
            {data.preset === "cross" && "Using the cross-provider setup"}
            {data.preset === "custom" && "Using custom model roles"} ({data.prompt_version})
          </Text>
          <RolesTable info={data} />
          <div>
            <Button size="sm" variant="secondary" onClick={() => apply("gemini")} loading={busy}>
              Switch back to Gemini only
            </Button>
          </div>
        </div>
      )}
      {problems.length > 0 && <Note title="That did not work" description={problems.join(" ")} variant="danger" />}
    </div>
  );
}

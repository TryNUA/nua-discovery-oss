"use client";

// Per-step drafts, lifted above the step components so switching steps never drops what was typed.
//
// A step reads and writes its fields through `useDraft` and registers how to save them with `useSaver`.
// The shell (setup-app.tsx) saves a step about a second after typing stops, on blur, and before it
// navigates away; missing values never block a save, malformed ones come back as field problems and stay
// in the draft. Non-secret drafts that are unsaved or have problems are mirrored to sessionStorage, so a
// reload keeps them. Secret fields are never stored in the browser.

import { createContext, useContext, useState } from "react";

import type { StepId } from "@/lib/api";

export type FieldProblem = { field: string; message: string };

export type StepDraft = {
  fields: Record<string, unknown>;
  secrets: string[]; // field names that hold secrets (never persisted)
  dirty: boolean;
  rev: number;
  problems: FieldProblem[];
};

export type Saver = () => Promise<FieldProblem[]>;

type Store = {
  drafts: Partial<Record<StepId, StepDraft>>;
  setField: (step: StepId, key: string, value: unknown, opts?: { secret?: boolean; silent?: boolean }) => void;
  savers: Map<StepId, Saver>;
  flush: (step: StepId) => Promise<void>;
};

const StoreContext = createContext<Store | null>(null);

export function StoreProvider({ value, children }: { value: Store; children: React.ReactNode }) {
  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

function useStore(): Store {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useDraft needs the setup store");
  return store;
}

/**
 * A draft field of `step`, initialized from `init` the first time (or after the step's draft was dropped
 * because it was saved cleanly). `secret` fields save on blur or Next only, never while typing.
 */
export function useDraft<T>(
  step: StepId,
  key: string,
  init: () => T,
  opts: { secret?: boolean } = {},
): [T, (value: T) => void] {
  const store = useStore();
  const draft = store.drafts[step];
  const stored = draft && key in draft.fields ? (draft.fields[key] as T) : undefined;
  const [initial] = useState(init);
  const value = stored === undefined ? initial : stored;
  return [value, (next: T) => store.setField(step, key, next, { secret: opts.secret })];
}

/** The fields this step's draft holds now. */
export function useDraftFields(step: StepId): Record<string, unknown> {
  return useStore().drafts[step]?.fields || {};
}

/** Set a draft field without marking the step as changed (e.g. clearing a secret once it is saved). */
export function useQuietSet(step: StepId) {
  const store = useStore();
  return (key: string, value: unknown) => store.setField(step, key, value, { silent: true });
}

/** Register how this step saves its draft. Called on every render, so the shell always has the latest. */
export function useSaver(step: StepId, saver: Saver): void {
  useStore().savers.set(step, saver);
}

/** Save the step now (blur on a secret box, for instance). Resolves once the save is done. */
export function useFlush(step: StepId): () => Promise<void> {
  const store = useStore();
  return () => store.flush(step);
}

/** The step's field problems from its last save, and a lookup by field. */
export function useProblems(step: StepId): { problems: FieldProblem[]; error: (field: string) => string | undefined } {
  const problems = useStore().drafts[step]?.problems || [];
  return {
    problems,
    error: (field: string) =>
      problems
        .filter((p) => p.field === field)
        .map((p) => p.message)
        .join(" ") || undefined,
  };
}

// ---------- sessionStorage mirror (non-secret fields only) ----------

const SESSION_KEY = "nua.setup.drafts";

export function sessionKey(root: string): string {
  return `${SESSION_KEY}:${root}`;
}

export function loadSessionDrafts(root: string): Partial<Record<StepId, StepDraft>> {
  try {
    const raw = sessionStorage.getItem(sessionKey(root));
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Partial<Record<StepId, { fields: Record<string, unknown>; problems?: FieldProblem[] }>>;
    const out: Partial<Record<StepId, StepDraft>> = {};
    for (const [step, d] of Object.entries(parsed)) {
      if (d && typeof d.fields === "object") {
        out[step as StepId] = { fields: d.fields, secrets: [], dirty: true, rev: 1, problems: d.problems || [] };
      }
    }
    return out;
  } catch {
    return {};
  }
}

export function storeSessionDrafts(root: string, drafts: Partial<Record<StepId, StepDraft>>): void {
  try {
    const keep: Record<string, { fields: Record<string, unknown>; problems: FieldProblem[] }> = {};
    for (const [step, d] of Object.entries(drafts)) {
      if (!d || (!d.dirty && !d.problems.length)) continue;
      const fields = Object.fromEntries(
        Object.entries(d.fields).filter(([k]) => !d.secrets.includes(k) && !k.startsWith("secret:")),
      );
      keep[step] = { fields, problems: d.problems };
    }
    if (Object.keys(keep).length) sessionStorage.setItem(sessionKey(root), JSON.stringify(keep));
    else sessionStorage.removeItem(sessionKey(root));
  } catch {
    // storage blocked or full: drafts still live in memory
  }
}

// Client for the local API in nua/setup/server.py. Every call carries the per-run token from the link
// `nua setup` printed (?token=...), kept in sessionStorage so a reload without it still works.

import useSWR, { mutate as globalMutate } from "swr";

const TOKEN_KEY = "nua.setup.token";

export type Domain = {
  company: string;
  market: string;
  exploring: string;
  institution_examples: string;
  bot_name: string;
  scope: { line: string; party: string; segment: string };
  roles: Record<string, string>;
};

export type Person = {
  name: string | null;
  email: string | null;
  slack_id: string | null;
  github_login: string | null;
  circleback_key_name: string | null;
};

export type Pipeline = {
  timezone: string;
  internal_email_domains: string[];
  channels: { calls: string; ops: string; test: string };
  ingest_cutoff: string | null;
};

export type SetupConfig = {
  domain: Domain;
  people: Record<string, Person>;
  seats: string[];
  pipeline: Pipeline;
  examples: string[];
  personal_domains: string[];
};

export type StepId = "team" | "recorder" | "cloud" | "github" | "slack" | "deploy" | "golive";

export type Cloud = "gcp" | "aws";
export type Recorder = "granola" | "circleback" | "both";

export type Status = {
  steps: Record<StepId, boolean>;
  started?: Record<StepId, boolean>;
  cloud: Cloud;
  recorder: Recorder | null;
  root: string;
};

export type EnvStatus = {
  path: string;
  exists: boolean;
  keys: Record<string, boolean>;
  needed: { models: string[]; slack: string[]; github: string[]; recorder: string[] };
};

export type TfValue = string | number | boolean | string[] | null;

export type DeployInfo = {
  cloud: Cloud;
  recorder: Recorder | null;
  repo: string;
  tfvars_path: string;
  exists: boolean;
  fields: Record<string, TfValue>;
  read_only: string[];
  timezone: string;
  people: string[];
  receiver_url_override: string;
  aws_profile?: string;
  aws_ready: boolean;
};

export type ReceiverInfo = { url: string | null; source: string | null; how_to: string; detail?: string };

export type SlackInfo = {
  receiver_url: string | null;
  how_to: string;
  bot_name: string;
  channels: Pipeline["channels"];
  create_url?: string;
  create_url_no_events?: string;
  manifest_yaml?: string;
};

export type GithubStatus = {
  status: "idle" | "starting" | "waiting" | "installing" | "done" | "error";
  url?: string | null;
  html_url?: string;
  install_url?: string;
  installation_id?: string;
  error?: string;
  slug?: string;
  repo?: string;
  saved: { app: boolean; slug: string; installation_id: string };
  default_name?: string;
};

export type Check = { id: string; label: string; ok: boolean; fix: string; detail: string };

export type Checks = {
  cloud: Cloud;
  checks: Check[];
  deploy: { command: string; command_create?: string; doc: string; note?: string };
};

export type GoLive = {
  ingest_cutoff: string | null;
  schedules_on: boolean;
  timezone: string;
  recorder: Recorder | null;
  receiver_url: string | null;
  circleback_people: string[];
  cloud: Cloud;
  granola_url?: string;
  granola_curl?: string;
  circleback_urls?: Record<string, string>;
};

export class ApiError extends Error {
  problems: string[];
  status: number;
  constructor(status: number, problems: string[]) {
    super(problems.join("; "));
    this.status = status;
    this.problems = problems;
  }
}

export function token(): string {
  if (typeof window === "undefined") return "";
  const fromUrl = new URLSearchParams(window.location.search).get("token");
  try {
    if (fromUrl) sessionStorage.setItem(TOKEN_KEY, fromUrl);
    return fromUrl || sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return fromUrl || "";
  }
}

export function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  return fetch(`/api/${path}`, {
    method,
    headers: { "X-Nua-Token": token(), "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  }).then((r) =>
    r
      .json()
      .catch(() => ({ problems: [`${r.status} ${r.statusText}`] }))
      .then((data) => {
        if (!r.ok) {
          const problems = Array.isArray(data?.problems) ? data.problems : [`${r.status} ${r.statusText}`];
          return Promise.reject(new ApiError(r.status, problems));
        }
        return data as T;
      }),
  );
}

export function problemsOf(e: unknown): string[] {
  if (e instanceof ApiError) return e.problems;
  if (e instanceof Error) return [e.message];
  return ["Something went wrong"];
}

function fetcher<T>(path: string): Promise<T> {
  return api<T>(path);
}

export function useApi<T>(path: string | null, refreshInterval = 0) {
  return useSWR<T>(path, fetcher, { revalidateOnFocus: true, refreshInterval });
}

/** Re-fetch the live views that depend on files a person may edit by hand (not the slow checks). */
export function refreshLive(): Promise<unknown> {
  const live = ["status", "env", "deploy", "receiver", "slack", "golive", "github"];
  return globalMutate((key) => typeof key === "string" && live.includes(key));
}

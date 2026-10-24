"use client";

import { useEffect, useState } from "react";
import { Check, Moon, Sun, TriangleAlert } from "lucide-react";

import { cn } from "@/lib/cn";
import {
  ApiError,
  problemsOf,
  refreshLive,
  useApi,
  type DeployInfo,
  type EnvStatus,
  type GoLive,
  type SetupConfig,
  type Status,
  type StepId,
} from "@/lib/api";
import { loadSessionDrafts, StoreProvider, storeSessionDrafts, type Saver, type StepDraft } from "@/components/setup/store";
import { useTheme } from "@/components/theme-provider";
import { Button } from "@/components/ui/button";
import { List, ListItem } from "@/components/ui/list";
import { Progress } from "@/components/ui/progress";
import { ApertureMark, PageLoader } from "@/components/ui/spinner";
import { Text } from "@/components/ui/text";
import { Note } from "@/components/setup/common";
import type { SaveIndicator, StepNav } from "@/components/setup/frame";
import { StepCloud } from "@/components/setup/step-cloud";
import { StepDeploy } from "@/components/setup/step-deploy";
import { StepGithub } from "@/components/setup/step-github";
import { StepGoLive } from "@/components/setup/step-golive";
import { StepRecorder } from "@/components/setup/step-recorder";
import { StepSlack } from "@/components/setup/step-slack";
import { StepTeam } from "@/components/setup/step-team";

const STEPS: { id: StepId; label: string }[] = [
  { id: "team", label: "About your team" },
  { id: "recorder", label: "Recorder" },
  { id: "cloud", label: "Cloud" },
  { id: "github", label: "GitHub App" },
  { id: "slack", label: "Slack app" },
  { id: "deploy", label: "Deploy" },
  { id: "golive", label: "Go live" },
];

function stepFromHash(): StepId {
  if (typeof window === "undefined") return "team";
  const id = window.location.hash.replace(/^#/, "");
  return (STEPS.find((s) => s.id === id)?.id || "team") as StepId;
}

function ThemeButton() {
  const { theme, toggleTheme } = useTheme();
  return (
    <Button variant="ghost" iconOnly onClick={toggleTheme} aria-label="Toggle dark mode">
      {theme === "dark" && <Sun className="w-4 h-4" />}
      {theme !== "dark" && <Moon className="w-4 h-4" />}
    </Button>
  );
}

function Rail({
  active,
  done,
  started,
  warned,
  onPick,
}: {
  active: StepId;
  done: Record<StepId, boolean> | undefined;
  started: Record<StepId, boolean> | undefined;
  warned: Record<StepId, boolean>;
  onPick: (id: StepId) => void;
}) {
  const count = done ? STEPS.filter((s) => done[s.id]).length : 0;
  return (
    <nav aria-label="Setup steps" className="flex flex-col gap-4">
      <div className="flex flex-col gap-2 px-1">
        <div className="flex items-baseline justify-between">
          <Text size="xs" color="faint" weight="medium">
            Progress
          </Text>
          <Text size="xs" color="faint" mono>
            {count}/{STEPS.length}
          </Text>
        </div>
        <Progress value={count / STEPS.length} color="accent" aria-label={`${count} of ${STEPS.length} steps done`} />
      </div>
      <List gap="none" className="flex-row md:flex-col overflow-x-auto scrollbar-hide -mx-1 md:mx-0">
        {STEPS.map((s, i) => {
          const isActive = s.id === active;
          const isDone = Boolean(done?.[s.id]);
          const isStarted = !isDone && Boolean(started?.[s.id]);
          const isWarned = warned[s.id];
          return (
            <ListItem key={s.id} className="flex-none">
              <button
                type="button"
                onClick={() => onPick(s.id)}
                aria-current={isActive ? "step" : undefined}
                className={cn(
                  "w-full flex items-center gap-2.5 px-2 py-1.5 rounded-sm text-left cursor-pointer",
                  "transition-colors duration-[120ms] ease-out focus-visible:outline-none focus-visible:ring-nua",
                  isActive && "bg-bg-raised shadow-xs border border-rule",
                  !isActive && "border border-transparent hover:bg-bg-sunk",
                )}
              >
                <span
                  className={cn(
                    "w-5 h-5 rounded-full grid place-items-center flex-none text-xs font-medium",
                    isDone && "bg-accent text-accent-solid-text",
                    !isDone && isActive && "border border-accent text-accent-ink",
                    !isDone && !isActive && !isStarted && "border border-rule-strong text-ink-3",
                    !isDone && !isActive && isStarted && "border border-accent/60 border-dashed text-accent-ink bg-accent-soft",
                  )}
                >
                  {isDone && <Check className="w-3 h-3" strokeWidth={3} />}
                  {!isDone && i + 1}
                </span>
                <Text size="sm" weight={isActive ? "medium" : "normal"} color={isActive ? "default" : "muted"} className="whitespace-nowrap">
                  {s.label}
                </Text>
                {isWarned && (
                  <TriangleAlert className="w-3.5 h-3.5 text-warn flex-none" aria-label="needs a fix" />
                )}
              </button>
            </ListItem>
          );
        })}
      </List>
    </nav>
  );
}

export function SetupApp() {
  const [active, setActive] = useState<StepId>("team");
  const status = useApi<Status>("status", 5000);
  const config = useApi<SetupConfig>("config");
  const env = useApi<EnvStatus>("env");
  const deploy = useApi<DeployInfo>("deploy");
  const golive = useApi<GoLive>("golive");
  const [drafts, setDrafts] = useState<Partial<Record<StepId, StepDraft>>>({});
  const [restored, setRestored] = useState(false);
  const [savers] = useState(() => new Map<StepId, Saver>());
  const [pendingSeq, setPendingSeq] = useState(0);
  const [indicator, setIndicator] = useState<SaveIndicator>("idle");
  const [navigating, setNavigating] = useState(false);
  const root = status.data?.root;

  useEffect(() => {
    // the step lives in the URL hash so a reload keeps your place
    const sync = () => setActive(stepFromHash());
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  useEffect(() => {
    // on narrow screens the rail scrolls sideways: keep the current step in view
    const frame = requestAnimationFrame(() => {
      const item = document.querySelector<HTMLElement>('[aria-current="step"]')?.parentElement;
      const rail = item?.parentElement;
      if (item && rail && rail.scrollWidth > rail.clientWidth) rail.scrollLeft = item.offsetLeft - rail.offsetLeft - 8;
    });
    return () => cancelAnimationFrame(frame);
  }, [active]);

  useEffect(() => {
    // a reload brings back what was typed but not saved (non-secret fields only)
    if (!root || restored) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time restore from sessionStorage
    setDrafts((d) => ({ ...loadSessionDrafts(root), ...d }));
    setRestored(true);
  }, [root, restored]);

  useEffect(() => {
    if (root && restored) storeSessionDrafts(root, drafts);
  }, [root, restored, drafts]);

  useEffect(() => {
    // autosave about a second after typing stops
    if (!pendingSeq) return;
    const timer = setTimeout(() => {
      runSave(active);
    }, 1000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingSeq]);

  useEffect(() => {
    // warn before leaving only when something is not saved
    const unsaved = Object.values(drafts).some((d) => d && (d.dirty || d.problems.length));
    if (!unsaved && indicator !== "saving") return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [drafts, indicator]);

  useEffect(() => {
    // pick up hand edits to .env or tfvars when the person comes back to the tab or switches steps
    const onFocus = () => {
      if (document.visibilityState === "visible") refreshLive();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, []);

  function setField(step: StepId, key: string, value: unknown, opts: { secret?: boolean; silent?: boolean } = {}) {
    setDrafts((all) => {
      const cur = all[step] || { fields: {}, secrets: [], dirty: false, rev: 0, problems: [] };
      const secrets = opts.secret && !cur.secrets.includes(key) ? [...cur.secrets, key] : cur.secrets;
      return {
        ...all,
        [step]: {
          ...cur,
          fields: { ...cur.fields, [key]: value },
          secrets,
          dirty: opts.silent ? cur.dirty : true,
          rev: opts.silent ? cur.rev : cur.rev + 1,
        },
      };
    });
    if (!opts.silent && !opts.secret) {
      setIndicator("pending");
      setPendingSeq((n) => n + 1);
    }
  }

  function runSave(step: StepId): Promise<void> {
    const cur = drafts[step];
    const saver = savers.get(step);
    if (!cur || !cur.dirty || !saver) return Promise.resolve();
    const rev = cur.rev;
    setIndicator("saving");
    return saver()
      .then((problems) => {
        setDrafts((all) => {
          const now = all[step];
          if (!now) return all;
          return { ...all, [step]: { ...now, dirty: now.rev !== rev, problems } };
        });
        setIndicator(problems.length ? "problems" : "saved");
        refreshLive();
        config.mutate();
      })
      .catch((e) => {
        setDrafts((all) => {
          const now = all[step];
          if (!now) return all;
          return { ...all, [step]: { ...now, problems: [{ field: "", message: problemsOf(e).join(" ") }] } };
        });
        setIndicator("failed");
      });
  }

  function pick(id: StepId) {
    if (id === active) return;
    setNavigating(true);
    // save the step being left first; navigate whatever the outcome, the draft keeps the typed values
    runSave(active).finally(() => {
      setNavigating(false);
      setDrafts((all) => {
        const left = all[active];
        if (!left || left.dirty || left.problems.length) return all;
        // a cleanly saved draft is dropped, so the step reopens from the saved files
        const rest = { ...all };
        delete rest[active];
        return rest;
      });
      refreshLive();
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${id}`);
      setActive(id);
      setIndicator("idle");
      window.scrollTo({ top: 0 });
    });
  }

  const store = {
    drafts,
    setField,
    savers,
    flush: (step: StepId) => runSave(step),
  };

  const index = STEPS.findIndex((s) => s.id === active);
  const nav: StepNav = {
    step: active,
    index,
    total: STEPS.length,
    onNext: index < STEPS.length - 1 ? () => pick(STEPS[index + 1].id) : undefined,
    nextLabel: index < STEPS.length - 1 ? `Next: ${STEPS[index + 1].label}` : undefined,
    navigating,
    indicator,
  };
  const warned = Object.fromEntries(
    STEPS.map((s) => [s.id, Boolean(drafts[s.id]?.problems.length)]),
  ) as Record<StepId, boolean>;
  const error = status.error || config.error || env.error || deploy.error;
  const ready = config.data && env.data && deploy.data && golive.data && restored;
  const key = active;

  return (
    <div className="min-h-screen flex flex-col">
      <header className="h-14 px-4 sm:px-7 flex items-center gap-3 border-b border-rule bg-bg-raised">
        <ApertureMark className="w-6 h-6" />
        <Text size="base" weight="semibold">
          Nua Discovery setup
        </Text>
        {status.data?.root && (
          <Text size="xs" color="faint" mono className="hidden md:inline truncate min-w-0">
            {status.data.root}
          </Text>
        )}
        <div className="ml-auto flex items-center gap-1">
          <ThemeButton />
        </div>
      </header>
      <div className="flex-1 flex flex-col md:flex-row min-h-0">
        <aside className="md:w-60 flex-none border-b md:border-b-0 md:border-r border-rule bg-bg-sunk/40 px-3 sm:px-4 py-4 md:py-6 md:sticky md:top-0 md:self-start md:h-screen">
          <Rail
            active={active}
            done={status.data?.steps}
            started={status.data?.started}
            warned={warned}
            onPick={pick}
          />
        </aside>
        <main className="flex-1 min-w-0">
          {error && (
            <div className="px-4 sm:px-7 py-6 max-w-3xl">
              {error instanceof ApiError && error.status === 401 && (
                <Note
                  variant="danger"
                  title="This link has no valid token"
                  description="Each run of nua setup makes a new one. Open the link the terminal printed."
                />
              )}
              {!(error instanceof ApiError && error.status === 401) && (
                <Note
                  variant="danger"
                  title="Cannot reach the setup server"
                  description={`${error.message}. Is nua setup still running in your terminal?`}
                />
              )}
            </div>
          )}
          {!error && !ready && <PageLoader label="Reading your config..." />}
          {!error && ready && (
            <StoreProvider value={store}>
              {active === "team" && <StepTeam key={key} config={config.data!} env={env.data!} nav={nav} />}
              {active === "recorder" && (
                <StepRecorder key={key} config={config.data!} deploy={deploy.data!} env={env.data!} nav={nav} />
              )}
              {active === "cloud" && <StepCloud key={key} deploy={deploy.data!} nav={nav} />}
              {active === "github" && (
                <StepGithub key={key} deploy={deploy.data!} nav={nav} onSaved={refreshLive} onCheck={refreshLive} />
              )}
              {active === "slack" && <StepSlack key={key} config={config.data!} env={env.data!} nav={nav} />}
              {active === "deploy" && <StepDeploy key={key} nav={nav} />}
              {active === "golive" && <StepGoLive key={key} live={golive.data!} env={env.data!} nav={nav} />}
            </StoreProvider>
          )}
        </main>
      </div>
    </div>
  );
}

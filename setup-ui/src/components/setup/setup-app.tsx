"use client";

import { useEffect, useState } from "react";
import { Check, Moon, Sun } from "lucide-react";

import { cn } from "@/lib/cn";
import { ApiError, useApi, type DeployInfo, type EnvStatus, type SetupConfig, type Status, type StepId } from "@/lib/api";
import { useTheme } from "@/components/theme-provider";
import { Button } from "@/components/ui/button";
import { List, ListItem } from "@/components/ui/list";
import { Progress } from "@/components/ui/progress";
import { ApertureMark, PageLoader } from "@/components/ui/spinner";
import { Text } from "@/components/ui/text";
import { Note } from "@/components/setup/common";
import type { StepNav } from "@/components/setup/frame";
import { StepCloud } from "@/components/setup/step-cloud";
import { StepDeploy } from "@/components/setup/step-deploy";
import { StepDiscovery } from "@/components/setup/step-discovery";
import { StepGithub } from "@/components/setup/step-github";
import { StepGoLive } from "@/components/setup/step-golive";
import { StepModels } from "@/components/setup/step-models";
import { StepRecorder } from "@/components/setup/step-recorder";
import { StepSeats } from "@/components/setup/step-seats";
import { StepSlack } from "@/components/setup/step-slack";
import { StepTeam } from "@/components/setup/step-team";

const STEPS: { id: StepId; label: string }[] = [
  { id: "discovery", label: "Your discovery" },
  { id: "team", label: "Team" },
  { id: "seats", label: "Seats" },
  { id: "recorder", label: "Recorder" },
  { id: "cloud", label: "Cloud" },
  { id: "github", label: "GitHub App" },
  { id: "slack", label: "Slack app" },
  { id: "models", label: "Model keys" },
  { id: "deploy", label: "Deploy" },
  { id: "golive", label: "Go live" },
];

function stepFromHash(): StepId {
  if (typeof window === "undefined") return "discovery";
  const id = window.location.hash.replace(/^#/, "");
  return (STEPS.find((s) => s.id === id)?.id || "discovery") as StepId;
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
  onPick,
}: {
  active: StepId;
  done: Record<StepId, boolean> | undefined;
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
                    !isDone && !isActive && "border border-rule-strong text-ink-3",
                  )}
                >
                  {isDone && <Check className="w-3 h-3" strokeWidth={3} />}
                  {!isDone && i + 1}
                </span>
                <Text size="sm" weight={isActive ? "medium" : "normal"} color={isActive ? "default" : "muted"} className="whitespace-nowrap">
                  {s.label}
                </Text>
              </button>
            </ListItem>
          );
        })}
      </List>
    </nav>
  );
}

export function SetupApp() {
  const [active, setActive] = useState<StepId>("discovery");
  const [version, setVersion] = useState(0);
  const status = useApi<Status>("status", 5000);
  const config = useApi<SetupConfig>("config");
  const env = useApi<EnvStatus>("env");
  const deploy = useApi<DeployInfo>("deploy");

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

  function pick(id: StepId) {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${id}`);
    setActive(id);
    window.scrollTo({ top: 0 });
  }

  function refresh() {
    // remount the step only once fresh data is in, so its form starts from what was saved
    Promise.all([status.mutate(), config.mutate(), env.mutate(), deploy.mutate()]).finally(() =>
      setVersion((v) => v + 1),
    );
  }

  function onConfigSaved(next: SetupConfig) {
    config.mutate(next, { revalidate: false });
    status.mutate();
    env.mutate();
    deploy.mutate();
    setVersion((v) => v + 1);
  }

  const index = STEPS.findIndex((s) => s.id === active);
  const nav: StepNav = {
    index,
    total: STEPS.length,
    onNext: index < STEPS.length - 1 ? () => pick(STEPS[index + 1].id) : undefined,
    nextLabel: index < STEPS.length - 1 ? `Next: ${STEPS[index + 1].label}` : undefined,
  };
  const error = status.error || config.error || env.error || deploy.error;
  const ready = config.data && env.data && deploy.data;
  const key = `${active}-${version}`;

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
          <Rail active={active} done={status.data?.steps} onPick={pick} />
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
          {!error && ready && active === "discovery" && (
            <StepDiscovery key={key} config={config.data!} nav={nav} onSaved={onConfigSaved} />
          )}
          {!error && ready && active === "team" && (
            <StepTeam key={key} config={config.data!} nav={nav} onSaved={onConfigSaved} />
          )}
          {!error && ready && active === "seats" && (
            <StepSeats key={key} config={config.data!} nav={nav} onSaved={onConfigSaved} />
          )}
          {!error && ready && active === "recorder" && (
            <StepRecorder key={key} config={config.data!} deploy={deploy.data!} env={env.data!} nav={nav} onSaved={refresh} />
          )}
          {!error && ready && active === "cloud" && <StepCloud key={key} deploy={deploy.data!} nav={nav} onSaved={refresh} />}
          {!error && ready && active === "github" && <StepGithub key={key} deploy={deploy.data!} nav={nav} onSaved={refresh} />}
          {!error && ready && active === "slack" && <StepSlack key={key} env={env.data!} nav={nav} onSaved={refresh} />}
          {!error && ready && active === "models" && <StepModels key={key} env={env.data!} nav={nav} onSaved={refresh} />}
          {!error && ready && active === "deploy" && <StepDeploy key={key} nav={nav} />}
          {!error && ready && active === "golive" && <StepGoLive key={key} env={env.data!} nav={nav} onSaved={refresh} />}
        </main>
      </div>
    </div>
  );
}

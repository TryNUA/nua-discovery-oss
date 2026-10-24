"use client";

import { ArrowRight, CircleCheck, TriangleAlert } from "lucide-react";

import type { StepId } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { List, ListItem } from "@/components/ui/list";
import { Spinner } from "@/components/ui/spinner";
import { Text } from "@/components/ui/text";
import { useProblems } from "@/components/setup/store";

export type SaveIndicator = "idle" | "pending" | "saving" | "saved" | "problems" | "failed";

export type StepNav = {
  step: StepId;
  index: number;
  total: number;
  onNext?: () => void;
  nextLabel?: string;
  navigating?: boolean;
  indicator?: SaveIndicator;
};

type FrameProps = {
  nav: StepNav;
  title: string;
  intro: React.ReactNode;
  children: React.ReactNode;
  /** Step actions that are not saving (Create the GitHub App, Check installation). */
  aside?: React.ReactNode;
};

function Indicator({ state }: { state: SaveIndicator }) {
  if (state === "saving" || state === "pending") {
    return (
      <span className="inline-flex items-center gap-1.5">
        <Spinner size="sm" />
        <Text size="xs" color="faint">
          {state === "saving" && "Saving..."}
          {state === "pending" && "Changes not saved yet"}
        </Text>
      </span>
    );
  }
  if (state === "saved") {
    return (
      <span className="inline-flex items-center gap-1.5">
        <CircleCheck className="w-3.5 h-3.5 text-success" aria-hidden />
        <Text size="xs" color="faint">
          Saved
        </Text>
      </span>
    );
  }
  if (state === "problems" || state === "failed") {
    return (
      <span className="inline-flex items-center gap-1.5">
        <TriangleAlert className="w-3.5 h-3.5 text-warn" aria-hidden />
        <Text size="xs" color="muted">
          {state === "problems" && "Saved, except the fields marked in red"}
          {state === "failed" && "Not saved. Your changes are kept here; it retries when you continue"}
        </Text>
      </span>
    );
  }
  return (
    <Text size="xs" color="faint">
      Changes save as you go
    </Text>
  );
}

/** One step: header, body sections, and a footer with the save state and Next. Saving is automatic. */
export function StepFrame({ nav, title, intro, children, aside }: FrameProps) {
  const { problems } = useProblems(nav.step);
  return (
    <div className="flex flex-col min-h-full">
      <div className="px-4 sm:px-7 pt-6 pb-5 border-b border-rule">
        <div className="max-w-3xl flex flex-col gap-1.5">
          <Text size="xs" color="faint" weight="medium" className="uppercase tracking-wide">
            Step {nav.index + 1} of {nav.total}
          </Text>
          <Text as="h1" size="h2" weight="semibold">
            {title}
          </Text>
          <Text size="sm" color="muted" leading="relaxed">
            {intro}
          </Text>
        </div>
      </div>
      <div className="px-4 sm:px-7 flex-1">
        <div className="max-w-3xl flex flex-col animate-rise-in">{children}</div>
      </div>
      <div className="sticky bottom-0 px-4 sm:px-7 py-3 border-t border-rule bg-bg/95 backdrop-blur">
        <div className="max-w-3xl flex flex-col gap-2">
          {problems.length > 0 && (
            <List gap="xs" role="alert">
              {problems.map((p) => (
                <ListItem key={`${p.field}-${p.message}`} align="start" gap="md">
                  <Text size="xs" color="danger">
                    {p.message}
                  </Text>
                </ListItem>
              ))}
            </List>
          )}
          <div className="flex items-center gap-2 flex-wrap min-h-9">
            {aside}
            <Indicator state={problems.length && (nav.indicator || "idle") === "idle" ? "problems" : nav.indicator || "idle"} />
            {nav.onNext && (
              <Button onClick={nav.onNext} loading={nav.navigating} className="ml-auto">
                {nav.nextLabel || "Next"}
                <ArrowRight className="w-4 h-4" />
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

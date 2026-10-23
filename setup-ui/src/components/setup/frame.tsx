"use client";

import { ArrowRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Text } from "@/components/ui/text";
import { Problems } from "@/components/setup/common";

export type StepNav = {
  index: number;
  total: number;
  onNext?: () => void;
  nextLabel?: string;
};

type FrameProps = {
  nav: StepNav;
  title: string;
  intro: React.ReactNode;
  children: React.ReactNode;
  problems?: string[];
  onSave?: () => void;
  saving?: boolean;
  saveLabel?: string;
  saveDisabled?: boolean;
  aside?: React.ReactNode;
};

/** One step: header, body sections, and a footer with Save and Next. */
export function StepFrame({
  nav,
  title,
  intro,
  children,
  problems = [],
  onSave,
  saving,
  saveLabel = "Save",
  saveDisabled,
  aside,
}: FrameProps) {
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
        <div className="max-w-3xl flex flex-col gap-3">
          <Problems problems={problems} />
          <div className="flex items-center gap-2 flex-wrap">
            {onSave && (
              <Button onClick={onSave} loading={saving} disabled={saveDisabled}>
                {saveLabel}
              </Button>
            )}
            {aside}
            {nav.onNext && (
              <Button variant="ghost" onClick={nav.onNext} className="ml-auto">
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

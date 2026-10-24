"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/cn";
import { Text } from "@/components/ui/text";

export type CollapsibleSectionProps = {
  title: React.ReactNode;
  /** Extra inline content beside the title (counts, badges). */
  meta?: React.ReactNode;
  /** Interactive content pinned right of the header, OUTSIDE the toggle , 
   *  buttons here don't fight the disclosure click. */
  actions?: React.ReactNode;
  defaultOpen?: boolean;
  /** "xs" renders a quiet, transcript-weight header (default "sm"). */
  titleSize?: "sm" | "xs";
  disabled?: boolean;
  children: React.ReactNode;
  className?: string;
};

/**
 * Disclosure section with a SMOOTH height animation: the panel is a CSS grid
 * whose row animates 0fr → 1fr, which transitions to auto height with no JS
 * measurement, content slides open instead of jutting into place. Content
 * stays mounted, so inner state (pagination, players) survives toggling.
 * Under reduced motion the panel snaps (transition-none) but still fades.
 */
export function CollapsibleSection({
  title,
  meta,
  actions,
  defaultOpen = false,
  titleSize = "sm",
  disabled = false,
  children,
  className,
}: CollapsibleSectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={cn("flex flex-col", className)}>
      <div className="flex items-center justify-between gap-3">
        <button
          type="button"
          onClick={() => !disabled && setOpen((v) => !v)}
          aria-expanded={open}
          disabled={disabled}
          className="group/coll flex items-center gap-2 text-left cursor-pointer select-none rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-default"
        >
          <ChevronRight
            className={cn(
              titleSize === "xs" ? "w-3.5 h-3.5" : "w-4 h-4",
              "text-ink-4 transition-transform duration-200 ease-out",
              "group-hover/coll:text-ink-2",
              open && "rotate-90",
              disabled && "opacity-0",
            )}
          />
          {titleSize === "sm" && (
            <Text size="sm" weight="medium">
              {title}
            </Text>
          )}
          {titleSize === "xs" && (
            <Text size="xs" mono color="faint">
              {title}
            </Text>
          )}
          {meta}
        </button>
        {actions && <span className="shrink-0">{actions}</span>}
      </div>
      <div
        className={cn(
          "grid transition-[grid-template-rows,opacity] duration-300",
          "ease-[cubic-bezier(0.23,1,0.32,1)] motion-reduce:transition-none",
          open && "grid-rows-[1fr] opacity-100",
          !open && "grid-rows-[0fr] opacity-0",
        )}
      >
        <div className="overflow-hidden min-h-0">
          <div className="pt-3">{children}</div>
        </div>
      </div>
    </div>
  );
}

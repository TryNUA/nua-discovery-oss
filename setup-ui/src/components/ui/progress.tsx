"use client";

import * as ProgressPrimitive from "@radix-ui/react-progress";
import { cn } from "@/lib/cn";
import { forwardRef } from "react";

export type ProgressProps = React.ComponentPropsWithoutRef<typeof ProgressPrimitive.Root> & {
  value?: number;
  color?: "default" | "accent" | "warn" | "danger";
  indeterminate?: boolean;
};

const Progress = forwardRef<
  React.ComponentRef<typeof ProgressPrimitive.Root>,
  ProgressProps
>(({ className, value, color = "default", indeterminate, ...props }, ref) => {
  const fillColor = {
    default: "bg-ink",
    accent: "bg-accent",
    warn: "bg-warn",
    danger: "bg-danger",
  }[color];

  return (
    <ProgressPrimitive.Root
      ref={ref}
      className={cn(
        "h-1.5 bg-bg-inset rounded-full overflow-hidden relative",
        className
      )}
      {...props}
    >
      <ProgressPrimitive.Indicator
        className={cn(
          "h-full rounded-full transition-[width] duration-300 ease-out",
          fillColor,
          indeterminate && "!w-[40%] animate-indeterminate"
        )}
        style={
          indeterminate ? undefined : { width: `${Math.round((value ?? 0) * 100)}%` }
        }
      />
    </ProgressPrimitive.Root>
  );
});
Progress.displayName = "Progress";

export { Progress };

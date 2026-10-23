"use client";

import * as CheckboxPrimitive from "@radix-ui/react-checkbox";
import { Check, Minus } from "lucide-react";
import { cn } from "@/lib/cn";
import { forwardRef } from "react";

export interface CheckboxProps
  extends React.ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root> {
  label?: string;
}

const Checkbox = forwardRef<
  React.ComponentRef<typeof CheckboxPrimitive.Root>,
  CheckboxProps
>(({ className, label, ...props }, ref) => {
  const checkbox = (
    <CheckboxPrimitive.Root
      ref={ref}
      className={cn(
        "group/cb w-4 h-4 rounded-xs flex-none",
        "border border-rule-strong bg-bg-raised",
        "inline-grid place-items-center cursor-pointer",
        "transition-all duration-[120ms] ease-out",
        "hover:border-ink-3",
        "focus-visible:outline-none focus-visible:ring-nua",
        "data-[state=checked]:bg-ink data-[state=checked]:border-ink",
        "data-[state=indeterminate]:bg-ink data-[state=indeterminate]:border-ink",
        "[data-theme=dark]_&:data-[state=checked]:bg-accent [data-theme=dark]_&:data-[state=checked]:border-accent",
        "[data-theme=dark]_&:data-[state=indeterminate]:bg-accent [data-theme=dark]_&:data-[state=indeterminate]:border-accent",
        "disabled:opacity-50 disabled:pointer-events-none",
        className
      )}
      {...props}
    >
      {/* Radix mounts the indicator for checked AND indeterminate; the state
          on the root picks which glyph shows. */}
      <CheckboxPrimitive.Indicator>
        <Check
          className="w-[9px] h-[9px] text-white group-data-[state=indeterminate]/cb:hidden"
          strokeWidth={3}
        />
        <Minus
          className="w-[9px] h-[9px] text-white hidden group-data-[state=indeterminate]/cb:block"
          strokeWidth={3}
        />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );

  if (label) {
    return (
      <label className="inline-flex items-center gap-2 cursor-pointer text-sm text-ink">
        {checkbox}
        {label}
      </label>
    );
  }

  return checkbox;
});
Checkbox.displayName = "Checkbox";

export { Checkbox };

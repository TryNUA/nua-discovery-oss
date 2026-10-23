"use client";

import * as SwitchPrimitive from "@radix-ui/react-switch";
import { cn } from "@/lib/cn";
import { forwardRef } from "react";

export interface SwitchProps
  extends React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root> {
  label?: string;
}

const Switch = forwardRef<
  React.ComponentRef<typeof SwitchPrimitive.Root>,
  SwitchProps
>(({ className, label, ...props }, ref) => {
  const toggle = (
    <SwitchPrimitive.Root
      ref={ref}
      className={cn(
        "w-[34px] h-5 rounded-full relative cursor-pointer flex-none",
        "bg-rule border-none p-0",
        "transition-colors duration-150 ease-out",
        "data-[state=checked]:bg-accent",
        "focus-visible:outline-none focus-visible:ring-nua",
        "disabled:opacity-50 disabled:pointer-events-none",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          "block w-4 h-4 rounded-full bg-white shadow-sm",
          "transition-transform duration-[180ms] cubic-bezier(0.4,0.1,0.2,1)",
          "translate-x-0.5 data-[state=checked]:translate-x-[14px]"
        )}
      />
    </SwitchPrimitive.Root>
  );

  if (label) {
    return (
      <label className="inline-flex items-center justify-between gap-2 cursor-pointer text-sm text-ink w-[220px]">
        {label}
        {toggle}
      </label>
    );
  }

  return toggle;
});
Switch.displayName = "Switch";

export { Switch };

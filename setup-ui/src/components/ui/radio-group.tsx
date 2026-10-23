"use client";

import * as RadioGroupPrimitive from "@radix-ui/react-radio-group";
import { cn } from "@/lib/cn";
import { forwardRef } from "react";

const RadioGroup = forwardRef<
  React.ComponentRef<typeof RadioGroupPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Root>
>(({ className, ...props }, ref) => (
  <RadioGroupPrimitive.Root
    ref={ref}
    className={cn("flex flex-col gap-2.5", className)}
    {...props}
  />
));
RadioGroup.displayName = "RadioGroup";

export interface RadioGroupItemProps
  extends React.ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Item> {
  label?: string;
}

const RadioGroupItem = forwardRef<
  React.ComponentRef<typeof RadioGroupPrimitive.Item>,
  RadioGroupItemProps
>(({ className, label, ...props }, ref) => {
  const radio = (
    <RadioGroupPrimitive.Item
      ref={ref}
      className={cn(
        "w-4 h-4 rounded-full flex-none",
        "border border-rule-strong bg-bg-raised",
        "inline-grid place-items-center cursor-pointer",
        "transition-all duration-[120ms] ease-out",
        "hover:border-ink-3",
        "focus-visible:outline-none focus-visible:ring-nua",
        "data-[state=checked]:border-ink data-[state=checked]:border-[5px]",
        "disabled:opacity-50 disabled:pointer-events-none",
        className
      )}
      {...props}
    >
      <RadioGroupPrimitive.Indicator />
    </RadioGroupPrimitive.Item>
  );

  if (label) {
    return (
      <label className="inline-flex items-center gap-2 cursor-pointer text-sm text-ink">
        {radio}
        {label}
      </label>
    );
  }

  return radio;
});
RadioGroupItem.displayName = "RadioGroupItem";

export { RadioGroup, RadioGroupItem };

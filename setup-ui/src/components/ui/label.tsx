import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

/**
 * Form label. Pair with a control via the standard `htmlFor` attribute.
 * The visual scale matches `Text`'s `sm` (form caption) by default; bump to
 * `base` for primary form labels.
 */
const labelVariants = cva("font-sans block text-ink", {
  variants: {
    size: {
      xs: "text-xs",
      sm: "text-sm",
      md: "text-base",
      base: "text-base",
    },
    weight: {
      normal: "font-normal",
      medium: "font-medium",
      semibold: "font-semibold",
    },
  },
  defaultVariants: {
    size: "sm",
    weight: "medium",
  },
});

export interface LabelProps
  extends React.LabelHTMLAttributes<HTMLLabelElement>,
    VariantProps<typeof labelVariants> {}

export function Label({ size, weight, className, ...props }: LabelProps) {
  return (
    <label className={cn(labelVariants({ size, weight }), className)} {...props} />
  );
}

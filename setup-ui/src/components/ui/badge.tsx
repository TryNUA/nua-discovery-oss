import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

const badgeVariants = cva(
  [
    "inline-flex items-center gap-1.5",
    "h-[22px] px-2 rounded-full",
    "text-xs font-medium tracking-[0.01em]",
    "border border-transparent whitespace-nowrap",
  ],
  {
    variants: {
      variant: {
        soft: "",
        solid: "",
        outline: "bg-transparent !border-current",
      },
      color: {
        neutral: "",
        success: "",
        danger: "",
        warn: "",
        info: "",
      },
    },
    compoundVariants: [
      // Soft
      { variant: "soft", color: "neutral", class: "bg-bg-sunk text-ink-2 border-rule" },
      { variant: "soft", color: "success", class: "bg-accent-soft text-accent-soft-ink" },
      { variant: "soft", color: "danger", class: "bg-danger-soft text-danger-ink" },
      { variant: "soft", color: "warn", class: "bg-warn-soft text-warn-ink" },
      { variant: "soft", color: "info", class: "bg-info-soft text-info-ink" },
      // Solid
      { variant: "solid", color: "neutral", class: "bg-ink text-bg" },
      { variant: "solid", color: "success", class: "bg-accent text-white" },
      { variant: "solid", color: "danger", class: "bg-danger text-white" },
      { variant: "solid", color: "warn", class: "bg-warn text-[oklch(0.2_0.01_70)]" },
      { variant: "solid", color: "info", class: "bg-info text-white" },
      // Outline
      { variant: "outline", color: "neutral", class: "text-ink-2 !border-rule-strong" },
      { variant: "outline", color: "success", class: "text-accent-ink !border-accent" },
      { variant: "outline", color: "danger", class: "text-danger-ink !border-danger" },
    ],
    defaultVariants: {
      variant: "soft",
      color: "neutral",
    },
  }
);

export interface BadgeProps
  extends Omit<React.HTMLAttributes<HTMLSpanElement>, "color">,
    VariantProps<typeof badgeVariants> {
  dot?: boolean;
}

export function Badge({
  className,
  variant,
  color,
  dot,
  children,
  ...props
}: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ variant, color }), className)} {...props}>
      {dot && <span className="w-1.5 h-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

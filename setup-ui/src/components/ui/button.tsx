import { forwardRef } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";
import { ApertureMark } from "@/components/ui/spinner";

const buttonVariants = cva(
  [
    "relative inline-flex items-center justify-center gap-2",
    "font-sans text-sm font-medium tracking-[-0.003em] whitespace-nowrap select-none",
    "rounded-sm border border-transparent",
    "transition-[background,color,border-color,box-shadow] duration-[120ms] ease-out",
    "cursor-pointer",
    "focus-visible:outline-none focus-visible:ring-nua",
    "disabled:opacity-45 disabled:pointer-events-none",
  ],
  {
    variants: {
      variant: {
        primary: [
          "bg-accent text-accent-solid-text border-accent",
          "hover:[background:oklch(from_var(--accent)_calc(l-0.04)_c_h)]",
          "active:translate-y-[0.5px] active:[background:oklch(from_var(--accent)_calc(l-0.07)_c_h)]",
        ],
        secondary: [
          "bg-bg-raised text-ink border-rule shadow-xs",
          "hover:bg-bg-sunk hover:border-rule-strong",
          "active:bg-bg-inset active:translate-y-[0.5px]",
        ],
        ghost: [
          "bg-transparent text-ink-2",
          "hover:bg-bg-sunk hover:text-ink",
          "active:bg-bg-inset",
        ],
        // Bordered but flat, for icon actions that should read as buttons at
        // rest (unlike ghost) without the raised weight of secondary.
        outline: [
          "bg-transparent text-ink-2 border-rule",
          "hover:bg-bg-sunk hover:text-ink hover:border-rule-strong",
          "active:bg-bg-inset active:translate-y-[0.5px]",
        ],
        // Quiet-until-hovered destructive affordance for icon buttons in data
        // rows: neutral at rest, unmistakably red on hover/focus. Pair with
        // opacity-0 group-hover/row reveal for tables (see TableRow's group).
        "ghost-danger": [
          "bg-transparent text-ink-3",
          "hover:bg-danger-soft hover:text-danger-ink",
          "active:bg-danger-soft active:translate-y-[0.5px]",
          "focus-visible:ring-nua-danger",
        ],
        destructive: [
          "bg-danger text-white border-danger",
          "hover:[background:oklch(from_var(--danger)_calc(l-0.04)_c_h)]",
          "active:translate-y-[0.5px]",
          "focus-visible:ring-nua-danger",
        ],
      },
      size: {
        sm: "h-7 px-2.5 text-sm rounded-sm",
        md: "h-9 px-3.5",
        lg: "h-[42px] px-[18px] text-base",
      },
      iconOnly: {
        true: "!px-0",
      },
    },
    compoundVariants: [
      { iconOnly: true, size: "sm", class: "w-7" },
      { iconOnly: true, size: "md", class: "w-9" },
      { iconOnly: true, size: "lg", class: "w-[42px]" },
    ],
    defaultVariants: {
      variant: "primary",
      size: "md",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  loading?: boolean;
  success?: boolean;
  error?: boolean;
  kbd?: string;
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      className,
      variant,
      size,
      iconOnly,
      loading,
      success,
      error,
      kbd,
      disabled,
      children,
      ...props
    },
    ref
  ) => {
    return (
      <button
        ref={ref}
        className={cn(
          buttonVariants({ variant, size, iconOnly }),
          // Loading is an ACTIVE state, not a disabled one: keep the button at
          // full strength (disabled:opacity-100 out-merges the base opacity-45,
          // which washed the spinner to near-invisible in light mode) and hide
          // only the label under the spinner.
          loading && "!text-transparent pointer-events-none disabled:opacity-100",
          success && "!bg-accent !text-white !border-accent",
          error && "!border-danger !text-danger-ink",
          className
        )}
        disabled={disabled || loading}
        {...props}
      >
        {children}
        {kbd && (
          <span className="text-xs text-ink-3 px-[5px] py-px border border-rule rounded-xs ml-0.5">
            {kbd}
          </span>
        )}
        {loading && (
          // `variant` is the raw prop, undefined when the caller relies on the
          // cva default, so resolve it before picking the spinner color (an
          // unresolved undefined fell through to ink: a black spinner on the
          // navy primary button).
          <span
            className="ap-chase absolute inset-0 grid place-items-center"
            style={{
              color:
                (variant ?? "primary") === "primary"
                  ? "var(--accent-solid-text)"
                  : variant === "destructive"
                    ? "white"
                    : "var(--ink)",
            }}
          >
            <ApertureMark mono className="w-4 h-4" />
          </span>
        )}
      </button>
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };

import { CircleAlert, CircleCheck, Info, TriangleAlert } from "lucide-react";

import { cn } from "@/lib/cn";

export interface BannerProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: "default" | "success" | "danger" | "warn";
  orientation?: "horizontal" | "vertical";
  title: string;
  description?: string;
  actions?: React.ReactNode;
}

/* Status is carried by the edge bar + icon tile, NOT by flooding the surface , 
   the banner stays a neutral raised card so it sits calmly on the canvas next
   to tables and forms instead of shouting in pastel. */
const edgeBar = {
  default: "bg-rule-strong",
  success: "bg-accent",
  danger: "bg-danger",
  warn: "bg-warn",
};

const iconTile = {
  default: "bg-bg-sunk text-ink-3",
  success: "bg-accent-soft text-accent-soft-ink",
  danger: "bg-danger-soft text-danger-ink",
  warn: "bg-warn-soft text-warn-ink",
};

const GLYPH = {
  default: Info,
  success: CircleCheck,
  danger: CircleAlert,
  warn: TriangleAlert,
};

export function Banner({
  className,
  variant = "default",
  orientation = "horizontal",
  title,
  description,
  actions,
  ...props
}: BannerProps) {
  const vertical = orientation === "vertical";
  const Glyph = GLYPH[variant];
  return (
    <div
      className={cn(
        "relative overflow-hidden flex gap-3 pl-4 pr-3.5 py-3",
        "rounded-md border border-rule bg-bg-raised shadow-xs text-sm",
        vertical && "flex-col items-start",
        !vertical && "items-center",
        className,
      )}
      {...props}
    >
      <span
        aria-hidden
        className={cn("absolute left-0 inset-y-0 w-[3px]", edgeBar[variant])}
      />
      <span
        className={cn(
          "w-7 h-7 rounded-sm grid place-items-center flex-none",
          iconTile[variant],
        )}
      >
        <Glyph className="w-4 h-4" />
      </span>
      <span className="font-medium text-ink flex-none">{title}</span>
      {description && (
        <span className={cn("text-ink-2", !vertical && "min-w-0 truncate")}>
          {description}
        </span>
      )}
      {actions && (
        <span className={cn("flex gap-1.5 flex-none", !vertical && "ml-auto")}>
          {actions}
        </span>
      )}
    </div>
  );
}

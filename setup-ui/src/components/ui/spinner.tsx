import { cn } from "@/lib/cn";
import { Text } from "@/components/ui/text";

// Blade geometry lifted from the brand mark (see icons/nua-logo.tsx, minus the
// tile): five aperture blades around a hollow hub, listed clockwise from the
// accent blade so staggered cycles travel around the ring. The accent blade
// keeps its "lifted proud of the wheel" offset via `lift`.
type ApertureBlade = { d: string; lift?: string; accent?: boolean };

const APERTURE_BLADES: ApertureBlade[] = [
  {
    d: "M 48.43 5.03 A 45 45 0 0 1 88.16 26.15 L 62.72 42.05 A 15 15 0 0 0 49.48 35.01 Z",
    lift: "translate(2.82 -5.30)",
    accent: true,
  },
  { d: "M 89.47 35.64 A 42 42 0 0 1 82.17 77.00 L 61.49 59.64 A 15 15 0 0 0 64.10 44.87 Z" },
  { d: "M 75.86 83.10 A 42 42 0 0 1 34.27 88.94 L 44.38 63.91 A 15 15 0 0 0 59.23 61.82 Z" },
  { d: "M 26.51 84.82 A 42 42 0 0 1 8.10 47.07 L 35.04 48.95 A 15 15 0 0 0 41.61 62.44 Z" },
  { d: "M 9.63 38.42 A 42 42 0 0 1 39.84 9.25 L 46.37 35.45 A 15 15 0 0 0 35.58 45.87 Z" },
];

export type ApertureMarkProps = {
  className?: string;
  /**
   * Render in currentColor (for inline spinners that must sit on any surface)
   * instead of the pinned brand palette. The accent blade stays full-strength
   * while the others dim, so rotation and chase cycles stay legible.
   */
  mono?: boolean;
};

/**
 * The bare aperture mark. Carries NO animation of its own, wrap it in one of
 * the behavior classes from globals.css (`ap-loader [ap-iris|ap-glow|ap-tick]`,
 * `ap-spin`, `ap-chase`) to bring it to life.
 */
export function ApertureMark({ className, mono }: ApertureMarkProps) {
  return (
    <svg viewBox="0 0 100 100" aria-hidden className={cn("ap-mark", className)}>
      {APERTURE_BLADES.map((blade, i) => (
        <g
          key={blade.d}
          className="ap-blade"
          style={{ "--ap-i": i } as React.CSSProperties}
        >
          <g className="ap-blade-inner">
            <path
              d={blade.d}
              transform={blade.lift}
              className={cn(
                !mono && "fill-[#0d1320] dark:fill-[#ededed]",
                !mono && blade.accent && "fill-[#1e3a8a] dark:fill-[#8fb0fb]",
                mono && "fill-current",
                mono && !blade.accent && "opacity-40",
              )}
            />
          </g>
        </g>
      ))}
    </svg>
  );
}

const sizes = {
  sm: "w-3.5 h-3.5",
  md: "w-5 h-5",
  lg: "w-8 h-8",
  xl: "w-11 h-11",
} as const;

export type SpinnerVariant = "spin" | "chase" | "orbit";

const SPINNER_VARIANT_CLASS: Record<SpinnerVariant, string> = {
  spin: "ap-spin",
  chase: "ap-chase",
  orbit: "ap-orbit",
};

export interface SpinnerProps {
  size?: keyof typeof sizes;
  /** Use the brand accent (navy) instead of ink. */
  accent?: boolean;
  /** "chase" runs a light around the still mark (default); "spin" rotates the
   *  mark; "orbit" is the chase over a slow turn. */
  variant?: SpinnerVariant;
  className?: string;
}

/** Inline loading indicator: the brand aperture mark in currentColor. */
export function Spinner({
  size = "md",
  accent,
  variant = "chase",
  className,
}: SpinnerProps) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className={cn(
        "inline-block shrink-0 align-[-0.15em]",
        SPINNER_VARIANT_CLASS[variant],
        sizes[size],
        accent ? "text-accent" : "text-ink",
        className,
      )}
    >
      <ApertureMark mono className="w-full h-full" />
    </span>
  );
}

export type PageLoaderVariant =
  | "cascade"
  | "iris"
  | "glow"
  | "tick"
  | "ripple"
  | "comet";

const LOADER_VARIANT_CLASS: Record<PageLoaderVariant, string> = {
  cascade: "",
  iris: "ap-iris",
  glow: "ap-glow",
  tick: "ap-tick",
  ripple: "ap-ripple",
  comet: "ap-comet",
};

export interface PageLoaderProps {
  /** Optional context line, e.g. "Loading runs…". Keep it short. */
  label?: string;
  /** Motion treatment; "glow" (the chase, a light running the still ring) is
   *  the picked default. Other variants remain for the /test lab. */
  variant?: PageLoaderVariant;
  className?: string;
}

/**
 * Brand page loader: the aperture mark in the pinned brand palette, animated
 * per `variant` (see the aperture motion system in globals.css). Only mounted
 * while a page is loading.
 */
export function PageLoader({ label, variant = "glow", className }: PageLoaderProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn("flex flex-col items-center justify-center gap-5 py-24", className)}
    >
      <span className={cn("ap-loader", LOADER_VARIANT_CLASS[variant])}>
        <ApertureMark className="w-16 h-16" />
      </span>
      {label && (
        <Text size="sm" color="faint">
          {label}
        </Text>
      )}
    </div>
  );
}

import { cva, type VariantProps } from "class-variance-authority";
import { forwardRef } from "react";
import { cn } from "@/lib/cn";

/**
 * Inline text styling. Pair with `as="h1"`–`as="h6"` for semantic headings;
 * for block layout wrap the Text in a raw `<div>` (or `<ListItem>` / `<Label>`
 * via the design-system list / label components). There is no `<p>` in this
 * codebase, paragraphs render as `<Text>` inside a `<div>`. The size scale
 * runs `xs / sm / md / base / h3 / h2 / h1 / display`; the top four are sized
 * for heading and display use. Per-size letter-spacing (tracking) is baked in
 * here so call sites never hand-tune it; use the `leading` prop for line-height
 * instead of a `leading-[…]` className.
 */
const textVariants = cva("font-sans", {
  variants: {
    size: {
      xs: "text-xs",
      sm: "text-sm",
      md: "text-base",
      base: "text-base",
      h3: "text-lg tracking-[-0.012em]",
      h2: "text-xl tracking-[-0.016em]",
      h1: "text-2xl tracking-[-0.02em]",
      display: "text-3xl tracking-[-0.02em]",
    },
    weight: {
      normal: "font-normal",
      medium: "font-medium",
      semibold: "font-semibold",
      bold: "font-bold",
    },
    color: {
      default: "text-ink",
      muted: "text-ink-2",
      faint: "text-ink-3",
      accent: "text-accent-ink",
      danger: "text-destructive-ink",
    },
    leading: {
      none: "leading-none",
      tight: "leading-tight",
      snug: "leading-snug",
      normal: "leading-normal",
      relaxed: "leading-relaxed",
    },
  },
  defaultVariants: {
    size: "base",
    weight: "normal",
    color: "default",
  },
});

type HeadingTag = "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
type TextElement = "span" | HeadingTag;

export type TextProps = Omit<React.HTMLAttributes<HTMLElement>, "color"> &
  VariantProps<typeof textVariants> & {
    as?: TextElement;
    /** Render in Geist Mono. Use sparingly: for UUIDs/IDs, code-like strings,
     *  and content where character-by-character comparison matters. Body text,
     *  labels, headings stay in the default Geist Sans. */
    mono?: boolean;
  };

export const Text = forwardRef<HTMLElement, TextProps>(function Text(
  { as = "span", size, weight, color, leading, mono, className, ...props },
  ref,
) {
  const Tag = as as React.ElementType;
  return (
    <Tag
      ref={ref}
      className={cn(
        textVariants({ size, weight, color, leading }),
        mono && "font-mono",
        className,
      )}
      {...props}
    />
  );
});
Text.displayName = "Text";

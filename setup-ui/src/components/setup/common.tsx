"use client";

import { useState } from "react";
import { Check, Copy, ExternalLink, Plus, X } from "lucide-react";

import { cn } from "@/lib/cn";
import { Badge } from "@/components/ui/badge";
import { Banner } from "@/components/ui/banner";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { List, ListItem } from "@/components/ui/list";
import { RadioGroup } from "@/components/ui/radio-group";
import { Text } from "@/components/ui/text";
import * as RadioGroupPrimitive from "@radix-ui/react-radio-group";

export function Problems({ problems }: { problems: string[] }) {
  if (!problems.length) return null;
  return (
    <div
      role="alert"
      className="relative overflow-hidden flex flex-col gap-1.5 pl-4 pr-3.5 py-3 rounded-md border border-rule bg-bg-raised shadow-xs"
    >
      <span aria-hidden className="absolute left-0 inset-y-0 w-[3px] bg-danger" />
      <Text size="sm" weight="medium">
        Not saved. Fix these first:
      </Text>
      <List gap="xs">
        {problems.map((p) => (
          <ListItem key={p} align="start" gap="md">
            <Text size="sm" color="danger">
              {p}
            </Text>
          </ListItem>
        ))}
      </List>
    </div>
  );
}

export function Section({
  title,
  description,
  children,
  className,
}: {
  title: string;
  description?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("flex flex-col gap-4 py-6 border-b border-rule-2 last:border-b-0", className)}>
      <div className="flex flex-col gap-1">
        <Text as="h3" size="base" weight="semibold">
          {title}
        </Text>
        {description && (
          <Text size="sm" color="faint" leading="relaxed">
            {description}
          </Text>
        )}
      </div>
      {children}
    </section>
  );
}

type FieldProps = {
  id: string;
  label: string;
  hint?: React.ReactNode;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  multiline?: boolean;
  mono?: boolean;
  type?: string;
  required?: boolean;
  disabled?: boolean;
  className?: string;
  /** A problem from the last save; shown under the field. */
  error?: string;
  onBlur?: () => void;
};

export function Field({
  id,
  label,
  hint,
  value,
  onChange,
  placeholder,
  multiline,
  mono,
  type,
  required,
  disabled,
  className,
  error,
  onBlur,
}: FieldProps) {
  return (
    <div className={cn("flex flex-col gap-1.5 min-w-0", className)}>
      <Label htmlFor={id} className="text-ink-2">
        {label}
        {required && <Text color="faint"> (required)</Text>}
      </Label>
      {multiline && (
        <Textarea
          id={id}
          rows={3}
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          error={Boolean(error)}
          aria-invalid={Boolean(error)}
          onBlur={onBlur}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {!multiline && (
        <Input
          id={id}
          type={type || "text"}
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          error={Boolean(error)}
          aria-invalid={Boolean(error)}
          onBlur={onBlur}
          className={cn(mono && "font-mono placeholder:font-sans")}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {error && (
        <Text size="xs" color="danger" leading="relaxed">
          {error}
        </Text>
      )}
      {hint && (
        <Text size="xs" color="faint" leading="relaxed">
          {hint}
        </Text>
      )}
    </div>
  );
}

/** A secret box: write-only. Shows whether the key is set, never its value. */
export function SecretField({
  name,
  label,
  isSet,
  value,
  onChange,
  hint,
  required,
  error,
  onBlur,
}: {
  name: string;
  label: string;
  isSet: boolean;
  value: string;
  onChange: (v: string) => void;
  hint?: React.ReactNode;
  required?: boolean;
  error?: string;
  /** Secrets save on blur (and on Next), never while typing. */
  onBlur?: () => void;
}) {
  const id = `secret-${name}`;
  return (
    <div className="flex flex-col gap-1.5 min-w-0">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <Label htmlFor={id} className="text-ink-2">
          {label}
          {required && <Text color="faint"> (required)</Text>}
          {!required && <Text color="faint"> (optional)</Text>}
        </Label>
        <SetBadge set={isSet} />
      </div>
      <Input
        id={id}
        type="password"
        autoComplete="off"
        spellCheck={false}
        value={value}
        placeholder={isSet ? "Set. Paste a new value to replace it" : "Paste the value"}
        className="font-mono placeholder:font-sans"
        error={Boolean(error)}
        aria-invalid={Boolean(error)}
        onBlur={onBlur}
        onChange={(e) => onChange(e.target.value)}
      />
      {error && (
        <Text size="xs" color="danger">
          {error}
        </Text>
      )}
      <Text size="xs" color="faint" leading="relaxed">
        <Text size="xs" mono color="faint">
          {name}
        </Text>
        {hint && <Text size="xs" color="faint"> · {hint}</Text>}
      </Text>
    </div>
  );
}

export function SetBadge({ set }: { set: boolean }) {
  if (set) {
    return (
      <Badge color="success" dot>
        Set
      </Badge>
    );
  }
  return (
    <Badge color="neutral" dot>
      Not set
    </Badge>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1400);
      })
      .catch(() => setCopied(false));
  }
  return (
    <Button variant="secondary" size="sm" onClick={copy} type="button">
      {copied && <Check className="w-3.5 h-3.5" />}
      {!copied && <Copy className="w-3.5 h-3.5" />}
      {copied && "Copied"}
      {!copied && label}
    </Button>
  );
}

/** A command or URL in a mono block with a copy button. */
export function CodeBlock({ text, label }: { text: string; label?: string }) {
  return (
    <div className="flex flex-col gap-1.5 min-w-0">
      {label && (
        <Text size="xs" color="faint">
          {label}
        </Text>
      )}
      <div className="flex items-start gap-2 rounded-md border border-rule bg-bg-sunk px-3 py-2.5 min-w-0">
        <pre className="flex-1 min-w-0 overflow-x-auto whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-xs leading-relaxed text-ink m-0">
          {text}
        </pre>
        <CopyButton text={text} />
      </div>
    </div>
  );
}

export function LinkButton({
  href,
  children,
  variant = "secondary",
  external = true,
}: {
  href: string;
  children: React.ReactNode;
  variant?: "primary" | "secondary" | "outline";
  external?: boolean;
}) {
  const styles = {
    primary: "bg-accent text-accent-solid-text border-accent hover:opacity-90",
    secondary: "bg-bg-raised text-ink border-rule shadow-xs hover:bg-bg-sunk hover:border-rule-strong",
    outline: "bg-transparent text-ink-2 border-rule hover:bg-bg-sunk hover:text-ink",
  };
  return (
    <a
      href={href}
      target={external ? "_blank" : undefined}
      rel={external ? "noreferrer noopener" : undefined}
      className={cn(
        "inline-flex items-center justify-center gap-2 h-9 px-3.5 rounded-sm border",
        "font-sans text-sm font-medium whitespace-nowrap no-underline",
        "transition-[background,color,border-color] duration-[120ms] ease-out",
        "focus-visible:outline-none focus-visible:ring-nua",
        styles[variant],
      )}
    >
      {children}
      {external && <ExternalLink className="w-3.5 h-3.5" />}
    </a>
  );
}

export function TextLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="text-accent-ink underline underline-offset-2 decoration-rule-strong hover:decoration-accent"
    >
      {children}
    </a>
  );
}

export type Choice = { value: string; title: string; description: string; disabled?: boolean };

/** Card-style radio choice for the few big decisions (cloud, recorder). */
export function ChoiceCards({
  value,
  onChange,
  choices,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  choices: Choice[];
  label: string;
}) {
  return (
    <RadioGroup
      value={value}
      onValueChange={onChange}
      aria-label={label}
      className="grid grid-cols-1 sm:grid-cols-3 gap-3"
    >
      {choices.map((c) => (
        <RadioGroupPrimitive.Item
          key={c.value}
          value={c.value}
          disabled={c.disabled}
          className={cn(
            "group text-left flex flex-col gap-1 rounded-md border border-rule bg-bg-raised px-4 py-3 shadow-xs cursor-pointer",
            "transition-[border-color,box-shadow,background] duration-[120ms] ease-out",
            "hover:border-rule-strong focus-visible:outline-none focus-visible:ring-nua",
            "data-[state=checked]:border-accent data-[state=checked]:bg-accent-soft/40",
            "disabled:opacity-50 disabled:pointer-events-none",
          )}
        >
          <div className="flex items-center gap-2">
            <span
              aria-hidden
              className="w-4 h-4 rounded-full border border-rule-strong bg-bg-raised flex-none group-data-[state=checked]:border-accent group-data-[state=checked]:border-[5px]"
            />
            <Text size="sm" weight="semibold">
              {c.title}
            </Text>
          </div>
          <Text size="xs" color="faint" leading="relaxed">
            {c.description}
          </Text>
        </RadioGroupPrimitive.Item>
      ))}
    </RadioGroup>
  );
}

/** Editable list of one-line strings. */
export function StringList({
  items,
  onChange,
  placeholder,
  addLabel,
  idPrefix,
}: {
  items: string[];
  onChange: (items: string[]) => void;
  placeholder?: string;
  addLabel: string;
  idPrefix: string;
}) {
  return (
    <div className="flex flex-col gap-2">
      {items.map((item, i) => (
        <div key={`${idPrefix}-${i}`} className="flex items-center gap-2">
          <Text size="xs" color="faint" mono className="w-6 text-right flex-none">
            {i + 1}
          </Text>
          <Input
            aria-label={`${idPrefix} ${i + 1}`}
            value={item}
            placeholder={placeholder}
            onChange={(e) => onChange(items.map((x, j) => (j === i ? e.target.value : x)))}
          />
          <Button
            variant="ghost-danger"
            size="md"
            iconOnly
            type="button"
            aria-label={`Remove ${item || "row"}`}
            onClick={() => onChange(items.filter((_, j) => j !== i))}
          >
            <X className="w-4 h-4" />
          </Button>
        </div>
      ))}
      <div>
        <Button variant="outline" size="sm" type="button" onClick={() => onChange([...items, ""])}>
          <Plus className="w-3.5 h-3.5" />
          {addLabel}
        </Button>
      </div>
    </div>
  );
}

export function Note({ title, description, variant = "default" }: {
  title: string;
  description?: string;
  variant?: "default" | "success" | "danger" | "warn";
}) {
  return <Banner title={title} description={description} variant={variant} orientation="vertical" />;
}

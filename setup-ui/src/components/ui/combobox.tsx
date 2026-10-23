"use client";

import { useState } from "react";
import * as PopoverPrimitive from "@radix-ui/react-popover";
import { Command } from "cmdk";
import { Search } from "lucide-react";

import { cn } from "@/lib/cn";
import { Text } from "@/components/ui/text";

export type ComboboxItem = {
  id: string;
  label: string;
  /** Quiet right-aligned annotation (a count, a status word). */
  hint?: string;
  /** Small leading adornment (icon, colored dot). */
  icon?: React.ReactNode;
  disabled?: boolean;
};

export type ComboboxProps = {
  /** The element that opens the popover (rendered asChild). */
  trigger: React.ReactNode;
  items: ComboboxItem[];
  onSelect: (id: string) => void;
  placeholder?: string;
  emptyText?: string;
  align?: "start" | "center" | "end";
  className?: string;
};

/**
 * A searchable select in a popover: trigger opens a cmdk list with a filter
 * input, arrow-key navigation, and Enter to pick. For overflow pickers where
 * a plain dropdown would be too long to scan (draft rails, long entity lists).
 */
export function Combobox({
  trigger,
  items,
  onSelect,
  placeholder = "Search…",
  emptyText = "No matches.",
  align = "start",
  className,
}: ComboboxProps) {
  const [open, setOpen] = useState(false);
  return (
    <PopoverPrimitive.Root open={open} onOpenChange={setOpen}>
      <PopoverPrimitive.Trigger asChild>{trigger}</PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align={align}
          sideOffset={6}
          collisionPadding={12}
          className={cn(
            "z-50 w-64 rounded-md border border-rule bg-bg-raised shadow-md",
            "data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95",
            "data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95",
            className,
          )}
        >
          <Command loop>
            <div className="flex items-center gap-2 px-2.5 border-b border-rule">
              <Search className="w-3.5 h-3.5 text-ink-4 shrink-0" aria-hidden />
              <Command.Input
                placeholder={placeholder}
                className="w-full bg-transparent py-2 text-sm text-ink placeholder:text-ink-4 outline-none"
              />
            </div>
            <Command.List className="max-h-64 overflow-y-auto p-1">
              <Command.Empty className="px-2.5 py-3">
                <Text size="xs" color="faint">
                  {emptyText}
                </Text>
              </Command.Empty>
              {items.map((item) => (
                <Command.Item
                  key={item.id}
                  value={`${item.label} ${item.id}`}
                  disabled={item.disabled}
                  onSelect={() => {
                    setOpen(false);
                    onSelect(item.id);
                  }}
                  className={cn(
                    "flex items-center gap-2 rounded-sm px-2.5 py-2 min-w-0",
                    "cursor-pointer select-none",
                    "data-[selected=true]:bg-bg-inset",
                    "data-[disabled=true]:opacity-50 data-[disabled=true]:pointer-events-none",
                  )}
                >
                  {item.icon && <span className="shrink-0">{item.icon}</span>}
                  <Text size="sm" className="truncate flex-1">
                    {item.label}
                  </Text>
                  {item.hint && (
                    <Text size="xs" color="faint" className="shrink-0">
                      {item.hint}
                    </Text>
                  )}
                </Command.Item>
              ))}
            </Command.List>
          </Command>
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}

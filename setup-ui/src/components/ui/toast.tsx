"use client";

import { Toaster, toast as sonner } from "sonner";

type ToastType = "success" | "danger" | "warn" | "info";

type ToastAction = {
  label: string;
  onClick: () => void;
};

type ToastOptions = {
  type: ToastType;
  title: string;
  description?: string;
  action?: ToastAction;
};

/* Map the app's toast vocabulary onto Sonner's. The call-site API predates the
   Sonner migration and is kept stable: toast({ type, title, description }). */
const FIRE: Record<ToastType, typeof sonner.success> = {
  success: sonner.success,
  danger: sonner.error,
  warn: sonner.warning,
  info: sonner.info,
};

function fire({ type, title, description, action }: ToastOptions) {
  FIRE[type](title, { description, action });
}

/**
 * Sonner toaster themed with the app's tokens (token classes flip with
 * `data-theme`, so light/dark follow the app for free). Mounted once at the
 * root; components fire toasts through `useToast()`.
 */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  return (
    <>
      {children}
      <Toaster
        position="bottom-right"
        gap={8}
        duration={5000}
        closeButton
        toastOptions={{
          classNames: {
            toast: "!bg-bg-raised !border !border-rule !rounded-md !shadow-md !gap-3",
            title: "!text-ink !text-sm !font-medium",
            description: "!text-ink-2 !text-sm",
            closeButton: "!bg-bg-raised !border-rule !text-ink-3 hover:!text-ink",
            actionButton: "!bg-ink !text-bg !text-xs !font-medium !rounded-sm",
            icon: "!text-ink-2",
          },
        }}
      />
    </>
  );
}

export function useToast() {
  return { toast: fire };
}

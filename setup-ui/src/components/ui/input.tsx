import { forwardRef } from "react";
import { cn } from "@/lib/cn";

export interface InputProps
  extends React.InputHTMLAttributes<HTMLInputElement> {
  error?: boolean;
  success?: boolean;
  /** Chromeless inline editor: no border/shadow/box until hover or focus , 
   *  for text that should read as content (titles) yet stay editable. */
  inline?: boolean;
}

const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, error, success, inline, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "h-9 w-full rounded-md font-sans text-sm text-ink",
        "placeholder:text-ink-4",
        "transition-[border-color,box-shadow,background] duration-[120ms] ease-out",
        !inline && "px-3 border border-rule bg-bg-raised shadow-xs",
        !inline && "hover:border-rule-strong",
        !inline && "focus:outline-none focus:border-accent focus:ring-nua",
        inline && "px-1 -mx-1 border border-transparent bg-transparent shadow-none",
        inline && "hover:bg-bg-sunk/60 focus:bg-bg-sunk/60 focus:outline-none",
        error && "!border-danger focus:ring-nua-danger",
        success && "!border-accent",
        "disabled:opacity-50 disabled:pointer-events-none",
        !inline && "disabled:bg-bg-sunk",
        className
      )}
      {...props}
    />
  )
);
Input.displayName = "Input";

export interface TextFieldProps extends Omit<InputProps, "prefix"> {
  label?: string;
  hint?: string;
  errorMessage?: string;
  successMessage?: string;
  prefix?: React.ReactNode;
  suffix?: React.ReactNode;
}

const TextField = forwardRef<HTMLInputElement, TextFieldProps>(
  ({ label, hint, errorMessage, successMessage, prefix, suffix, className, ...inputProps }, ref) => {
    const hasError = !!errorMessage;
    const hasSuccess = !!successMessage;

    return (
      <div className={cn("flex flex-col gap-1.5", className)}>
        {label && (
          <label className="text-sm text-ink-2 font-medium">{label}</label>
        )}
        {prefix || suffix ? (
          <div className="relative">
            {prefix && (
              <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-3 text-sm pointer-events-none">
                {prefix}
              </span>
            )}
            <Input
              ref={ref}
              error={hasError}
              success={hasSuccess}
              className={cn(prefix && "pl-16", suffix && "pr-16")}
              {...inputProps}
            />
            {suffix && (
              <span className="absolute right-2.5 top-1/2 -translate-y-1/2 text-ink-3 text-xs">
                {suffix}
              </span>
            )}
          </div>
        ) : (
          <Input ref={ref} error={hasError} success={hasSuccess} {...inputProps} />
        )}
        {(hint || errorMessage || successMessage) && (
          <div
            className={cn(
              "text-xs",
              hasError ? "text-danger-ink" : hasSuccess ? "text-accent-ink" : "text-ink-3"
            )}
          >
            {errorMessage ?? successMessage ?? hint}
          </div>
        )}
      </div>
    );
  }
);
TextField.displayName = "TextField";

export interface TextareaProps
  extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  error?: boolean;
  success?: boolean;
}

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, error, success, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        "w-full px-3 py-2 rounded-md",
        "border border-rule bg-bg-raised text-ink shadow-xs",
        "font-sans text-[13.5px] leading-[1.5]",
        "placeholder:text-ink-4",
        "resize-y",
        "transition-[border-color,box-shadow,background] duration-[120ms] ease-out",
        "hover:border-rule-strong",
        "focus:outline-none focus:border-accent focus:ring-nua",
        error && "!border-danger focus:ring-nua-danger",
        success && "!border-accent",
        "disabled:opacity-50 disabled:pointer-events-none disabled:bg-bg-sunk",
        className,
      )}
      {...props}
    />
  ),
);
Textarea.displayName = "Textarea";

export { Input, TextField, Textarea };

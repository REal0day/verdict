import { type InputHTMLAttributes, type TextareaHTMLAttributes, type SelectHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/cn";

const base =
  "w-full bg-surface text-fg border border-border rounded-md px-3 py-2 text-sm " +
  "placeholder:text-fgmuted/70 focus:outline-none focus:ring-2 focus:ring-primary/50 " +
  "focus:border-primary/60 disabled:opacity-50 disabled:cursor-not-allowed transition";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={cn(base, className)} {...rest} />;
  }
);

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...rest }, ref) {
    return <textarea ref={ref} className={cn(base, "font-mono", className)} {...rest} />;
  }
);

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, ...rest }, ref) {
    return <select ref={ref} className={cn(base, "pr-8", className)} {...rest} />;
  }
);

export function Label({ children, className, ...rest }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label className={cn("block text-xs font-medium text-fgmuted mb-1", className)} {...rest}>
      {children}
    </label>
  );
}

import { type ButtonHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const variants: Record<Variant, string> = {
  primary:
    "bg-primary text-primaryfg hover:bg-primary/90 active:bg-primary/80 disabled:opacity-50",
  secondary:
    "bg-surface text-fg border border-border hover:bg-muted active:bg-muted/70 disabled:opacity-50",
  ghost:
    "bg-transparent text-fgmuted hover:text-fg hover:bg-muted disabled:opacity-50",
  danger:
    "bg-danger/10 text-danger border border-danger/30 hover:bg-danger/15 disabled:opacity-50",
};

const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-xs gap-1.5",
  md: "h-9 px-4 text-sm gap-2",
};

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", className, ...rest },
  ref
) {
  return (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center rounded-md font-medium transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60",
        "disabled:cursor-not-allowed whitespace-nowrap",
        variants[variant],
        sizes[size],
        className
      )}
      {...rest}
    />
  );
});

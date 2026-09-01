import { type HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

type Tone = "default" | "primary" | "success" | "warning" | "danger" | "muted";

const tones: Record<Tone, string> = {
  default: "bg-muted text-fg border border-border",
  primary: "bg-primary/15 text-primary border border-primary/30",
  success: "bg-success/15 text-success border border-success/30",
  warning: "bg-warning/15 text-warning border border-warning/30",
  danger:  "bg-danger/15 text-danger border border-danger/30",
  muted:   "bg-transparent text-fgmuted border border-border",
};

export function Badge({
  tone = "default", className, ...rest
}: HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium leading-4",
        tones[tone],
        className
      )}
      {...rest}
    />
  );
}

const sevToClass: Record<string, string> = {
  critical: "sev-critical",
  high:     "sev-high",
  medium:   "sev-medium",
  low:      "sev-low",
  info:     "sev-info",
  unknown:  "sev-unknown",
};

export function SeverityChip({ value }: { value: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-2 py-0.5 text-[11px] font-semibold leading-4 uppercase tracking-wide",
        sevToClass[value] || sevToClass.unknown
      )}
    >
      {value}
    </span>
  );
}

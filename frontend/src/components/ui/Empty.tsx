import { type ReactNode } from "react";

export function Empty({ icon, title, hint }: { icon?: ReactNode; title: string; hint?: ReactNode }) {
  return (
    <div className="border border-dashed border-border rounded-lg p-10 text-center text-fgmuted bg-surface/40">
      {icon ? <div className="flex justify-center mb-3 opacity-60">{icon}</div> : null}
      <div className="text-sm font-medium text-fg">{title}</div>
      {hint ? <div className="text-xs mt-1">{hint}</div> : null}
    </div>
  );
}

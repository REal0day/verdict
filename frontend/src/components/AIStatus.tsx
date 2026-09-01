/**
 * Shared "is the AI actually usable?" surface.
 *
 * Two failure modes used to look identical to a user: no key configured, and
 * a key the provider rejects. Both showed up as a generic "request failed"
 * after a long wait. `useAIStatus` lets a page warn up front; `aiErrorText`
 * turns a failed mutation into the server's actionable message.
 */
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { api, ApiError } from "@/lib/api";

export type AIStatus = {
  configured: boolean;
  provider: string;
  display_name: string;
  model: string;
  /** Is *any* provider usable? Distinguishes "nothing set up" from
   *  "the active one isn't set up, but another is". */
  any_configured: boolean;
};

export function useAIStatus() {
  return useQuery({
    queryKey: ["ai-status"],
    queryFn: () => api<AIStatus>("/settings/ai/status"),
    staleTime: 60_000,
    retry: false,
  });
}

/** Pull the server's `detail` out of an ApiError, with a usable fallback. */
export function aiErrorText(err: unknown, fallback = "Request failed."): string {
  if (err instanceof ApiError) {
    const d = err.detail;
    if (typeof d === "string" && d.trim()) return d;
    if (d && typeof d === "object" && typeof (d as any).detail === "string") {
      return (d as any).detail;
    }
    if (err.status === 0) return "Network error — the server is unreachable.";
  }
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

/** Inline warning shown before the user bothers composing a prompt. */
export function AIUnavailableNotice({ className = "" }: { className?: string }) {
  const { data } = useAIStatus();
  if (!data || data.configured) return null;
  return (
    <div
      role="status"
      className={`flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs ${className}`}
    >
      <AlertTriangle size={14} className="mt-0.5 shrink-0 text-warning" />
      <span>
        {data.any_configured ? (
          <>
            The active AI provider ({data.display_name}) isn't configured, so
            requests will fail. An administrator can switch to a configured
            provider under <strong>Settings → AI</strong>.
          </>
        ) : (
          <>
            No AI provider is configured yet, so the assistant can't answer. An
            administrator can add a hosted provider's key — or point the server
            at a local model — under <strong>Settings → AI</strong>.
          </>
        )}
      </span>
    </div>
  );
}

/** Error line for a failed AI call. Renders nothing when there's no error. */
export function AIErrorNotice({ error, className = "" }: { error: unknown; className?: string }) {
  if (!error) return null;
  return (
    <div
      role="alert"
      className={`flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger ${className}`}
    >
      <AlertTriangle size={14} className="mt-0.5 shrink-0" />
      <span>{aiErrorText(error, "Chat request failed.")}</span>
    </div>
  );
}

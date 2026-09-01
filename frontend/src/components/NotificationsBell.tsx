import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { Bell, Check } from "lucide-react";
import { cn } from "@/lib/cn";

type Notif = {
  id: string;
  kind: string;
  title: string;
  body: string;
  link: string;
  read_at: string | null;
  created_at: string;
};

type Count = { unread: number };

export function NotificationsBell() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const count = useQuery({
    queryKey: ["notif-count"],
    queryFn: () => api<Count>("/notifications/unread_count"),
    // Periodically poll so a freshly-arrived approval shows up without a reload.
    refetchInterval: 30_000,
  });

  const list = useQuery({
    queryKey: ["notif-list"],
    queryFn: () => api<Notif[]>("/notifications?limit=20"),
    enabled: open,
  });

  const markRead = useMutation({
    mutationFn: (id: string) => api(`/notifications/${id}/read`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notif-count"] });
      qc.invalidateQueries({ queryKey: ["notif-list"] });
    },
  });
  const markAll = useMutation({
    mutationFn: () => api(`/notifications/read_all`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notif-count"] });
      qc.invalidateQueries({ queryKey: ["notif-list"] });
    },
  });

  // Click-outside to close.
  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const unread = count.data?.unread ?? 0;

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "relative inline-flex items-center justify-center w-9 h-9 rounded-md",
          "text-fgmuted hover:bg-muted hover:text-fg transition-colors",
          open ? "bg-muted text-fg" : ""
        )}
        title="Notifications"
        aria-label="Notifications"
      >
        <Bell size={16} />
        {unread > 0 ? (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1
                          rounded-full bg-primary text-white text-[10px]
                          flex items-center justify-center font-medium">
            {unread > 99 ? "99+" : unread}
          </span>
        ) : null}
      </button>

      {open ? (
        <div className="absolute right-0 mt-2 w-80 bg-surface border border-border
                        rounded-lg shadow-lg overflow-hidden z-50">
          <div className="flex items-center justify-between px-3 py-2 border-b border-border">
            <span className="text-xs uppercase tracking-wider text-fgmuted">
              Notifications {unread > 0 ? `· ${unread} unread` : ""}
            </span>
            {unread > 0 ? (
              <button
                type="button"
                className="text-[11px] text-fgmuted hover:text-fg"
                onClick={() => markAll.mutate()}
              >
                Mark all read
              </button>
            ) : null}
          </div>
          <div className="max-h-96 overflow-y-auto">
            {list.isLoading ? (
              <div className="p-4 text-xs text-fgmuted">Loading…</div>
            ) : !list.data || list.data.length === 0 ? (
              <div className="p-6 text-center text-xs text-fgmuted">Nothing here yet.</div>
            ) : (
              list.data.map((n) => (
                <NotifRow
                  key={n.id}
                  n={n}
                  onClick={() => { if (!n.read_at) markRead.mutate(n.id); setOpen(false); }}
                />
              ))
            )}
          </div>
          <Link
            to="/notifications"
            onClick={() => setOpen(false)}
            className="block text-center py-2 text-xs text-primary hover:bg-muted/30 border-t border-border"
          >
            See all
          </Link>
        </div>
      ) : null}
    </div>
  );
}

function NotifRow({ n, onClick }: { n: Notif; onClick: () => void }) {
  const inner = (
    <div
      onClick={onClick}
      className={cn(
        "px-3 py-2 border-b border-border last:border-b-0 cursor-pointer hover:bg-muted/30",
        n.read_at ? "" : "bg-primary/5"
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span className={cn("text-sm leading-tight", n.read_at ? "text-fgmuted" : "text-fg font-medium")}>
          {n.title}
        </span>
        {n.read_at ? null : <span className="w-1.5 h-1.5 rounded-full bg-primary mt-1 shrink-0" />}
      </div>
      {n.body ? (
        <div className="text-xs text-fgmuted mt-0.5 line-clamp-2">{n.body}</div>
      ) : null}
      <div className="text-[10px] text-fgmuted/70 mt-1">
        {new Date(n.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
      </div>
    </div>
  );
  return n.link ? <Link to={n.link} className="block">{inner}</Link> : inner;
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Empty } from "@/components/ui/Empty";
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

export function Notifications() {
  const qc = useQueryClient();

  const q = useQuery({
    queryKey: ["notif-list-full"],
    queryFn: () => api<Notif[]>("/notifications?limit=200"),
  });

  const markRead = useMutation({
    mutationFn: (id: string) => api(`/notifications/${id}/read`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notif-list-full"] });
      qc.invalidateQueries({ queryKey: ["notif-count"] });
      qc.invalidateQueries({ queryKey: ["notif-list"] });
    },
  });
  const markAll = useMutation({
    mutationFn: () => api(`/notifications/read_all`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notif-list-full"] });
      qc.invalidateQueries({ queryKey: ["notif-count"] });
      qc.invalidateQueries({ queryKey: ["notif-list"] });
    },
  });

  const rows = q.data || [];
  const unread = rows.filter((n) => !n.read_at).length;

  return (
    <>
      <PageHeader
        title="Notifications"
        subtitle={unread ? `${unread} unread` : "All caught up."}
        action={
          unread ? (
            <Button variant="secondary" onClick={() => markAll.mutate()}>
              <Check size={14} /> Mark all read
            </Button>
          ) : null
        }
      />

      {q.isLoading ? (
        <div className="text-sm text-fgmuted">Loading…</div>
      ) : rows.length === 0 ? (
        <Empty
          icon={<Bell size={28} />}
          title="No notifications yet"
          hint="Project access requests, approvals, and updates will show up here."
        />
      ) : (
        <Card>
          {rows.map((n) => {
            const inner = (
              <div
                className={cn(
                  "px-4 py-3 border-b border-border last:border-b-0 hover:bg-muted/20",
                  n.read_at ? "" : "bg-primary/5"
                )}
                onClick={() => { if (!n.read_at) markRead.mutate(n.id); }}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className={cn(
                      "text-sm leading-tight",
                      n.read_at ? "text-fgmuted" : "font-medium"
                    )}>
                      {n.title}
                    </div>
                    {n.body ? (
                      <div className="text-xs text-fgmuted mt-1 whitespace-pre-wrap">{n.body}</div>
                    ) : null}
                    <div className="text-[11px] text-fgmuted/70 mt-1">
                      {new Date(n.created_at).toLocaleString()}
                    </div>
                  </div>
                  {n.read_at ? null : (
                    <span className="w-2 h-2 rounded-full bg-primary mt-1.5 shrink-0" />
                  )}
                </div>
              </div>
            );
            return n.link ? (
              <Link key={n.id} to={n.link} className="block">{inner}</Link>
            ) : (
              <div key={n.id}>{inner}</div>
            );
          })}
        </Card>
      )}
    </>
  );
}

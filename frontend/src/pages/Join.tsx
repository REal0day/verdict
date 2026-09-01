import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useState } from "react";
import { api, getToken } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { FolderGit2, ArrowRight, AlertTriangle, Check } from "lucide-react";

type InvitePreview = {
  project_id: string;
  project_name: string;
  project_description: string;
  inviter_email: string;
  expires_at: string | null;
  status: "active" | "expired" | "used_up" | "revoked" | "unknown";
};

/**
 * /app/join/:token
 *
 * Unauth-friendly landing page. We fetch the invite preview directly
 * (the endpoint is public), then branch on auth:
 *   - logged-in:  show "Join 'X'?" with a Redeem button
 *   - logged-out: show "Sign up to join 'X'" linking to /ui/register?invite=<token>
 */
export function Join() {
  const { token = "" } = useParams();
  const { me, ready, refresh } = useAuth();
  const nav = useNavigate();
  const [error, setError] = useState<string | null>(null);

  // The preview endpoint accepts no auth — but our `api()` helper will
  // attach a Bearer if one's around. That's fine; the server ignores it.
  const preview = useQuery({
    queryKey: ["invite-preview", token],
    queryFn: () => api<InvitePreview>(`/invites/${encodeURIComponent(token)}`),
    enabled: !!token,
  });

  const redeem = useMutation({
    mutationFn: () =>
      api<{ project_id: string; project_name: string; already_member: boolean }>(
        `/invites/${encodeURIComponent(token)}/redeem`,
        { method: "POST" }
      ),
    onSuccess: async (r) => {
      await refresh();
      nav(`/products/${r.project_id}`);
    },
    onError: (e: any) => setError(e?.detail || e?.message || "Couldn't redeem this invite."),
  });

  if (!ready) {
    return <CenteredCard><p className="text-sm text-fgmuted">Loading…</p></CenteredCard>;
  }

  if (preview.isLoading) {
    return <CenteredCard><p className="text-sm text-fgmuted">Checking invite…</p></CenteredCard>;
  }
  const p = preview.data;

  if (!p || p.status === "unknown" || !p.project_id) {
    return (
      <CenteredCard>
        <p className="text-sm text-danger flex items-center gap-2">
          <AlertTriangle size={14} /> This invite link isn't valid.
        </p>
        <p className="text-xs text-fgmuted mt-2">
          Ask whoever shared it for a fresh link, or <Link to="/" className="text-primary hover:underline">go to your dashboard</Link>.
        </p>
      </CenteredCard>
    );
  }

  const inactive = p.status !== "active";

  return (
    <CenteredCard>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderGit2 size={16} /> You've been invited
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-4">
        <div>
          <div className="text-xs text-fgmuted uppercase tracking-wider">Product</div>
          <div className="text-xl font-medium">{p.project_name}</div>
          {p.project_description ? (
            <p className="text-sm text-fgmuted mt-1">{p.project_description}</p>
          ) : null}
        </div>

        {p.inviter_email ? (
          <p className="text-xs text-fgmuted">
            Invited by <code className="text-fg">{p.inviter_email}</code>
          </p>
        ) : null}

        {inactive ? (
          <div className="text-sm text-danger flex items-center gap-2">
            <AlertTriangle size={14} /> This invite is <Badge tone="danger">{p.status}</Badge>.
            {" "}Ask for a fresh link.
          </div>
        ) : me ? (
          <>
            <p className="text-sm">
              You're signed in as <code className="text-fg">{me.email}</code>.
              Click below to join.
            </p>
            <Button onClick={() => redeem.mutate()} disabled={redeem.isPending}>
              {redeem.isPending ? "Joining…" : <>Join {p.project_name} <ArrowRight size={14} /></>}
            </Button>
            {error ? <p className="text-xs text-danger mt-2">{error}</p> : null}
          </>
        ) : (
          <>
            <p className="text-sm">
              Create an account to join this product — we'll add you automatically once you sign up.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <a href={`/ui/register?invite=${encodeURIComponent(token)}`}>
                <Button>Sign up &amp; join <ArrowRight size={14} /></Button>
              </a>
              <a href="/" className="text-xs text-fgmuted hover:text-fg">
                Already have an account? Sign in first.
              </a>
            </div>
          </>
        )}
      </CardBody>
    </CenteredCard>
  );
}

function CenteredCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-lg">{children}</Card>
    </div>
  );
}

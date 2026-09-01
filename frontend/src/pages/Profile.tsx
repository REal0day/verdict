import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Input, Label } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { AlertCircle, Check, FolderGit2, Sparkles } from "lucide-react";

type Team = { id: string; name: string };
type ProjectRow = {
  id: string; name: string; description: string; created_at: string;
  i_am_owner: boolean; i_am_member: boolean;
};

export function Profile() {
  const { me } = useAuth();
  const teams = useQuery({
    queryKey: ["teams"],
    queryFn: () => api<Team[]>("/teams"),
    enabled: !!me && me.role !== "user",   // only admin/manager can list teams
  });

  const teamName =
    me?.team_id ? teams.data?.find((t) => t.id === me.team_id)?.name : null;

  return (
    <>
      <PageHeader title="Profile" subtitle="Your account details and password." />

      <Card>
        <CardHeader><CardTitle>Account</CardTitle></CardHeader>
        <CardBody className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
          <div>
            <div className="text-xs text-fgmuted mb-1">Email</div>
            <div>{me?.email}</div>
          </div>
          <div>
            <div className="text-xs text-fgmuted mb-1">Role</div>
            <Badge tone={me?.role === "admin" ? "primary" : "muted"}>
              {me?.role}
            </Badge>
          </div>
          <div>
            <div className="text-xs text-fgmuted mb-1">Team</div>
            <div>{teamName || me?.team_id || <span className="text-fgmuted italic">(none)</span>}</div>
          </div>
        </CardBody>
      </Card>

      <ProjectsCard />
      <ChangePasswordCard />
      <OnboardingCard />
    </>
  );
}

function OnboardingCard() {
  const { refresh } = useAuth();
  const nav = useNavigate();
  const restart = useMutation({
    mutationFn: () => api("/auth/restart_onboarding", { method: "POST" }),
    onSuccess: async () => { await refresh(); nav("/welcome"); },
  });
  return (
    <Card className="mt-5">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles size={14} className="text-primary" /> Onboarding
        </CardTitle>
      </CardHeader>
      <CardBody className="flex items-center justify-between gap-3">
        <p className="text-sm text-fgmuted">
          Re-run the welcome wizard to request access to more products,
          generate another agent installer, or upload more files.
        </p>
        <Button variant="secondary" onClick={() => restart.mutate()} disabled={restart.isPending}>
          {restart.isPending ? "Starting…" : "Start onboarding"}
        </Button>
      </CardBody>
    </Card>
  );
}

function ProjectsCard() {
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<ProjectRow[]>("/projects"),
  });

  const owned = (projects.data || []).filter((p) => p.i_am_owner);
  const memberOf = (projects.data || [])
    .filter((p) => p.i_am_member && !p.i_am_owner);

  return (
    <Card className="mt-5">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderGit2 size={14} /> Products
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-4">
        <ProjectGroup
          title="You own"
          empty="You don't own any products yet."
          rows={owned}
          showOwner
        />
        <ProjectGroup
          title="You're a member of"
          empty="You're not a member of any products (other than ones you own)."
          rows={memberOf}
        />
      </CardBody>
    </Card>
  );
}

function ProjectGroup({ title, empty, rows, showOwner }:
  { title: string; empty: string; rows: ProjectRow[]; showOwner?: boolean }) {
  return (
    <div>
      <div className="text-xs text-fgmuted uppercase tracking-wider mb-2">{title}</div>
      {rows.length === 0 ? (
        <p className="text-sm text-fgmuted italic">{empty}</p>
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>Product</TH>
              <TH className="w-32">Created</TH>
              {showOwner ? <TH className="w-24"></TH> : null}
            </TR>
          </THead>
          <tbody>
            {rows.map((p) => (
              <TR key={p.id} className="hover:bg-muted/40">
                <TD>
                  <Link to={`/products/${p.id}`} className="text-primary hover:underline font-medium">
                    {p.name}
                  </Link>
                  {p.description ? (
                    <div className="text-xs text-fgmuted mt-0.5">{p.description}</div>
                  ) : null}
                </TD>
                <TD className="text-xs text-fgmuted whitespace-nowrap">
                  {new Date(p.created_at).toLocaleDateString()}
                </TD>
                {showOwner ? (
                  <TD><Badge tone="primary">owner</Badge></TD>
                ) : null}
              </TR>
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}

function ChangePasswordCard() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);

  const m = useMutation({
    mutationFn: () =>
      api("/auth/change-password", {
        method: "POST",
        body: { current_password: current, new_password: next },
      }),
    onSuccess: () => {
      setOk(true); setErr(null);
      setCurrent(""); setNext(""); setConfirm("");
      setTimeout(() => setOk(false), 3000);
    },
    onError: (e: any) => setErr(e.detail || "Failed to change password."),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null); setOk(false);
    if (next.length < 8) return setErr("New password must be at least 8 characters.");
    if (next !== confirm) return setErr("New passwords don't match.");
    m.mutate();
  }

  return (
    <Card className="mt-5">
      <CardHeader className="flex items-center justify-between">
        <CardTitle>Change password</CardTitle>
        {ok ? (
          <span className="text-xs text-success inline-flex items-center gap-1">
            <Check size={12} /> updated
          </span>
        ) : null}
      </CardHeader>
      <CardBody>
        <form onSubmit={submit} className="grid grid-cols-1 md:grid-cols-3 gap-3 items-end max-w-3xl">
          <div>
            <Label htmlFor="cur">Current password</Label>
            <Input id="cur" type="password" autoComplete="current-password"
                   value={current} onChange={(e) => setCurrent(e.target.value)} required />
          </div>
          <div>
            <Label htmlFor="new">New password</Label>
            <Input id="new" type="password" autoComplete="new-password"
                   minLength={8}
                   value={next} onChange={(e) => setNext(e.target.value)} required />
          </div>
          <div>
            <Label htmlFor="con">Confirm</Label>
            <Input id="con" type="password" autoComplete="new-password"
                   minLength={8}
                   value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
          </div>
          <div className="md:col-span-3 flex items-center gap-2">
            <Button type="submit" disabled={m.isPending}>
              {m.isPending ? "Updating…" : "Change password"}
            </Button>
            {err ? (
              <span className="text-xs text-danger inline-flex items-center gap-1">
                <AlertCircle size={12} /> {err}
              </span>
            ) : null}
          </div>
        </form>
      </CardBody>
    </Card>
  );
}

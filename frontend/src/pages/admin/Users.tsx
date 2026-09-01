import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Input, Label, Select } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Empty } from "@/components/ui/Empty";
import { Users as UsersIcon, Plus, Key, Check, AlertCircle, FolderGit2 } from "lucide-react";

type Role = "user" | "manager" | "admin";
type User = {
  id: string; email: string; role: Role; team_id: string | null;
  created_at: string;
};
type Team = { id: string; name: string };

export function UsersAdmin() {
  const { me } = useAuth();
  const qc = useQueryClient();

  const users = useQuery({
    queryKey: ["admin-users"],
    queryFn: () => api<User[]>("/users"),
  });
  const teams = useQuery({
    queryKey: ["teams"],
    queryFn: () => api<Team[]>("/teams"),
  });

  const [form, setForm] = useState<{ email: string; password: string; role: Role; team_id: string }>({
    email: "", password: "", role: "user", team_id: "",
  });
  const [createErr, setCreateErr] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      api<User>("/users", {
        method: "POST",
        body: {
          email: form.email, password: form.password,
          role: form.role,
          team_id: form.team_id || null,
        },
      }),
    onSuccess: () => {
      setForm({ email: "", password: "", role: "user", team_id: "" });
      setCreateErr(null);
      qc.invalidateQueries({ queryKey: ["admin-users"] });
    },
    onError: (e: any) => setCreateErr(e.detail || "Create failed"),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (form.email.trim() && form.password.length >= 8) create.mutate();
    else setCreateErr("Email + ≥8-char password required.");
  }

  const teamName = (id: string | null) =>
    id ? (teams.data?.find((t) => t.id === id)?.name || id.slice(0, 8) + "…") : "—";

  return (
    <>
      <PageHeader
        title="Users"
        subtitle="Create accounts, assign roles + teams, reset passwords."
      />

      <Card className="mb-5">
        <CardHeader><CardTitle>Create user</CardTitle></CardHeader>
        <CardBody>
          <form onSubmit={submit} className="grid grid-cols-1 md:grid-cols-5 gap-3 items-end">
            <div className="md:col-span-2">
              <Label htmlFor="email">Email</Label>
              <Input id="email" type="email" value={form.email}
                     onChange={(e) => setForm({...form, email: e.target.value})} required />
            </div>
            <div>
              <Label htmlFor="pw">Password</Label>
              <Input id="pw" type="password" minLength={8}
                     value={form.password}
                     onChange={(e) => setForm({...form, password: e.target.value})} required />
            </div>
            <div>
              <Label htmlFor="role">Role</Label>
              <Select id="role" value={form.role}
                      onChange={(e) => setForm({...form, role: e.target.value as Role})}>
                <option value="user">user</option>
                <option value="manager">manager</option>
                <option value="admin">admin</option>
              </Select>
            </div>
            <div>
              <Label htmlFor="team">Team</Label>
              <Select id="team" value={form.team_id}
                      onChange={(e) => setForm({...form, team_id: e.target.value})}>
                <option value="">— none —</option>
                {teams.data?.map((t) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </Select>
            </div>
            <div className="md:col-span-5 flex items-center gap-3">
              <Button type="submit" disabled={create.isPending}>
                <Plus size={14} /> {create.isPending ? "Creating…" : "Create user"}
              </Button>
              {createErr ? (
                <span className="text-xs text-danger inline-flex items-center gap-1">
                  <AlertCircle size={12} /> {createErr}
                </span>
              ) : null}
            </div>
          </form>
        </CardBody>
      </Card>

      <Card>
        <CardHeader><CardTitle>All users ({users.data?.length ?? 0})</CardTitle></CardHeader>
        {users.isLoading ? (
          <CardBody><p className="text-sm text-fgmuted">Loading…</p></CardBody>
        ) : !users.data || users.data.length === 0 ? (
          <CardBody><Empty icon={<UsersIcon size={28} />} title="No users yet" /></CardBody>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Email</TH>
                <TH className="w-36">Role</TH>
                <TH className="w-48">Team</TH>
                <TH className="w-32">Created</TH>
                <TH className="w-44"></TH>
              </TR>
            </THead>
            <tbody>
              {users.data.map((u) => (
                <UserRow key={u.id} u={u} teams={teams.data || []} me_id={me?.id || ""} teamName={teamName} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}

type ProjectLite = { id: string; name: string };

function ProductsPopover({ userId }: { userId: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  // Only fetch when opened so the page doesn't spam /users/{id}/projects for
  // every row on mount.
  const projects = useQuery({
    queryKey: ["all-projects"],
    queryFn: () => api<ProjectLite[]>("/projects"),
    enabled: open,
  });
  const current = useQuery({
    queryKey: ["user-projects", userId],
    queryFn: () => api<string[]>(`/users/${userId}/projects`),
    enabled: open,
  });

  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [savedAt, setSavedAt] = useState(0);
  // Sync local state from server data when the popover opens or the
  // server's current set changes (effects must not be conditional, so
  // we depend on the data and only act once it's available).
  useEffect(() => {
    if (current.data) setPicked(new Set(current.data));
  }, [current.data]);

  const save = useMutation({
    mutationFn: () =>
      api<string[]>(`/users/${userId}/projects`, {
        method: "PUT",
        body: { project_ids: Array.from(picked) },
      }),
    onSuccess: (ids) => {
      setSavedAt(Date.now());
      setTimeout(() => setSavedAt(0), 2000);
      qc.invalidateQueries({ queryKey: ["user-projects", userId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      setPicked(new Set(ids));
    },
  });

  function toggle(id: string) {
    const n = new Set(picked);
    n.has(id) ? n.delete(id) : n.add(id);
    setPicked(n);
  }

  const dirty = idsDiffer(picked, current.data || []);
  const justSaved = Date.now() - savedAt < 2000;

  return (
    <details
      className="relative"
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="text-xs text-fgmuted cursor-pointer hover:text-fg inline-flex items-center gap-1">
        <FolderGit2 size={12} /> products
      </summary>
      <div className="absolute right-0 mt-1 bg-surface border border-border rounded-md p-2 shadow-card z-10 w-72">
        <div className="text-[11px] uppercase tracking-wider text-fgmuted mb-1">
          Member of
        </div>
        <div className="max-h-48 overflow-y-auto pr-1 space-y-0.5">
          {projects.isLoading || current.isLoading ? (
            <p className="text-xs text-fgmuted">Loading…</p>
          ) : (projects.data || []).length === 0 ? (
            <p className="text-xs text-fgmuted italic">No products exist yet.</p>
          ) : (
            (projects.data || []).map((p) => (
              <label key={p.id} className="flex items-center gap-2 text-xs cursor-pointer py-0.5">
                <input
                  type="checkbox"
                  checked={picked.has(p.id)}
                  onChange={() => toggle(p.id)}
                />
                <span className="truncate" title={p.name}>{p.name}</span>
              </label>
            ))
          )}
        </div>
        <div className="flex items-center gap-2 mt-2 pt-2 border-t border-border">
          <Button size="sm" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "…" : justSaved ? <><Check size={12}/>saved</> : "Save"}
          </Button>
          <span className="text-[10px] text-fgmuted">
            {picked.size} selected
          </span>
        </div>
      </div>
    </details>
  );
}

function idsDiffer(a: Set<string>, b: readonly string[]) {
  const bs = new Set(b);
  if (a.size !== bs.size) return true;
  for (const id of a) if (!bs.has(id)) return true;
  return false;
}

function UserRow({ u, teams, me_id, teamName }:
  { u: User; teams: Team[]; me_id: string; teamName: (id: string | null) => string }) {
  const qc = useQueryClient();
  const [role, setRole] = useState<Role>(u.role);
  const [teamId, setTeamId] = useState<string>(u.team_id || "");
  const dirty = role !== u.role || teamId !== (u.team_id || "");
  const [saveOk, setSaveOk] = useState(false);

  const save = useMutation({
    mutationFn: () =>
      api(`/users/${u.id}`, {
        method: "PATCH",
        body: { role, team_id: teamId || null },
      }),
    onSuccess: () => {
      setSaveOk(true);
      setTimeout(() => setSaveOk(false), 2000);
      qc.invalidateQueries({ queryKey: ["admin-users"] });
    },
  });

  const [resetPw, setResetPw] = useState("");
  const [resetOk, setResetOk] = useState(false);
  const reset = useMutation({
    mutationFn: () =>
      api(`/users/${u.id}/reset-password`, {
        method: "POST",
        body: { new_password: resetPw },
      }),
    onSuccess: () => {
      setResetOk(true); setResetPw("");
      setTimeout(() => setResetOk(false), 2000);
    },
  });

  const isMe = u.id === me_id;

  return (
    <TR>
      <TD className="font-medium">
        {u.email}
        {isMe ? <Badge tone="muted" className="ml-2">you</Badge> : null}
      </TD>
      <TD>
        <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="user">user</option>
          <option value="manager">manager</option>
          <option value="admin">admin</option>
        </Select>
      </TD>
      <TD>
        <Select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
          <option value="">— none —</option>
          {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </Select>
        <div className="text-[11px] text-fgmuted mt-0.5 truncate" title={u.team_id || ""}>
          {teamName(u.team_id)}
        </div>
      </TD>
      <TD className="text-xs text-fgmuted">{new Date(u.created_at).toLocaleDateString()}</TD>
      <TD>
        <div className="flex items-center gap-2 flex-wrap">
          <Button size="sm" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "…" : saveOk ? <><Check size={12}/>saved</> : "Save"}
          </Button>
          <ProductsPopover userId={u.id} />
          <details className="relative">
            <summary className="text-xs text-fgmuted cursor-pointer hover:text-fg inline-flex items-center gap-1">
              <Key size={12} /> reset pw
            </summary>
            <div className="absolute right-0 mt-1 bg-surface border border-border rounded-md p-2 shadow-card z-10 w-64">
              <Label className="text-[11px]">New password</Label>
              <Input type="password" minLength={8} value={resetPw}
                     onChange={(e) => setResetPw(e.target.value)} />
              <div className="flex items-center gap-2 mt-2">
                <Button size="sm" disabled={resetPw.length < 8 || reset.isPending}
                        onClick={() => reset.mutate()}>
                  {reset.isPending ? "…" : resetOk ? <><Check size={12}/>set</> : "Set"}
                </Button>
                <span className="text-[10px] text-fgmuted">≥ 8 chars</span>
              </div>
            </div>
          </details>
        </div>
      </TD>
    </TR>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Input, Label } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Empty } from "@/components/ui/Empty";
import { Users2, Plus, Trash2, Check } from "lucide-react";

type Team = { id: string; name: string; member_count: number };

export function TeamsAdmin() {
  const qc = useQueryClient();

  const teams = useQuery({
    queryKey: ["teams"],
    queryFn: () => api<Team[]>("/teams"),
  });

  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => api<Team>("/teams", { method: "POST", body: { name } }),
    onSuccess: () => {
      setName("");
      qc.invalidateQueries({ queryKey: ["teams"] });
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    if (name.trim()) create.mutate();
  }

  return (
    <>
      <PageHeader
        title="Teams"
        subtitle="Create teams and assign them to users. Managers can see all reports from their team."
      />

      <Card className="mb-5">
        <CardHeader><CardTitle>Create team</CardTitle></CardHeader>
        <CardBody>
          <form onSubmit={submit} className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3 items-end">
            <div>
              <Label htmlFor="name">Name</Label>
              <Input id="name" value={name} onChange={(e) => setName(e.target.value)}
                     placeholder="e.g. fuzzing-core, web-research" required />
            </div>
            <Button type="submit" disabled={create.isPending || !name.trim()}>
              <Plus size={14} /> {create.isPending ? "Creating…" : "Create team"}
            </Button>
          </form>
          {create.isError ? (
            <p className="text-xs text-danger mt-2">Failed (name may already exist).</p>
          ) : null}
        </CardBody>
      </Card>

      <Card>
        <CardHeader><CardTitle>All teams ({teams.data?.length ?? 0})</CardTitle></CardHeader>
        {teams.isLoading ? (
          <CardBody><p className="text-sm text-fgmuted">Loading…</p></CardBody>
        ) : !teams.data || teams.data.length === 0 ? (
          <CardBody><Empty icon={<Users2 size={28} />} title="No teams yet" /></CardBody>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Name</TH>
                <TH className="w-32">Members</TH>
                <TH className="w-44"></TH>
              </TR>
            </THead>
            <tbody>
              {teams.data.map((t) => (
                <TeamRow key={t.id} t={t} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}

function TeamRow({ t }: { t: Team }) {
  const qc = useQueryClient();
  const [name, setName] = useState(t.name);
  const [savedAt, setSavedAt] = useState(0);
  const dirty = name.trim() !== t.name;

  const save = useMutation({
    mutationFn: () => api(`/teams/${t.id}`, { method: "PATCH", body: { name: name.trim() } }),
    onSuccess: () => {
      setSavedAt(Date.now());
      qc.invalidateQueries({ queryKey: ["teams"] });
    },
  });
  const del = useMutation({
    mutationFn: () => api(`/teams/${t.id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["teams"] }),
  });

  const justSaved = Date.now() - savedAt < 2000;

  return (
    <TR>
      <TD>
        <Input value={name} onChange={(e) => setName(e.target.value)} />
      </TD>
      <TD className="text-fgmuted">{t.member_count}</TD>
      <TD>
        <div className="flex items-center gap-2 flex-wrap">
          <Button size="sm" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "…" : justSaved ? <><Check size={12}/>saved</> : "Rename"}
          </Button>
          <Button size="sm" variant="ghost"
                  onClick={() => {
                    if (confirm(`Delete team "${t.name}"? Members will be unassigned.`)) {
                      del.mutate();
                    }
                  }}
                  disabled={del.isPending}>
            <Trash2 size={12} />
          </Button>
        </div>
      </TD>
    </TR>
  );
}

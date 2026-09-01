import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Card, CardBody } from "@/components/ui/Card";
import { Input, Label } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Empty } from "@/components/ui/Empty";
import { useState, type FormEvent } from "react";
import { FolderGit2, Plus } from "lucide-react";

type Project = {
  id: string; name: string; description: string;
  created_by: string; created_at: string; updated_at: string;
};

export function Projects() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [showForm, setShowForm] = useState(false);

  const create = useMutation({
    mutationFn: () => api<Project>("/projects", { method: "POST", body: { name, description } }),
    onSuccess: () => {
      setName(""); setDescription(""); setShowForm(false);
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (name.trim()) create.mutate();
  }

  return (
    <>
      <PageHeader
        title="Products"
        subtitle="Group runs, scans, and reports. Members of a product see everything inside it."
        action={
          <Button onClick={() => setShowForm((v) => !v)}>
            <Plus size={14} /> New product
          </Button>
        }
      />

      {showForm ? (
        <Card className="mb-5">
          <CardBody>
            <form onSubmit={onSubmit} className="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
              <div className="md:col-span-1">
                <Label htmlFor="name">Name</Label>
                <Input id="name" autoFocus value={name} onChange={(e) => setName(e.target.value)} required />
              </div>
              <div className="md:col-span-2">
                <Label htmlFor="description">Description</Label>
                <Input id="description" value={description} onChange={(e) => setDescription(e.target.value)} />
              </div>
              <div className="md:col-span-3 flex items-center gap-2">
                <Button type="submit" disabled={create.isPending}>
                  {create.isPending ? "Creating…" : "Create"}
                </Button>
                <Button type="button" variant="secondary" onClick={() => setShowForm(false)}>Cancel</Button>
                {create.isError ? (
                  <span className="text-xs text-danger ml-2">Failed to create.</span>
                ) : null}
              </div>
            </form>
          </CardBody>
        </Card>
      ) : null}

      {q.isLoading ? (
        <div className="text-sm text-fgmuted">Loading…</div>
      ) : !q.data || q.data.length === 0 ? (
        <Empty
          icon={<FolderGit2 size={28} />}
          title="No products yet"
          hint="Create one to start grouping runs and granting access to teammates."
        />
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>Name</TH>
              <TH>Description</TH>
              <TH>Created</TH>
            </TR>
          </THead>
          <tbody>
            {q.data.map((p) => (
              <TR key={p.id} className="hover:bg-muted/40">
                <TD>
                  <Link to={`/products/${p.id}`} className="text-primary hover:underline font-medium">
                    {p.name}
                  </Link>
                </TD>
                <TD className="text-fgmuted text-xs">{p.description}</TD>
                <TD className="text-fgmuted text-xs whitespace-nowrap">
                  {new Date(p.created_at).toLocaleDateString()}
                </TD>
              </TR>
            ))}
          </tbody>
        </Table>
      )}
    </>
  );
}

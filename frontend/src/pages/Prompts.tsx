import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input, Label, Textarea } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Empty } from "@/components/ui/Empty";
import { MessageSquareText, Plus, Pencil, Trash2, Upload, X } from "lucide-react";

type Prompt = {
  id: string; title: string; description: string; category: string;
  body: string; created_by_email: string | null; created_at: string; updated_at: string;
};

function variablesOf(body: string): string[] {
  const set = new Set<string>();
  for (const m of body.matchAll(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g)) set.add(m[1]);
  return [...set];
}

const EMPTY = { id: "", title: "", description: "", category: "", body: "" };

export function Prompts() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const q = useQuery({ queryKey: ["prompts"], queryFn: () => api<Prompt[]>("/prompts") });
  const [form, setForm] = useState<typeof EMPTY>(EMPTY);
  const [editing, setEditing] = useState(false);

  const save = useMutation({
    mutationFn: () => {
      const payload = { title: form.title, description: form.description, category: form.category, body: form.body };
      return form.id
        ? api(`/prompts/${form.id}`, { method: "PUT", body: payload })
        : api("/prompts", { method: "POST", body: payload });
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["prompts"] }); reset(); },
  });
  const del = useMutation({
    mutationFn: (id: string) => api(`/prompts/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["prompts"] }),
  });

  function reset() { setForm(EMPTY); setEditing(false); }
  function edit(p: Prompt) {
    setForm({ id: p.id, title: p.title, description: p.description, category: p.category, body: p.body });
    setEditing(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function submit(e: FormEvent) { e.preventDefault(); if (form.title.trim() && form.body.trim()) save.mutate(); }
  async function loadFile(f: File | undefined) {
    if (!f) return;
    const text = await f.text();
    setForm((s) => ({ ...s, body: text, title: s.title || f.name.replace(/\.[^.]+$/, "") }));
    setEditing(true);
  }

  const prompts = q.data ?? [];
  const formVars = useMemo(() => variablesOf(form.body), [form.body]);

  return (
    <>
      <PageHeader
        title="Prompts"
        subtitle="A shared library of prompt templates for the Workbench. Use {{variables}} like {{product}} so a prompt works across products."
      />

      <Card className="mb-4">
        <CardHeader className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            {editing ? <Pencil size={14} className="text-primary" /> : <Plus size={14} className="text-primary" />}
            {form.id ? "Edit prompt" : "New prompt"}
          </CardTitle>
          <div className="flex items-center gap-2">
            <input ref={fileRef} type="file" accept=".md,.txt,text/plain,text/markdown"
                   className="hidden" onChange={(e) => loadFile(e.target.files?.[0])} />
            <Button type="button" size="sm" variant="secondary" onClick={() => fileRef.current?.click()}>
              <Upload size={12} /> Load from file
            </Button>
            {editing ? (
              <Button type="button" size="sm" variant="ghost" onClick={reset}><X size={12} /> Cancel</Button>
            ) : null}
          </div>
        </CardHeader>
        <CardBody>
          <form onSubmit={submit} className="space-y-3">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div className="md:col-span-2">
                <Label htmlFor="t">Title</Label>
                <Input id="t" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })}
                       placeholder="e.g. Compliance audit (harness)" />
              </div>
              <div>
                <Label htmlFor="c">Category</Label>
                <Input id="c" value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}
                       placeholder="compliance, pentest, …" />
              </div>
            </div>
            <div>
              <Label htmlFor="d">Description</Label>
              <Input id="d" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
                     placeholder="One line on what this prompt does" />
            </div>
            <div>
              <Label htmlFor="b">Body</Label>
              <Textarea id="b" rows={12} value={form.body}
                        onChange={(e) => setForm({ ...form, body: e.target.value })}
                        className="font-mono text-xs"
                        placeholder="Write the prompt. Use {{product}}, {{host}}, … for fill-in keywords." />
              {formVars.length > 0 ? (
                <div className="mt-1.5 flex items-center gap-1.5 flex-wrap text-[11px] text-fgmuted">
                  variables:
                  {formVars.map((v) => <Badge key={v} tone="muted">{`{{${v}}}`}</Badge>)}
                </div>
              ) : (
                <p className="text-[11px] text-fgmuted mt-1">No variables yet — add {`{{product}}`} where the product name goes.</p>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button type="submit" disabled={save.isPending || !form.title.trim() || !form.body.trim()}>
                {save.isPending ? "Saving…" : form.id ? "Save changes" : "Add prompt"}
              </Button>
              {save.isError ? <span className="text-xs text-danger">Couldn't save.</span> : null}
            </div>
          </form>
        </CardBody>
      </Card>

      {q.isLoading ? (
        <p className="text-sm text-fgmuted">Loading…</p>
      ) : prompts.length === 0 ? (
        <Empty icon={<MessageSquareText size={28} />} title="No prompts yet"
               hint="Add one above, or load a .md/.txt file." />
      ) : (
        <div className="space-y-3">
          {prompts.map((p) => {
            const vars = variablesOf(p.body);
            return (
              <Card key={p.id}>
                <CardBody>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-medium">{p.title}</span>
                        {p.category ? <Badge tone="primary">{p.category}</Badge> : null}
                        {vars.map((v) => <Badge key={v} tone="muted">{`{{${v}}}`}</Badge>)}
                      </div>
                      {p.description ? <div className="text-xs text-fgmuted mt-1">{p.description}</div> : null}
                      <pre className="mt-2 text-[11px] bg-muted/40 border border-border rounded-md p-2 max-h-40 overflow-auto whitespace-pre-wrap">{p.body}</pre>
                      <div className="text-[11px] text-fgmuted mt-1">by {p.created_by_email?.split("@")[0] ?? "—"}</div>
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      <Button size="sm" variant="ghost" onClick={() => edit(p)}><Pencil size={13} /></Button>
                      <Button size="sm" variant="ghost" disabled={del.isPending}
                              onClick={() => { if (confirm(`Delete prompt “${p.title}”?`)) del.mutate(p.id); }}>
                        <Trash2 size={13} />
                      </Button>
                    </div>
                  </div>
                </CardBody>
              </Card>
            );
          })}
        </div>
      )}
    </>
  );
}

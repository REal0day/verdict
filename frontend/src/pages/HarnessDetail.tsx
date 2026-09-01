import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useState } from "react";
import { api, getToken } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input, Label, Select, Textarea } from "@/components/ui/Input";
import { Badge } from "@/components/ui/Badge";
import { useAuth } from "@/lib/auth";
import {
  ArrowLeft, Check, Download, FileText, Trash2, Wrench, Pencil, Plus, X, Save,
} from "lucide-react";

type Project = { id: string; name: string; i_am_owner?: boolean; i_am_member?: boolean };
type HarnessFile = { relpath: string; size_bytes: number; content_type: string; sha256: string };
type Harness = {
  id: string; user_id: string;
  project_id: string | null; project_name: string | null;
  name: string; description: string;
  file_count: number; total_bytes: number;
  created_at: string; updated_at: string;
  files: HarnessFile[];
};

export function HarnessDetail() {
  const { harness_id = "" } = useParams();
  const qc = useQueryClient();
  const nav = useNavigate();
  const { me } = useAuth();

  const q = useQuery({
    queryKey: ["harness", harness_id],
    queryFn: () => api<Harness>(`/harnesses/${harness_id}`),
  });
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [projectId, setProjectId] = useState("");
  const [savedAt, setSavedAt] = useState(0);

  useEffect(() => {
    if (q.data) {
      setName(q.data.name);
      setDescription(q.data.description);
      setProjectId(q.data.project_id || "");
    }
  }, [q.data]);

  const save = useMutation({
    mutationFn: () =>
      api<Harness>(`/harnesses/${harness_id}`, {
        method: "PATCH",
        body: {
          name,
          description,
          project_id: projectId || null,
        },
      }),
    onSuccess: () => {
      setSavedAt(Date.now());
      setTimeout(() => setSavedAt(0), 2400);
      qc.invalidateQueries({ queryKey: ["harness", harness_id] });
      qc.invalidateQueries({ queryKey: ["harnesses"] });
    },
  });

  const destroy = useMutation({
    mutationFn: () => api(`/harnesses/${harness_id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["harnesses"] });
      nav("/harnesses");
    },
  });

  // ---- file editing ----
  // editPath: null = closed, "" = creating a new file, else the relpath open.
  const [editPath, setEditPath] = useState<string | null>(null);
  const [newPath, setNewPath] = useState("");
  const [editText, setEditText] = useState("");
  const [loadingFile, setLoadingFile] = useState(false);
  const [fileErr, setFileErr] = useState<string | null>(null);

  const saveFile = useMutation({
    mutationFn: (payload: { relpath: string; content: string }) =>
      api(`/harnesses/${harness_id}/files`, { method: "PUT", body: payload }),
    onSuccess: () => {
      setEditPath(null); setNewPath(""); setEditText(""); setFileErr(null);
      qc.invalidateQueries({ queryKey: ["harness", harness_id] });
    },
    onError: (e: any) => setFileErr(e?.message || "Save failed"),
  });

  const delFile = useMutation({
    mutationFn: (relpath: string) =>
      api(`/harnesses/${harness_id}/files?relpath=${encodeURIComponent(relpath)}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["harness", harness_id] }),
  });

  async function openEdit(relpath: string) {
    setFileErr(null);
    setLoadingFile(true);
    setEditPath(relpath);
    try {
      const tok = getToken();
      const r = await fetch(`/harnesses/${harness_id}/files/raw?relpath=${encodeURIComponent(relpath)}`,
        { headers: tok ? { Authorization: `Bearer ${tok}` } : {} });
      setEditText(r.ok ? await r.text() : "");
    } finally {
      setLoadingFile(false);
    }
  }
  function openNew() { setFileErr(null); setEditPath(""); setNewPath(""); setEditText(""); }
  function submitFile() {
    const rel = (editPath || newPath).trim();
    if (!rel) { setFileErr("File path is required"); return; }
    saveFile.mutate({ relpath: rel, content: editText });
  }

  if (q.isLoading) return <div className="text-sm text-fgmuted">Loading…</div>;
  if (q.isError || !q.data) return <div className="text-sm text-danger">Harness not found.</div>;
  const h = q.data;
  const assignable = (projects.data || []).filter((p) => p.i_am_owner || p.i_am_member);
  const dirty =
    name !== h.name ||
    description !== h.description ||
    (projectId || null) !== (h.project_id || null);
  const justSaved = Date.now() - savedAt < 2400;
  const canEdit = !!me && (me.role === "admin" || me.id === h.user_id);

  function fileUrl(relpath: string) {
    return `/harnesses/${h.id}/files/raw?relpath=${encodeURIComponent(relpath)}`;
  }

  async function downloadOne(relpath: string) {
    const tok = getToken();
    const r = await fetch(fileUrl(relpath), {
      headers: tok ? { Authorization: `Bearer ${tok}` } : {},
    });
    if (!r.ok) return;
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = relpath.split("/").pop() || "file";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title={h.name}
        subtitle={`${h.file_count} files · ${(h.total_bytes / 1024).toFixed(1)} KB`}
        action={
          <Link to="/harnesses" className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
            <ArrowLeft size={12} /> Back to harnesses
          </Link>
        }
      />

      <Card>
        <CardHeader className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Wrench size={14} /> Details
          </CardTitle>
          <div className="flex items-center gap-2">
            {justSaved ? (
              <span className="text-xs text-success inline-flex items-center gap-1">
                <Check size={12} /> Saved!
              </span>
            ) : dirty ? (
              <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
                {save.isPending ? "Saving…" : "Save"}
              </Button>
            ) : null}
            <Button variant="ghost" size="sm"
                    onClick={() => { if (confirm(`Delete harness '${h.name}'?`)) destroy.mutate(); }}>
              <Trash2 size={12} /> Delete
            </Button>
          </div>
        </CardHeader>
        <CardBody className="space-y-3">
          <div>
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div>
            <Label>Description</Label>
            <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div>
            <Label>Product</Label>
            <Select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">— none —</option>
              {assignable.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </Select>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <FileText size={14} /> Files ({h.files.length})
          </CardTitle>
          {canEdit ? (
            <Button size="sm" variant="secondary" onClick={openNew}>
              <Plus size={12} /> New file
            </Button>
          ) : null}
        </CardHeader>
        <CardBody className="space-y-1 text-xs font-mono max-h-[60vh] overflow-y-auto">
          {h.files.map((f) => (
            <div key={f.relpath} className="flex items-center justify-between gap-2 hover:bg-muted/30 px-2 py-1 rounded">
              <span className="truncate" title={f.relpath}>{f.relpath}</span>
              <span className="text-fgmuted shrink-0 inline-flex items-center gap-2">
                {f.size_bytes} B
                <Badge tone="muted">{f.content_type.split("/")[0]}</Badge>
                {canEdit ? (
                  <button type="button" className="text-primary hover:underline" title="Edit"
                          onClick={() => openEdit(f.relpath)}>
                    <Pencil size={12} className="inline" />
                  </button>
                ) : null}
                <button type="button" className="text-primary hover:underline" title="Download"
                        onClick={() => downloadOne(f.relpath)}>
                  <Download size={12} className="inline" />
                </button>
                {canEdit ? (
                  <button type="button" className="text-danger hover:underline" title="Delete"
                          disabled={delFile.isPending}
                          onClick={() => { if (confirm(`Delete ${f.relpath}?`)) delFile.mutate(f.relpath); }}>
                    <Trash2 size={12} className="inline" />
                  </button>
                ) : null}
              </span>
            </div>
          ))}
          {h.files.length === 0 ? <p className="text-fgmuted px-2 py-1">No files.</p> : null}
        </CardBody>
      </Card>

      {editPath !== null ? (
        <Card>
          <CardHeader className="flex items-center justify-between">
            <CardTitle className="flex items-center gap-2 font-mono text-xs">
              <Pencil size={13} className="text-primary" />
              {editPath === "" ? "New file" : editPath}
            </CardTitle>
            <button type="button" className="text-fgmuted hover:text-fg"
                    onClick={() => { setEditPath(null); setFileErr(null); }}>
              <X size={14} />
            </button>
          </CardHeader>
          <CardBody className="space-y-2">
            {editPath === "" ? (
              <div>
                <Label htmlFor="np">File path</Label>
                <Input id="np" value={newPath} onChange={(e) => setNewPath(e.target.value)}
                       placeholder="e.g. controls/controls.yaml" className="font-mono" />
              </div>
            ) : null}
            {loadingFile ? (
              <p className="text-xs text-fgmuted">Loading…</p>
            ) : (
              <Textarea value={editText} onChange={(e) => setEditText(e.target.value)}
                        rows={20} className="font-mono text-xs" spellCheck={false} />
            )}
            {fileErr ? <p className="text-xs text-danger">{fileErr}</p> : null}
            <div className="flex items-center gap-2">
              <Button onClick={submitFile} disabled={saveFile.isPending || loadingFile}>
                <Save size={14} /> {saveFile.isPending ? "Saving…" : "Save file"}
              </Button>
              <Button variant="ghost" onClick={() => { setEditPath(null); setFileErr(null); }}>Cancel</Button>
            </div>
          </CardBody>
        </Card>
      ) : null}
    </div>
  );
}

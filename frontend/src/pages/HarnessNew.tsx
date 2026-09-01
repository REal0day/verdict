import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { useRef, useState } from "react";
import { api, getToken } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input, Label, Select, Textarea } from "@/components/ui/Input";
import { Empty } from "@/components/ui/Empty";
import { ArrowLeft, FolderUp, Upload, Wrench } from "lucide-react";

type Project = { id: string; name: string; i_am_owner?: boolean; i_am_member?: boolean };

/**
 * POST /harnesses
 *
 * Folder upload that lands directly in the DB (no AI planning step — a
 * harness is just a static bundle of files Claude will be run inside).
 */
export function HarnessNew() {
  const nav = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });
  const assignable = (projects.data || []).filter((p) => p.i_am_owner || p.i_am_member);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [projectId, setProjectId] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function pick(list: FileList | null) {
    if (!list) return;
    setFiles(Array.from(list));
    // Pre-fill name from the top-level folder name if it's empty.
    if (!name && list.length > 0) {
      const wkrp = (list[0] as any).webkitRelativePath as string | undefined;
      if (wkrp && wkrp.includes("/")) setName(wkrp.split("/")[0]);
    }
  }

  const total = files.reduce((n, f) => n + f.size, 0);

  async function upload() {
    if (!name.trim() || files.length === 0) return;
    setUploading(true); setError(null);
    try {
      const fd = new FormData();
      fd.append("name", name.trim());
      fd.append("description", description.trim());
      if (projectId) fd.append("project_id", projectId);
      for (const f of files) {
        const wkrp = (f as any).webkitRelativePath as string | undefined;
        // Strip leading top-level dir for the same reason as imports.
        const rel = wkrp && wkrp.includes("/") ? wkrp.split("/").slice(1).join("/") : (wkrp || f.name);
        fd.append("relpaths", rel || f.name);
        fd.append("files", f, f.name);
      }
      const tok = getToken();
      const resp = await fetch("/harnesses", {
        method: "POST",
        headers: tok ? { Authorization: `Bearer ${tok}` } : {},
        body: fd,
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`upload failed (${resp.status}): ${text}`);
      }
      const out = await resp.json();
      nav(`/harnesses/${out.id}`);
    } catch (e: any) {
      setError(e.message || String(e));
      setUploading(false);
    }
  }

  return (
    <>
      <PageHeader
        title="New harness"
        subtitle="Upload a folder of prompts, tools, or scaffolding for Claude runs to reference."
        action={
          <Link to="/harnesses" className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
            <ArrowLeft size={12} /> Back to harnesses
          </Link>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Wrench size={14} className="text-primary" /> Harness details
          </CardTitle>
        </CardHeader>
        <CardBody className="space-y-3">
          <div>
            <Label htmlFor="hn-name">Name</Label>
            <Input id="hn-name" value={name} onChange={(e) => setName(e.target.value)}
                   placeholder="e.g. 'Acme Gateway audit harness'" />
          </div>
          <div>
            <Label htmlFor="hn-desc">Description (optional)</Label>
            <Textarea id="hn-desc" rows={2} value={description}
                      onChange={(e) => setDescription(e.target.value)}
                      placeholder="What's in this harness and how it changes Claude's behaviour." />
          </div>
          <div>
            <Label htmlFor="hn-proj">Product (optional)</Label>
            <Select id="hn-proj" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">— none —</option>
              {assignable.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </Select>
          </div>

          <input
            ref={inputRef}
            type="file"
            multiple
            // @ts-expect-error — folder picker attrs
            webkitdirectory=""
            directory=""
            className="hidden"
            onChange={(e) => pick(e.target.files)}
          />
          <div className="flex items-center gap-2">
            <Button type="button" variant="secondary" onClick={() => inputRef.current?.click()}>
              <FolderUp size={14} /> Choose folder…
            </Button>
            {files.length > 0 ? (
              <span className="text-xs text-fgmuted">
                {files.length} file{files.length === 1 ? "" : "s"}, {(total / 1024).toFixed(1)} KB
              </span>
            ) : (
              <span className="text-xs text-fgmuted">No folder picked yet.</span>
            )}
          </div>

          {files.length > 0 ? (
            <div className="border border-border rounded-md max-h-48 overflow-y-auto text-xs font-mono">
              {files.slice(0, 200).map((f, i) => {
                const wkrp = (f as any).webkitRelativePath as string | undefined;
                return (
                  <div key={i} className="px-3 py-1 border-b border-border last:border-b-0 flex justify-between gap-2">
                    <span className="truncate">{wkrp || f.name}</span>
                    <span className="text-fgmuted shrink-0">{f.size} B</span>
                  </div>
                );
              })}
              {files.length > 200 ? (
                <div className="px-3 py-1 text-fgmuted text-center">
                  … and {files.length - 200} more
                </div>
              ) : null}
            </div>
          ) : (
            <Empty
              icon={<FolderUp size={28} />}
              title="No folder picked"
              hint="Pick the directory that you want Claude to run inside."
            />
          )}

          {error ? <p className="text-sm text-danger">{error}</p> : null}

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-border">
            <Button variant="ghost" onClick={() => { setFiles([]); setName(""); setDescription(""); setProjectId(""); }}>
              Reset
            </Button>
            <Button onClick={upload} disabled={uploading || files.length === 0 || !name.trim()}>
              <Upload size={14} /> {uploading ? "Uploading…" : "Create harness"}
            </Button>
          </div>
        </CardBody>
      </Card>
    </>
  );
}

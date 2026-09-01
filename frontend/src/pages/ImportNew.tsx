import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input, Label } from "@/components/ui/Input";
import { Empty } from "@/components/ui/Empty";
import { getToken } from "@/lib/api";
import { ArrowLeft, FolderUp, Upload, FileArchive } from "lucide-react";

type FolderImportOut = {
  id: string;
  status: string;
};

export function ImportNew() {
  const nav = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const zipRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [label, setLabel] = useState("");
  const [progress, setProgress] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function onPick(list: FileList | null) {
    if (!list) return;
    setFiles(Array.from(list));
  }

  // Zips are additive: pick several at once, or click again to add more.
  function addZips(list: FileList | null) {
    if (!list || list.length === 0) return;
    const incoming = Array.from(list);
    setFiles((cur) => {
      const seen = new Set(cur.map((f) => `${f.name}:${f.size}`));
      return [...cur, ...incoming.filter((f) => !seen.has(`${f.name}:${f.size}`))];
    });
  }

  const total = files.reduce((n, f) => n + f.size, 0);
  // Files come with a `webkitRelativePath` when picked via the directory input.
  const hasRelpaths = files.length > 0 && (files[0] as any).webkitRelativePath;

  async function upload() {
    if (files.length === 0) return;
    setUploading(true);
    setError(null);
    setProgress(0);
    try {
      const fd = new FormData();
      fd.append("label", label);
      for (const f of files) {
        // Strip the leading top-level folder from the path so two uploads
        // of "foo/" don't end up nested as "foo/foo/...". webkitRelativePath
        // looks like "topdir/sub/file.md".
        const wkrp = (f as any).webkitRelativePath as string | undefined;
        const rel = wkrp && wkrp.includes("/") ? wkrp.split("/").slice(1).join("/") : (wkrp || f.name);
        fd.append("relpaths", rel || f.name);
        fd.append("files", f, f.name);
      }
      const tok = getToken();
      const resp = await fetch("/imports", {
        method: "POST",
        headers: tok ? { Authorization: `Bearer ${tok}` } : {},
        body: fd,
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`upload failed (${resp.status}): ${text}`);
      }
      const out: FolderImportOut = await resp.json();
      nav(`/imports/${out.id}`);
    } catch (e: any) {
      setError(e.message || String(e));
      setUploading(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Import folder"
        subtitle="Upload a directory — or a .zip of source code — of reports, POCs, and notes. Claude will figure out how to organize them."
        action={
          <Link to="/" className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
            <ArrowLeft size={12} /> Back to reports
          </Link>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FolderUp size={16} className="text-primary" /> Pick a folder
          </CardTitle>
        </CardHeader>
        <CardBody className="space-y-4">
          <div>
            <Label htmlFor="label">Label (optional)</Label>
            <Input
              id="label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. 'libfoo audit — June run'"
            />
          </div>

          <input
            ref={inputRef}
            type="file"
            multiple
            // The two non-standard attrs let the browser show a folder picker.
            // @ts-expect-error — non-standard but widely supported.
            webkitdirectory=""
            directory=""
            className="hidden"
            onChange={(e) => onPick(e.target.files)}
          />
          <input
            ref={zipRef}
            type="file"
            multiple
            accept=".zip,application/zip"
            className="hidden"
            onClick={(e) => { (e.target as HTMLInputElement).value = ""; }}
            onChange={(e) => addZips(e.target.files)}
          />

          <div className="flex items-center gap-2">
            <Button type="button" onClick={() => inputRef.current?.click()} disabled={uploading}>
              <FolderUp size={14} /> Choose folder…
            </Button>
            <Button type="button" variant="secondary" onClick={() => zipRef.current?.click()} disabled={uploading}>
              <FileArchive size={14} /> Choose .zip…
            </Button>
            {files.length > 0 ? (
              <span className="text-xs text-fgmuted">
                {files.length} file{files.length === 1 ? "" : "s"}, {(total / 1024).toFixed(1)} KB total
              </span>
            ) : (
              <span className="text-xs text-fgmuted">No folder picked yet.</span>
            )}
          </div>

          {files.length > 0 && !hasRelpaths ? (
            <p className="text-xs text-warning">
              Your browser dropped the directory paths — files will upload flat.
              Try Chrome/Edge/Firefox on desktop for full folder support.
            </p>
          ) : null}

          {files.length > 0 ? (
            <div className="border border-border rounded-md max-h-64 overflow-y-auto text-xs font-mono">
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
              title="Nothing picked yet"
              hint="Choose a folder, or a .zip of source code — we unpack it server-side and keep the directory structure intact."
            />
          )}

          {error ? <p className="text-sm text-danger">{error}</p> : null}

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-border">
            <Button variant="ghost" onClick={() => { setFiles([]); setLabel(""); }}>
              Reset
            </Button>
            <Button onClick={upload} disabled={uploading || files.length === 0}>
              <Upload size={14} />
              {uploading ? "Uploading…" : `Upload ${files.length || ""} file${files.length === 1 ? "" : "s"}`}
            </Button>
          </div>

          {uploading ? (
            <div className="h-1.5 bg-muted rounded-full overflow-hidden">
              <div className="h-full bg-primary transition-all" style={{ width: `${progress}%` }} />
            </div>
          ) : null}
        </CardBody>
      </Card>
    </>
  );
}

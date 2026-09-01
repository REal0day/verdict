import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useEffect, useState } from "react";
import { api, getToken } from "@/lib/api";
import { downloadFile } from "@/lib/download";
import { Card, CardBody } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { ArrowLeft, Download, AlertTriangle } from "lucide-react";

type Att = {
  id: string; user_id: string; session_id: string | null;
  scan_id: string | null; finding_id: string | null;
  filename: string; original_path: string | null;
  content_type: string; sha256: string; size_bytes: number;
  created_at: string;
};

// Mirror the server-side textual/image/pdf heuristics so we render inline.
const TEXT_PREFIXES = ["text/", "application/json", "application/xml"];
const TEXT_EXTS = new Set([
  "txt","md","log","json","xml","yml","yaml","csv","py","c","h","cpp","hpp",
  "rs","go","java","js","ts","tsx","jsx","sh","rb","php","html","css","sql",
  "diff","patch","ini","toml","conf",
]);
const PREVIEW_MAX = 1 * 1024 * 1024; // 1 MiB

function isText(ct: string, name: string) {
  if (TEXT_PREFIXES.some((p) => ct.toLowerCase().startsWith(p))) return true;
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return TEXT_EXTS.has(ext);
}
function isImage(ct: string) { return ct.toLowerCase().startsWith("image/"); }
function isPdf(ct: string, name: string) {
  return ct.toLowerCase() === "application/pdf" || name.toLowerCase().endsWith(".pdf");
}

export function AttachmentView() {
  const { att_id = "" } = useParams();

  const meta = useQuery({
    queryKey: ["att-meta", att_id],
    queryFn: () => api<Att>(`/attachments/${att_id}/meta`),
  });

  const [textPreview, setTextPreview] = useState<{ text: string; truncated: boolean } | null>(null);
  const [textLoading, setTextLoading] = useState(false);
  const [imgUrl, setImgUrl] = useState<string | null>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);

  // For text: fetch + decode in JS (so we can apply the 1 MiB cap).
  useEffect(() => {
    if (!meta.data) return;
    const a = meta.data;
    if (isText(a.content_type, a.filename)) {
      setTextLoading(true);
      const tok = getToken();
      fetch(`/attachments/${a.id}/inline`, {
        headers: tok ? { Authorization: `Bearer ${tok}` } : {},
      })
        .then(async (r) => {
          const buf = await r.arrayBuffer();
          const truncated = buf.byteLength > PREVIEW_MAX;
          const slice = truncated ? buf.slice(0, PREVIEW_MAX) : buf;
          const text = new TextDecoder("utf-8", { fatal: false }).decode(slice);
          setTextPreview({ text, truncated });
        })
        .catch(() => setTextPreview({ text: "(failed to load preview)", truncated: false }))
        .finally(() => setTextLoading(false));
    } else if (isImage(a.content_type)) {
      blobUrlFor(a.id).then(setImgUrl).catch(() => setImgUrl(null));
    } else if (isPdf(a.content_type, a.filename)) {
      blobUrlFor(a.id).then(setPdfUrl).catch(() => setPdfUrl(null));
    }
  }, [meta.data?.id]);

  if (meta.isLoading) return <div className="text-sm text-fgmuted">Loading…</div>;
  if (meta.isError || !meta.data) return <div className="text-sm text-danger">Attachment not found.</div>;
  const a = meta.data;
  const back = a.scan_id ? `/scans/${a.scan_id}` :
               a.session_id ? `/runs/${a.session_id}` : "/";
  const backLabel = a.scan_id ? "back to scan" :
                    a.session_id ? "back to run" : "back";

  return (
    <div className="space-y-5">
      <div>
        <Link to={back} className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
          <ArrowLeft size={12} /> {backLabel}
        </Link>
        <div className="mt-2 flex items-baseline justify-between gap-3 flex-wrap">
          <h1 className="text-2xl font-semibold break-all">{a.filename}</h1>
          <Button variant="secondary"
                  onClick={() => downloadFile(`/attachments/${a.id}/download`, a.filename)}>
            <Download size={14} /> Download
          </Button>
        </div>
        <p className="text-xs text-fgmuted mt-1">
          {a.content_type} · {(a.size_bytes / 1024).toFixed(1)} KB · uploaded {fmt(a.created_at)}
        </p>
      </div>

      {/* Preview */}
      {isText(a.content_type, a.filename) ? (
        <Card>
          <CardBody className="p-0">
            {textPreview?.truncated ? (
              <div className="flex items-center gap-2 px-4 py-2 border-b border-border bg-warning/10 text-xs text-warning">
                <AlertTriangle size={12} /> Preview truncated to 1 MB.
                <button type="button"
                        onClick={() => downloadFile(`/attachments/${a.id}/download`, a.filename)}
                        className="text-warning underline bg-transparent border-0 p-0 h-auto cursor-pointer inline">
                  Download
                </button>
                for the full file.
              </div>
            ) : null}
            <pre className="m-0 p-4 max-h-[75vh] overflow-auto whitespace-pre-wrap break-words text-[12.5px] leading-[1.5] font-mono">
              {textLoading ? "Loading…" : textPreview?.text ?? ""}
            </pre>
          </CardBody>
        </Card>
      ) : isImage(a.content_type) ? (
        <Card>
          <CardBody className="flex justify-center bg-bg">
            {imgUrl ? (
              <img src={imgUrl} alt={a.filename} className="max-w-full max-h-[80vh] rounded-md border border-border" />
            ) : (
              <div className="text-sm text-fgmuted py-12">Loading image…</div>
            )}
          </CardBody>
        </Card>
      ) : isPdf(a.content_type, a.filename) ? (
        <Card>
          <CardBody className="p-0">
            {pdfUrl ? (
              <iframe src={pdfUrl} title={a.filename}
                      className="w-full h-[80vh] border-0 rounded-md" />
            ) : (
              <div className="text-sm text-fgmuted py-12 text-center">Loading PDF…</div>
            )}
          </CardBody>
        </Card>
      ) : (
        <Card>
          <CardBody className="text-center py-12 space-y-3">
            <p className="text-sm text-fgmuted">
              Binary file ({a.content_type}) — can't be previewed in the browser.
            </p>
            <Button onClick={() => downloadFile(`/attachments/${a.id}/download`, a.filename)}>
              <Download size={14} /> Download
            </Button>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

async function blobUrlFor(id: string): Promise<string> {
  const tok = getToken();
  const r = await fetch(`/attachments/${id}/inline`, {
    headers: tok ? { Authorization: `Bearer ${tok}` } : {},
  });
  const blob = await r.blob();
  return URL.createObjectURL(blob);
}

function fmt(iso: string) {
  return new Date(iso).toLocaleString(undefined,
    { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

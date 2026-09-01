import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import { Button } from "@/components/ui/Button";
import { Download, Table as TableIcon, FileCode, FileText } from "lucide-react";

type ReplyKind = "csv" | "json" | "markdown";

export function ChatReply({ text, defaultFilename }: { text: string; defaultFilename?: string }) {
  const kind = useMemo(() => detect(text), [text]);
  if (kind === "csv") return <CSVView text={text} defaultFilename={defaultFilename || "claude-output.csv"} />;
  if (kind === "json") return <JSONView text={text} defaultFilename={defaultFilename || "claude-output.json"} />;
  return (
    <div className="prose-irs">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

/** ------------------------- detection ------------------------- **/

function detect(text: string): ReplyKind {
  const t = text.trim();
  if (!t) return "markdown";

  // Strip a single leading/trailing code fence if Claude ignored the prompt.
  const stripped = t.replace(/^```[a-z]*\n/i, "").replace(/```$/, "").trim();

  // JSON: starts with { or [ and parses cleanly.
  if (/^[{[]/.test(stripped)) {
    try {
      JSON.parse(stripped);
      return "json";
    } catch { /* fallthrough */ }
  }

  // CSV: first 3+ lines all have the same comma count (>=1) and at least
  // one row beyond the header. Ignore blank lines.
  const lines = stripped.split(/\r?\n/).filter((l) => l.length > 0);
  if (lines.length >= 2) {
    const counts = lines.slice(0, Math.min(5, lines.length)).map(countCommasOutsideQuotes);
    if (counts[0] >= 1 && counts.every((c) => c === counts[0])) {
      return "csv";
    }
  }
  return "markdown";
}

function countCommasOutsideQuotes(line: string): number {
  let count = 0, inq = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (c === '"') {
      if (inq && line[i + 1] === '"') i++;
      else inq = !inq;
    } else if (c === "," && !inq) {
      count++;
    }
  }
  return count;
}

/** ------------------------- CSV view -------------------------- **/

function CSVView({ text, defaultFilename }: { text: string; defaultFilename: string }) {
  const rows = useMemo(() => parseCSV(text), [text]);
  const [showRaw, setShowRaw] = useState(false);
  const dlUrl = useMemo(
    () => URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" })),
    [text]
  );

  const header = rows[0] || [];
  const body = rows.slice(1);

  return (
    <div>
      <div className="flex items-center justify-between mb-2 text-xs text-fgmuted">
        <span className="inline-flex items-center gap-1.5"><TableIcon size={12} /> CSV · {body.length} rows · {header.length} cols</span>
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => setShowRaw((v) => !v)}
                  className="text-fgmuted hover:text-fg inline-flex items-center gap-1">
            <FileText size={12} /> {showRaw ? "table" : "raw"}
          </button>
          <a href={dlUrl} download={defaultFilename}>
            <Button variant="secondary" size="sm"><Download size={12} /> Download</Button>
          </a>
        </div>
      </div>

      {showRaw ? (
        <pre className="bg-muted/40 border border-border rounded-md p-3 max-h-[55vh] overflow-auto text-[12.5px] font-mono whitespace-pre">
{text}
        </pre>
      ) : (
        <div className="border border-border rounded-md overflow-auto max-h-[55vh]">
          <table className="w-full text-xs">
            <thead className="bg-muted/60 sticky top-0">
              <tr>
                {header.map((h, i) => (
                  <th key={i} className="text-left px-2.5 py-1.5 font-medium text-fgmuted whitespace-nowrap border-b border-border">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((r, i) => (
                <tr key={i} className="hover:bg-muted/30">
                  {header.map((_, j) => (
                    <td key={j} className="px-2.5 py-1.5 border-b border-border/60 align-top whitespace-pre-wrap">
                      {r[j] ?? ""}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** ------------------------- JSON view ------------------------- **/

function JSONView({ text, defaultFilename }: { text: string; defaultFilename: string }) {
  const pretty = useMemo(() => {
    try {
      const stripped = text.trim().replace(/^```[a-z]*\n/i, "").replace(/```$/, "").trim();
      return JSON.stringify(JSON.parse(stripped), null, 2);
    } catch {
      return text;
    }
  }, [text]);
  const dlUrl = useMemo(
    () => URL.createObjectURL(new Blob([pretty], { type: "application/json;charset=utf-8" })),
    [pretty]
  );
  return (
    <div>
      <div className="flex items-center justify-between mb-2 text-xs text-fgmuted">
        <span className="inline-flex items-center gap-1.5"><FileCode size={12} /> JSON</span>
        <a href={dlUrl} download={defaultFilename}>
          <Button variant="secondary" size="sm"><Download size={12} /> Download</Button>
        </a>
      </div>
      <pre className="bg-muted/40 border border-border rounded-md p-3 max-h-[55vh] overflow-auto text-[12.5px] font-mono whitespace-pre">
{pretty}
      </pre>
    </div>
  );
}

/** ------------------------- CSV parser ------------------------ **/

function parseCSV(text: string): string[][] {
  const rows: string[][] = [];
  let cur: string[] = [];
  let cell = "";
  let inq = false;
  const s = text.replace(/\r\n/g, "\n").replace(/^```[a-z]*\n/i, "").replace(/```$/, "");

  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inq) {
      if (c === '"') {
        if (s[i + 1] === '"') { cell += '"'; i++; }
        else { inq = false; }
      } else {
        cell += c;
      }
    } else {
      if (c === '"') inq = true;
      else if (c === ",") { cur.push(cell); cell = ""; }
      else if (c === "\n") { cur.push(cell); cell = ""; rows.push(cur); cur = []; }
      else cell += c;
    }
  }
  if (cell.length > 0 || cur.length > 0) {
    cur.push(cell);
    rows.push(cur);
  }
  return rows;
}

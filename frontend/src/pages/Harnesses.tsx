import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Button } from "@/components/ui/Button";
import { Empty } from "@/components/ui/Empty";
import { Wrench, Plus } from "lucide-react";

type Harness = {
  id: string; user_id: string;
  project_id: string | null; project_name: string | null;
  name: string; description: string;
  file_count: number; total_bytes: number;
  created_at: string; updated_at: string;
};

export function Harnesses() {
  const q = useQuery({
    queryKey: ["harnesses"],
    queryFn: () => api<Harness[]>("/harnesses"),
  });

  const rows = q.data || [];

  return (
    <>
      <PageHeader
        title="Harnesses"
        subtitle="Folders of prompts, configs, and tooling that Claude runs inside. Each run can reference one."
        action={
          <Link to="/harnesses/new">
            <Button><Plus size={14} /> New harness</Button>
          </Link>
        }
      />

      {q.isLoading ? (
        <div className="text-sm text-fgmuted">Loading…</div>
      ) : rows.length === 0 ? (
        <Empty
          icon={<Wrench size={28} />}
          title="No harnesses yet"
          hint="Upload a folder so future Claude runs can re-use the same prompts and tools."
        />
      ) : (
        <Card>
          <Table>
            <THead>
              <TR>
                <TH>Name</TH>
                <TH>Product</TH>
                <TH>Files</TH>
                <TH>Size</TH>
                <TH>Updated</TH>
              </TR>
            </THead>
            <tbody>
              {rows.map((h) => (
                <TR key={h.id} className="hover:bg-muted/40">
                  <TD>
                    <Link to={`/harnesses/${h.id}`} className="text-primary hover:underline font-medium">
                      {h.name}
                    </Link>
                    {h.description ? (
                      <div className="text-xs text-fgmuted mt-0.5 max-w-[40ch] truncate" title={h.description}>
                        {h.description}
                      </div>
                    ) : null}
                  </TD>
                  <TD className="text-fgmuted text-xs">
                    {h.project_name ? (
                      <Link to={`/products/${h.project_id}`} className="hover:text-fg">
                        {h.project_name}
                      </Link>
                    ) : <span className="italic">—</span>}
                  </TD>
                  <TD className="tabular-nums text-xs">{h.file_count}</TD>
                  <TD className="tabular-nums text-xs">{(h.total_bytes / 1024).toFixed(1)} KB</TD>
                  <TD className="text-xs text-fgmuted whitespace-nowrap">
                    {new Date(h.updated_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
                  </TD>
                </TR>
              ))}
            </tbody>
          </Table>
        </Card>
      )}
    </>
  );
}

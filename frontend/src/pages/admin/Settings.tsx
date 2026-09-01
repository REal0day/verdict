import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input, Label } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { KeyRound, Check, AlertCircle, Sparkles } from "lucide-react";

type AISettings = {
  configured: boolean;
  source: "db" | "env" | "none";
  hint: string | null;
  model: string;
};

export function SettingsAdmin() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["ai-settings"],
    queryFn: () => api<AISettings>("/settings/ai"),
  });
  const [key, setKey] = useState("");
  const [saved, setSaved] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string } | null>(null);

  const save = useMutation({
    mutationFn: () =>
      api<AISettings>("/settings/ai", { method: "PUT", body: { anthropic_api_key: key } }),
    onSuccess: () => {
      setKey("");
      setSaved(true);
      setTestResult(null);
      qc.invalidateQueries({ queryKey: ["ai-settings"] });
    },
  });

  const test = useMutation({
    mutationFn: () => api<{ ok: boolean; error?: string }>("/settings/ai/test", { method: "POST" }),
    onSuccess: (r) => setTestResult(r),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }

  const s = q.data;

  return (
    <>
      <PageHeader title="Settings" subtitle="Server-wide configuration. Admin only." />

      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles size={14} className="text-primary" /> Anthropic API key
          </CardTitle>
        </CardHeader>
        <CardBody className="space-y-4">
          <p className="text-sm text-fgmuted">
            Used by the import planner ("Generate plan") and the analytics chat.
            Stored encrypted on the server and applied immediately — no redeploy
            needed.
          </p>

          <div className="flex items-center gap-3 text-sm">
            <span className="text-fgmuted">Status:</span>
            {s?.configured ? (
              <Badge tone="success">
                <Check size={11} className="inline mr-1" />
                configured · {s.source}{s.hint ? ` · ${s.hint}` : ""}
              </Badge>
            ) : (
              <Badge tone="danger">
                <AlertCircle size={11} className="inline mr-1" />
                no key set
              </Badge>
            )}
            {s ? <span className="text-xs text-fgmuted">model: {s.model}</span> : null}
          </div>

          <form onSubmit={submit} className="space-y-3">
            <div>
              <Label htmlFor="key">New API key</Label>
              <Input
                id="key"
                type="password"
                autoComplete="off"
                value={key}
                onChange={(e) => { setKey(e.target.value); setSaved(false); }}
                placeholder="sk-ant-..."
              />
              <p className="text-[11px] text-fgmuted mt-1">
                Leave blank and save to clear the stored key (revert to the
                server's environment variable).
              </p>
            </div>

            <div className="flex items-center gap-2">
              <Button type="submit" disabled={save.isPending}>
                <KeyRound size={14} /> {save.isPending ? "Saving…" : "Save key"}
              </Button>
              <Button
                type="button"
                variant="secondary"
                disabled={!s?.configured || test.isPending}
                onClick={() => { setTestResult(null); test.mutate(); }}
              >
                {test.isPending ? "Testing…" : "Test key"}
              </Button>
              {saved ? (
                <span className="text-xs text-success inline-flex items-center gap-1">
                  <Check size={12} /> Saved
                </span>
              ) : null}
            </div>
          </form>

          {save.isError ? <p className="text-sm text-danger">Couldn't save the key.</p> : null}
          {testResult ? (
            testResult.ok ? (
              <p className="text-sm text-success inline-flex items-center gap-1">
                <Check size={13} /> Key works — Anthropic accepted the request.
              </p>
            ) : (
              <p className="text-sm text-danger inline-flex items-start gap-1">
                <AlertCircle size={13} className="mt-0.5 shrink-0" />
                <span>{testResult.error || "Key test failed."}</span>
              </p>
            )
          ) : null}
        </CardBody>
      </Card>
    </>
  );
}

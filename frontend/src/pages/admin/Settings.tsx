import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input, Label, Select } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { aiErrorText } from "@/components/AIStatus";
import {
  KeyRound, Check, AlertCircle, Sparkles, Server, Search, Loader2, Cpu,
} from "lucide-react";

type Provider = {
  name: string;
  display_name: string;
  configured: boolean;
  source: "db" | "env" | "none";
  hint: string | null;
  model: string;
  base_url: string | null;
  requires_key: boolean;
  supports_tools: boolean;
  self_hosted: boolean;
  is_active: boolean;
};

type AISettings = { active_provider: string; providers: Provider[] };

type LocalCandidate = {
  label: string;
  base_url: string;
  reachable: boolean;
  models: string[];
  error: string | null;
};

type TestResult = { ok: boolean; provider: string; model?: string; error?: string };

export function SettingsAdmin() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["ai-settings"], queryFn: () => api<AISettings>("/settings/ai") });
  const [testResult, setTestResult] = useState<TestResult | null>(null);

  const setActive = useMutation({
    mutationFn: (provider: string) =>
      api<AISettings>("/settings/ai/active", { method: "PUT", body: { provider } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ai-settings"] }),
  });

  const test = useMutation({
    mutationFn: (provider: string) =>
      api<TestResult>("/settings/ai/test", { method: "POST", body: { provider } }),
    onSuccess: (r) => setTestResult(r),
  });

  const data = q.data;
  const active = data?.providers.find((p) => p.is_active);

  return (
    <>
      <PageHeader title="Settings" subtitle="Server-wide configuration. Admin only." />

      <div className="space-y-6 max-w-3xl">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Sparkles size={14} className="text-primary" /> Active AI provider
            </CardTitle>
          </CardHeader>
          <CardBody className="space-y-3">
            <p className="text-sm text-fgmuted">
              Which model the server uses for summaries, finding extraction, chat
              and the import planner. Saved on the server, so it survives a restart.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Select
                value={data?.active_provider ?? ""}
                onChange={(e) => setActive.mutate(e.target.value)}
                disabled={!data || setActive.isPending}
                className="max-w-xs"
              >
                {data?.providers.map((p) => (
                  <option key={p.name} value={p.name} disabled={!p.configured}>
                    {p.display_name}
                    {p.configured ? "" : " — not configured"}
                  </option>
                ))}
              </Select>
              <Button
                type="button"
                variant="secondary"
                disabled={!active?.configured || test.isPending}
                onClick={() => { setTestResult(null); test.mutate(active!.name); }}
              >
                {test.isPending ? <Loader2 size={14} className="animate-spin" /> : null}
                {test.isPending ? "Testing…" : "Test active provider"}
              </Button>
            </div>
            {setActive.isError ? (
              <p className="text-sm text-danger">{aiErrorText(setActive.error, "Couldn't switch provider.")}</p>
            ) : null}
            {testResult ? (
              testResult.ok ? (
                <p className="text-sm text-success inline-flex items-center gap-1">
                  <Check size={13} /> Works — {testResult.provider} answered
                  {testResult.model ? ` using ${testResult.model}` : ""}.
                </p>
              ) : (
                <p className="text-sm text-danger inline-flex items-start gap-1">
                  <AlertCircle size={13} className="mt-0.5 shrink-0" />
                  <span>{testResult.error || "Test failed."}</span>
                </p>
              )
            ) : null}
            {active && !active.supports_tools ? (
              <p className="text-xs text-warning">
                {active.display_name} has no tool-calling support here, so the
                folder-import planner won't run on it. Everything else works.
              </p>
            ) : null}
          </CardBody>
        </Card>

        <LocalModelCard />

        {data?.providers.map((p) => <ProviderCard key={p.name} p={p} />)}
      </div>
    </>
  );
}

function ProviderCard({ p }: { p: Provider }) {
  const qc = useQueryClient();
  const [key, setKey] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: () =>
      api<AISettings>("/settings/ai", {
        method: "PUT",
        body: {
          provider: p.name,
          ...(key ? { api_key: key } : {}),
          ...(model ? { model } : {}),
          ...(baseUrl ? { base_url: baseUrl } : {}),
        },
      }),
    onSuccess: () => {
      setKey(""); setModel(""); setBaseUrl(""); setSaved(true);
      qc.invalidateQueries({ queryKey: ["ai-settings"] });
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          {p.self_hosted ? <Server size={14} className="text-primary" />
                         : <KeyRound size={14} className="text-primary" />}
          {p.display_name}
          {p.is_active ? <Badge tone="success">active</Badge> : null}
        </CardTitle>
        {p.configured ? (
          <Badge tone="success">
            <Check size={11} className="inline mr-1" />
            configured · {p.source}{p.hint ? ` · ${p.hint}` : ""}
          </Badge>
        ) : (
          <Badge tone="danger">
            <AlertCircle size={11} className="inline mr-1" /> not configured
          </Badge>
        )}
      </CardHeader>
      <CardBody className="space-y-3">
        <div className="text-xs text-fgmuted flex flex-wrap gap-x-4 gap-y-1">
          <span>model: <code>{p.model || "—"}</code></span>
          {p.base_url ? <span>endpoint: <code>{p.base_url}</code></span> : null}
          {!p.supports_tools ? <span className="text-warning">no tool-calling</span> : null}
        </div>

        <form onSubmit={submit} className="space-y-3">
          {p.self_hosted ? (
            <div>
              <Label htmlFor={`url-${p.name}`}>Endpoint base URL</Label>
              <Input
                id={`url-${p.name}`}
                value={baseUrl}
                onChange={(e) => { setBaseUrl(e.target.value); setSaved(false); }}
                placeholder="http://localhost:11434/v1"
              />
              <p className="text-[11px] text-fgmuted mt-1">
                A model on this machine: use <code>localhost</code> — the server
                rewrites it to reach your host from inside Docker. A model on
                another box: use its hostname. <code>/v1</code> is added if omitted.
              </p>
            </div>
          ) : null}

          <div>
            <Label htmlFor={`model-${p.name}`}>Model</Label>
            <Input
              id={`model-${p.name}`}
              value={model}
              onChange={(e) => { setModel(e.target.value); setSaved(false); }}
              placeholder={p.model || (p.self_hosted ? "llama3.1:8b" : "model id")}
            />
          </div>

          <div>
            <Label htmlFor={`key-${p.name}`}>
              API key {p.requires_key ? "" : <span className="text-fgmuted">(optional)</span>}
            </Label>
            <Input
              id={`key-${p.name}`}
              type="password"
              autoComplete="off"
              value={key}
              onChange={(e) => { setKey(e.target.value); setSaved(false); }}
              placeholder={p.requires_key ? "paste a key" : "usually not needed"}
            />
            <p className="text-[11px] text-fgmuted mt-1">
              Stored encrypted and applied immediately. Submit an empty field to
              clear the stored value and fall back to the server's environment.
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Button type="submit" disabled={save.isPending}>
              <KeyRound size={14} /> {save.isPending ? "Saving…" : "Save"}
            </Button>
            {saved ? (
              <span className="text-xs text-success inline-flex items-center gap-1">
                <Check size={12} /> Saved
              </span>
            ) : null}
          </div>
        </form>
        {save.isError ? (
          <p className="text-sm text-danger">{aiErrorText(save.error, "Couldn't save.")}</p>
        ) : null}
      </CardBody>
    </Card>
  );
}

/** One-click discovery of a model already running on this machine. */
function LocalModelCard() {
  const qc = useQueryClient();
  const [found, setFound] = useState<LocalCandidate[] | null>(null);

  const discover = useMutation({
    mutationFn: () => api<LocalCandidate[]>("/settings/ai/local/discover"),
    onSuccess: (r) => setFound(r),
  });

  const use = useMutation({
    mutationFn: (v: { base_url: string; model: string }) =>
      api<AISettings>("/settings/ai", {
        method: "PUT",
        body: { provider: "local", base_url: v.base_url, model: v.model },
      }),
    onSuccess: async () => {
      await api("/settings/ai/active", { method: "PUT", body: { provider: "local" } });
      qc.invalidateQueries({ queryKey: ["ai-settings"] });
      qc.invalidateQueries({ queryKey: ["ai-status"] });
    },
  });

  const reachable = (found ?? []).filter((c) => c.reachable);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Cpu size={14} className="text-primary" /> Find a local model
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-sm text-fgmuted">
          Scans the ports Ollama, LM Studio, vLLM and friends use by default. If
          a model is already running on this machine, pick it and the server
          will use it — nothing leaves your network.
        </p>
        <Button type="button" variant="secondary" onClick={() => discover.mutate()}
                disabled={discover.isPending}>
          {discover.isPending ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
          {discover.isPending ? "Scanning…" : "Scan for local models"}
        </Button>

        {found && reachable.length === 0 ? (
          <p className="text-xs text-fgmuted">
            Nothing responded. Start your model server (e.g. <code>ollama serve</code>)
            and scan again, or enter the endpoint manually under “Local model”.
          </p>
        ) : null}

        {reachable.map((c) => (
          <div key={c.base_url} className="border border-border rounded-md p-3 space-y-2">
            <div className="text-sm flex items-center gap-2">
              <Check size={13} className="text-success" />
              <strong>{c.label}</strong>
              <code className="text-xs text-fgmuted">{c.base_url}</code>
            </div>
            {c.models.length ? (
              <div className="flex flex-wrap gap-2">
                {c.models.map((m) => (
                  <Button key={m} type="button" size="sm" variant="ghost"
                          disabled={use.isPending}
                          onClick={() => use.mutate({ base_url: c.base_url, model: m })}>
                    use {m}
                  </Button>
                ))}
              </div>
            ) : (
              <p className="text-xs text-fgmuted">Reachable, but it reported no models.</p>
            )}
          </div>
        ))}
        {use.isError ? (
          <p className="text-sm text-danger">{aiErrorText(use.error, "Couldn't select that model.")}</p>
        ) : null}
      </CardBody>
    </Card>
  );
}

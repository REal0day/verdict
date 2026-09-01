import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/Button";
import { Input, Label } from "@/components/ui/Input";
import { Card, CardBody } from "@/components/ui/Card";
import { AlertCircle } from "lucide-react";

export function Login() {
  const { login } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      // login() resolves the new `me` into context. Pull it back from
      // /auth/me ourselves here so we can decide where to land before
      // the Layout mounts and avoid a flicker through /.
      const { api } = await import("@/lib/api");
      await login(email, password);
      const me = await api<{ onboarded_at: string | null }>("/auth/me");
      nav(me.onboarded_at ? "/" : "/welcome", { replace: true });
    } catch {
      setErr("Incorrect email or password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-bg px-4">
      <Card className="w-full max-w-sm">
        <CardBody className="p-6">
          <div className="mb-6">
            <div className="text-lg font-semibold">
              <span className="text-primary">Verdict</span>
              <span className="text-fgmuted text-xs font-normal ml-2">AI security findings</span>
            </div>
            <h1 className="text-xl font-semibold mt-3">Sign in</h1>
          </div>
          <form onSubmit={onSubmit} className="space-y-3">
            <div>
              <Label htmlFor="email">Email</Label>
              <Input
                id="email" type="email" autoFocus required
                value={email} onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="password">Password</Label>
              <Input
                id="password" type="password" required
                value={password} onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            {err ? (
              <div className="flex items-start gap-2 text-xs text-danger bg-danger/10 border border-danger/30 rounded-md p-2">
                <AlertCircle size={14} className="mt-0.5 shrink-0" />
                <span>{err}</span>
              </div>
            ) : null}
            <Button type="submit" className="w-full" disabled={busy}>
              {busy ? "Signing in..." : "Sign in"}
            </Button>
          </form>
          <p className="text-xs text-fgmuted mt-4 text-center">
            No account? <a href="/ui/register" className="text-primary hover:underline">Sign up</a>
          </p>
        </CardBody>
      </Card>
    </div>
  );
}

import { useEffect, useState, type FormEvent } from "react";
import { api, errorText } from "../api";
import type { Role, Session } from "../types";

export function Login({ onLogin }: { onLogin: (s: Session) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [oidc, setOidc] = useState(false);

  useEffect(() => {
    fetch("/healthz")
      .then((r) => r.json())
      .then((h: { oidc?: boolean }) => setOidc(Boolean(h.oidc)))
      .catch(() => setOidc(false));
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await api<{ token: string; email: string; role: Role; expires_at: string }>("/api/v1/auth/login", {
        method: "POST",
        body: { email, password },
      });
      onLogin({ token: r.token, email: r.email, role: r.role, expires_at: r.expires_at });
    } catch (err) {
      setError(errorText(err).startsWith("invalid credentials") ? "Wrong e-mail or password." : errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <form onSubmit={submit} aria-labelledby="login-title">
        <h1 id="login-title">Ansible Inventory</h1>
        <label>
          E-mail
          <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
        </label>
        <label>
          Password
          <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {oidc && (
          <a className="button secondary" href="/api/v1/auth/oidc/login?ui=1">
            Sign in with single sign-on
          </a>
        )}
      </form>
    </main>
  );
}

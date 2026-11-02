import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, errorText } from "../api";
import { Notice } from "../components";
import { useApp } from "../context";
import { when } from "../hooks";
import type { Role, TokenRow } from "../types";

const ROLES: Role[] = ["viewer", "editor", "admin"];
const RANK: Record<Role, number> = { viewer: 0, editor: 1, admin: 2 };

/** API tokens (`/tokens`): mint one for the inventory plugin or the modules (shown once),
 * list and revoke. Not an inventory write, so no `If-Match`. Admins can list everyone's. */
export function TokensView({ onSignedOut }: { onSignedOut: () => void }) {
  const { session } = useApp();
  const isAdmin = session.role === "admin";
  const [all, setAll] = useState(false);
  const [rows, setRows] = useState<TokenRow[]>([]);
  const [me, setMe] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [minted, setMinted] = useState<{ name: string; token: string } | null>(null);
  const [name, setName] = useState("");
  const [days, setDays] = useState("90");
  const [role, setRole] = useState("");
  const [confirm, setConfirm] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [list, who] = await Promise.all([
        api<TokenRow[]>("/api/v1/tokens", { query: { all: all || undefined } }),
        api<{ token: TokenRow | null }>("/api/v1/auth/me"),
      ]);
      setRows(list);
      setMe(who.token?.id ?? null);
      setError("");
    } catch (e) {
      setError(errorText(e));
    }
  }, [all]);

  useEffect(() => {
    load();
  }, [load]);

  const mint = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const body: Record<string, unknown> = { name: name.trim() || "token" };
      if (days.trim()) body.expires_days = Number(days);
      if (role) body.role = role;
      const r = await api<TokenRow & { token: string }>("/api/v1/tokens", { method: "POST", body });
      setMinted({ name: r.name, token: r.token });
      setName("");
      load();
    } catch (err) {
      setError(errorText(err));
    }
  };

  const revoke = async (id: number) => {
    setConfirm(null);
    try {
      await api(`/api/v1/tokens/${id}`, { method: "DELETE" });
      if (id === me) onSignedOut();
      else load();
    } catch (err) {
      setError(errorText(err));
    }
  };

  const live = (t: TokenRow) => !t.revoked_at && (!t.expires_at || new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(t.expires_at) ? t.expires_at : `${t.expires_at}Z`) > new Date());

  return (
    <div className="page">
      <h1>API tokens</h1>
      <p className="muted">
        Bearer tokens for the inventory plugin and the modules (<code>AIS_TOKEN</code>). A token can be capped below your role. Its secret is shown once.
      </p>
      {error && (
        <Notice kind="error" onClose={() => setError("")}>
          {error}
        </Notice>
      )}
      {minted && (
        <Notice kind="ok" onClose={() => setMinted(null)}>
          Token <strong>{minted.name}</strong>, copy it now (it is not shown again): <code className="secret" aria-label="New token">{minted.token}</code>{" "}
          <button type="button" className="small secondary" onClick={() => navigator.clipboard?.writeText(minted.token)}>
            Copy
          </button>
        </Notice>
      )}
      <form className="filters" onSubmit={mint} aria-label="New token">
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="ci-inventory" aria-label="Token name" />
        </label>
        <label>
          Expires in days
          <input className="narrow" value={days} onChange={(e) => setDays(e.target.value.replace(/\D/g, ""))} placeholder="never" aria-label="Expires in days" />
        </label>
        <label>
          Role
          <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Token role">
            <option value="">mine ({session.role})</option>
            {ROLES.filter((r) => RANK[r] < RANK[session.role]).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <div className="inline">
          <button type="submit">Create token</button>
        </div>
      </form>
      {isAdmin && (
        <label className="inline check">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> every user's tokens
        </label>
      )}
      <table className="list tokens">
        <thead>
          <tr>
            <th>Name</th>
            <th>Prefix</th>
            <th>Kind</th>
            <th>Role cap</th>
            {all && <th>User</th>}
            <th>Created</th>
            <th>Last used</th>
            <th>Expires</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => (
            <tr key={t.id} data-name={t.name} className={live(t) ? "" : "synthetic"}>
              <td>
                {t.name}
                {t.id === me && <span className="muted"> (this session)</span>}
              </td>
              <td>
                <code>{t.prefix}…</code>
              </td>
              <td>{t.kind}</td>
              <td>{t.role ?? <span className="muted">none</span>}</td>
              {all && <td>{t.user_id}</td>}
              <td>{when(t.created_at)}</td>
              <td>{when(t.last_used)}</td>
              <td>{t.revoked_at ? `revoked ${when(t.revoked_at)}` : t.expires_at ? when(t.expires_at) : "never"}</td>
              <td className="actions">
                {!t.revoked_at &&
                  (confirm === t.id ? (
                    <button type="button" className="small danger" onClick={() => revoke(t.id)} aria-label={`Confirm revoking ${t.name}`}>
                      Confirm revoke
                    </button>
                  ) : (
                    <button type="button" className="small secondary" onClick={() => setConfirm(t.id)} aria-label={`Revoke ${t.name}`}>
                      Revoke
                    </button>
                  ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, errorText } from "../api";
import { Notice, Value } from "../components";
import { when } from "../hooks";
import { href, navigate } from "../router";
import type { AuditRow } from "../types";

const PAGE = 50;
// `node` (a path) + `key` follow a node through moves and renames (the server walks node.move rows).
const FILTERS = ["actor", "action", "target_type", "target_id", "node", "key", "revision", "change", "since", "until"] as const;
type Filters = Record<(typeof FILTERS)[number], string>;

/** `GET /audit` (editor+). Filters live in the URL so "who changed this key" links work. */
export function AuditView({ params }: { params: URLSearchParams }) {
  const fromParams = (): Filters => Object.fromEntries(FILTERS.map((f) => [f, params.get(f) ?? ""])) as Filters;
  const [filters, setFilters] = useState<Filters>(fromParams);
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const key = params.toString();

  const load = async (before?: number) => {
    const active = fromParams();
    try {
      const page = await api<AuditRow[]>("/api/v1/audit", { query: { ...active, limit: PAGE, before } });
      setRows((r) => (before ? [...r, ...page] : page));
      setMore(page.length === PAGE);
      setError("");
    } catch (e) {
      setError(e instanceof ApiError && e.status === 403 ? "The audit log needs the editor role." : errorText(e));
    }
  };

  useEffect(() => {
    setFilters(fromParams());
    load();
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  const apply = (e: FormEvent) => {
    e.preventDefault();
    navigate("audit", filters);
  };

  return (
    <div className="page">
      <h1>Audit log</h1>
      <p className="muted">Every effective write and every sign-in or token event, newest first.</p>
      <form className="filters" onSubmit={apply}>
        {FILTERS.map((f) => (
          <label key={f}>
            {f.replace("_", " ")}
            <input
              value={filters[f]}
              onChange={(e) => setFilters({ ...filters, [f]: e.target.value })}
              placeholder={
                f === "action" ? "node. or variable.set" : f === "node" ? "all.commercial.prod" : f === "change" ? "SRE-1234" : f === "since" || f === "until" ? "2026-11-01T00:00" : ""
              }
              aria-label={f}
            />
          </label>
        ))}
        <div className="inline">
          <button type="submit">Filter</button>
          <button type="button" className="secondary" onClick={() => navigate("audit")}>
            Clear
          </button>
        </div>
      </form>
      {error && <Notice kind="error">{error}</Notice>}
      <table className="list audit">
        <thead>
          <tr>
            <th>When</th>
            <th>Who</th>
            <th>Action</th>
            <th>Target</th>
            <th>Source</th>
            <th>Revision</th>
            <th>Change</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{when(r.ts)}</td>
              <td>{r.actor}</td>
              <td>
                <code>{r.action}</code>
              </td>
              <td>
                {r.target_type}
                {r.target_id ? ` ${r.target_id}` : ""}
                {(r.before != null || r.after != null) && (
                  <details>
                    <summary>details</summary>
                    <div className="two">
                      <div>
                        <div className="muted">before</div>
                        <Value value={r.before} block />
                      </div>
                      <div>
                        <div className="muted">after</div>
                        <Value value={r.after} block />
                      </div>
                    </div>
                    {r.request_id && <div className="muted">request {r.request_id}</div>}
                  </details>
                )}
              </td>
              <td>{r.source}</td>
              <td>{r.revision}</td>
              <td>{r.change && <a href={href("audit", { change: r.change })}>{r.change}</a>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {!error && rows.length === 0 && <p className="muted">No matching rows.</p>}
      {more && (
        <button type="button" className="secondary" onClick={() => load(rows[rows.length - 1]?.id)}>
          Older
        </button>
      )}
    </div>
  );
}

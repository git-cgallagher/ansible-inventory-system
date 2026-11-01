import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, errorText } from "../api";
import { Notice, Value } from "../components";
import { useAllNodes, when } from "../hooks";
import { href } from "../router";
import type { DiffResult, RevisionRow, SnapshotRow } from "../types";

/** Resolved variables of one host between two revisions (`GET /diff`). Every revision is
 * offered: the server computes a missing snapshot on demand; ● marks stored ones. */
export function DiffView({ params }: { params: URLSearchParams }) {
  const { nodes, error: nodesError } = useAllNodes();
  const [host, setHost] = useState(params.get("host") ?? "");
  const [revs, setRevs] = useState<RevisionRow[]>([]);
  const [snaps, setSnaps] = useState<Set<number>>(new Set());
  const [from, setFrom] = useState(params.get("from") ?? "");
  const [to, setTo] = useState(params.get("to") ?? "");
  const [result, setResult] = useState<DiffResult | null>(null);
  const [error, setError] = useState("");

  const hosts = useMemo(() => nodes.filter((n) => n.kind === "host").map((n) => n.name).sort(), [nodes]);

  useEffect(() => {
    api<RevisionRow[]>("/api/v1/revisions", { query: { limit: 500 } })
      .then((rows) => {
        setRevs(rows);
        if (!to && rows[0]) setTo(String(rows[0].id));
        if (!from && rows[1]) setFrom(String(rows[1].id));
      })
      .catch((e) => setError(errorText(e)));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setSnaps(new Set());
    if (!host) return;
    api<SnapshotRow[]>(`/api/v1/hosts/${encodeURIComponent(host)}/snapshots`)
      .then((rows) => setSnaps(new Set(rows.map((r) => r.revision))))
      .catch(() => setSnaps(new Set()));
  }, [host, result]);

  const compare = async (e?: FormEvent) => {
    e?.preventDefault();
    setError("");
    setResult(null);
    if (!host || !from || !to) {
      setError("Pick a host and two revisions.");
      return;
    }
    try {
      setResult(await api<DiffResult>("/api/v1/diff", { query: { host, from, to } }));
      history.replaceState(null, "", href("diff", { host, from, to }));
    } catch (err) {
      setError(errorText(err));
    }
  };

  useEffect(() => {
    if (params.get("host") && params.get("from") && params.get("to")) compare();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const revOption = (r: RevisionRow) => (
    <option key={r.id} value={r.id}>
      {snaps.has(r.id) ? "● " : ""}
      {r.id} · {when(r.ts)} · {r.actor}
      {r.message ? ` · ${r.message}` : ""}
    </option>
  );

  return (
    <div className="page">
      <h1>Diff</h1>
      <p className="muted">How one host's resolved variables changed between two revisions. ● = snapshot already stored.</p>
      {nodesError && <Notice kind="error">{nodesError}</Notice>}
      <form className="inline wrap" onSubmit={compare}>
        <label>
          Host
          <select value={host} onChange={(e) => setHost(e.target.value)} aria-label="Host">
            <option value="">— pick a host —</option>
            {hosts.map((h) => (
              <option key={h}>{h}</option>
            ))}
          </select>
        </label>
        <label>
          From
          <select value={from} onChange={(e) => setFrom(e.target.value)} aria-label="From revision">
            {revs.map(revOption)}
          </select>
        </label>
        <label>
          To
          <select value={to} onChange={(e) => setTo(e.target.value)} aria-label="To revision">
            {revs.map(revOption)}
          </select>
        </label>
        <button type="submit">Compare</button>
      </form>
      {error && <Notice kind="error">{error}</Notice>}
      {result &&
        (result.identical ? (
          <Notice kind="ok">
            {result.host} resolves identically at revisions {result.from} and {result.to}.
          </Notice>
        ) : (
          <section aria-label="Diff result">
            <table className="vars diff">
              <thead>
                <tr>
                  <th>Key</th>
                  <th>Revision {result.from}</th>
                  <th>Revision {result.to}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(result.removed).map(([k, v]) => (
                  <tr key={`r-${k}`} className="removed">
                    <th scope="row">
                      <code>{k}</code> <span className="tag">removed</span>
                    </th>
                    <td>
                      <Value value={v} />
                    </td>
                    <td />
                  </tr>
                ))}
                {Object.entries(result.changed).map(([k, v]) => (
                  <tr key={`c-${k}`} className="changed">
                    <th scope="row">
                      <code>{k}</code> <span className="tag">changed</span>
                    </th>
                    <td>
                      <Value value={v.from} />
                    </td>
                    <td>
                      <Value value={v.to} />
                    </td>
                  </tr>
                ))}
                {Object.entries(result.added).map(([k, v]) => (
                  <tr key={`a-${k}`} className="added">
                    <th scope="row">
                      <code>{k}</code> <span className="tag">added</span>
                    </th>
                    <td />
                    <td>
                      <Value value={v} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        ))}
    </div>
  );
}

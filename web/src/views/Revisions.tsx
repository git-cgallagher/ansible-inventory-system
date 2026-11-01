import { useEffect, useState } from "react";
import { api, errorText } from "../api";
import { Notice } from "../components";
import { useApp } from "../context";
import { when } from "../hooks";
import { href } from "../router";
import type { RevisionRow } from "../types";
import { canEdit } from "../types";

const PAGE = 50;

export function RevisionsView() {
  const { session } = useApp();
  const [rows, setRows] = useState<RevisionRow[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");

  const load = async (before?: number) => {
    try {
      const page = await api<RevisionRow[]>("/api/v1/revisions", { query: { limit: PAGE, before } });
      setRows((r) => (before ? [...r, ...page] : page));
      setMore(page.length === PAGE);
    } catch (e) {
      setError(errorText(e));
    }
  };

  useEffect(() => {
    load();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="page">
      <h1>Revisions</h1>
      <p className="muted">Every write opens an immutable revision; the plugin can pin any of them.</p>
      {error && <Notice kind="error">{error}</Notice>}
      <table className="list">
        <thead>
          <tr>
            <th>Revision</th>
            <th>When</th>
            <th>Who</th>
            <th>Source</th>
            <th>Message</th>
            {canEdit(session.role) && <th aria-label="Audit" />}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td>
              <td>{when(r.ts)}</td>
              <td>{r.actor}</td>
              <td>{r.source}</td>
              <td>{r.message}</td>
              {canEdit(session.role) && (
                <td>
                  <a href={href("audit", { revision: r.id })}>audit</a>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {more && (
        <button type="button" className="secondary" onClick={() => load(rows[rows.length - 1]?.id)}>
          Older
        </button>
      )}
    </div>
  );
}

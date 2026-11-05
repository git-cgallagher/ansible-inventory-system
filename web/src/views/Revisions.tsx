import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, errorText } from "../api";
import { Notice } from "../components";
import { useApp } from "../context";
import { when } from "../hooks";
import { href, navigate } from "../router";
import type { RevertConflict, RevertResult, RevisionRow } from "../types";
import { canEdit } from "../types";

const PAGE = 50;
type Msg = { kind: "ok" | "warn" | "error"; text: string } | null;
/** What a revert undoes: one revision, or every revision of a change id. */
type Target = { revision: number } | { change: string };

const targetUrl = (t: Target) =>
  "revision" in t ? `/api/v1/revisions/${t.revision}/revert` : `/api/v1/changes/${encodeURIComponent(t.change)}/revert`;
const targetLabel = (t: Target) => ("revision" in t ? `revision ${t.revision}` : `change ${t.change}`);

/** Revisions, newest first, optionally only one change id's (`#/revisions?change=SRE-1234`).
 *
 * Revert (editors): first a dry run (a read: no `If-Match`), which lists the inverse operations
 * and the hosts whose resolved vars would change, or the conflicts that refuse it; the
 * confirmation then sends `If-Match` = the revision that preview was computed at, so the server
 * refuses (409) if anything was written since the user looked. */
export function RevisionsView({ params }: { params: URLSearchParams }) {
  const { session } = useApp();
  const editable = canEdit(session.role);
  const change = params.get("change") ?? "";
  const [filter, setFilter] = useState(change);
  const [rows, setRows] = useState<RevisionRow[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState<Msg>(null);
  const [target, setTarget] = useState<Target | null>(null);

  const load = async (before?: number) => {
    try {
      const page = await api<RevisionRow[]>("/api/v1/revisions", { query: { limit: PAGE, before, change: change || undefined } });
      setRows((r) => (before ? [...r, ...page] : page));
      setMore(page.length === PAGE);
      setError("");
    } catch (e) {
      setError(errorText(e));
    }
  };

  useEffect(() => {
    setFilter(change);
    setTarget(null);
    load();
  }, [change]); // eslint-disable-line react-hooks/exhaustive-deps

  const apply = (e: FormEvent) => {
    e.preventDefault();
    navigate("revisions", { change: filter.trim() });
  };

  const reverted = (r: RevertResult, t: Target) => {
    setTarget(null);
    setMsg({ kind: "ok", text: `Reverted ${targetLabel(t)} as revision ${r.revision}: ${r.operations.length} operation(s).` });
    load();
  };

  return (
    <div className="page">
      <h1>Revisions</h1>
      <p className="muted">
        Every write opens an immutable revision; the plugin can pin any of them. Writes that carry a change id (the{" "}
        <code>X-AIS-Change</code> header, the modules' <code>change</code> option) can be listed and reverted together.
      </p>
      <form className="inline" onSubmit={apply} aria-label="Filter revisions">
        <label>
          change
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="SRE-1234" aria-label="change" />
        </label>
        <button type="submit">Filter</button>
        {change && (
          <button type="button" className="secondary" onClick={() => navigate("revisions")}>
            All revisions
          </button>
        )}
        {change && editable && rows.length > 0 && !target && (
          <button type="button" className="danger" onClick={() => setTarget({ change })}>
            Revert change {change}
          </button>
        )}
      </form>
      {error && <Notice kind="error">{error}</Notice>}
      {msg && (
        <Notice kind={msg.kind} onClose={() => setMsg(null)}>
          {msg.text}
        </Notice>
      )}
      {target && <RevertPanel key={targetLabel(target)} target={target} onDone={reverted} onCancel={() => setTarget(null)} />}
      <table className="list">
        <thead>
          <tr>
            <th>Revision</th>
            <th>When</th>
            <th>Who</th>
            <th>Source</th>
            <th>Change</th>
            <th>Message</th>
            {editable && <th aria-label="Actions" />}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} data-revision={r.id}>
              <td>{r.id}</td>
              <td>{when(r.ts)}</td>
              <td>{r.actor}</td>
              <td>{r.source}</td>
              <td>{r.change && <a href={href("revisions", { change: r.change })}>{r.change}</a>}</td>
              <td>{r.message}</td>
              {editable && (
                <td className="inline">
                  <a href={href("audit", { revision: r.id })}>audit</a>
                  <button type="button" className="small secondary" onClick={() => setTarget({ revision: r.id })} aria-label={`Revert revision ${r.id}`}>
                    Revert
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {!error && rows.length === 0 && <p className="muted">{change ? `No revision carries change id ${change}.` : "No revisions yet."}</p>}
      {more && (
        <button type="button" className="secondary" onClick={() => load(rows[rows.length - 1]?.id)}>
          Older
        </button>
      )}
    </div>
  );
}

/** The dry run of a revert, then its confirmation. */
function RevertPanel({ target, onDone, onCancel }: { target: Target; onDone: (r: RevertResult, t: Target) => void; onCancel: () => void }) {
  const [preview, setPreview] = useState<RevertResult | null>(null);
  const [conflicts, setConflicts] = useState<RevertConflict[] | null>(null);
  const [force, setForce] = useState(false);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setPreview(null);
    setProblem("");
    api<RevertResult>(targetUrl(target), { method: "POST", body: { dry_run: true, force } })
      .then((r) => {
        setPreview(r);
        if (!force) setConflicts(null);
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 409 && (e.detail as { conflicts?: RevertConflict[] })?.conflicts) {
          setConflicts((e.detail as { conflicts: RevertConflict[] }).conflicts);
        }
        setProblem(errorText(e));
      });
  }, [force]); // eslint-disable-line react-hooks/exhaustive-deps

  const confirm = async () => {
    if (!preview) return;
    setBusy(true);
    try {
      const r = await api<RevertResult>(targetUrl(target), { method: "POST", body: { force }, ifMatch: preview.revision });
      onDone(r, target);
    } catch (e) {
      setProblem(e instanceof ApiError && e.stale ? `Someone wrote since this preview (revision ${e.stale.current}); preview again.` : errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const label = targetLabel(target);
  return (
    <section className="editor" aria-label={`Revert ${label}`}>
      <strong>Revert {label}</strong>
      {!preview && !problem && <p className="muted">Asking the server what the revert would do…</p>}
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      {conflicts && conflicts.length > 0 && !conflicts.every((c) => c.already_reverted) && (
        <label className="inline">
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} aria-label="Force" /> Revert anyway, over those later
          changes
        </label>
      )}
      {preview && (
        <>
          <p>
            One new revision with <strong data-testid="revert-ops">{preview.operations.length} operation(s)</strong>, newest first:
          </p>
          <ul className="ops">
            {preview.operations.map((o, i) => (
              <li key={i}>
                <span className="muted">revision {o.reverts}:</span> {o.summary}
              </li>
            ))}
          </ul>
          <p>
            <strong data-testid="revert-hosts">
              {preview.hosts_changed ?? 0} {preview.hosts_changed === 1 ? "host" : "hosts"}
            </strong>{" "}
            would get different variables{preview.hosts_changed ? ":" : "."}
          </p>
          {(preview.hosts ?? []).length > 0 && (
            <ul className="ops" aria-label="Hosts that would change">
              {(preview.hosts ?? []).map((h) => {
                const keys = [...Object.keys(h.added ?? {}), ...Object.keys(h.removed ?? {}), ...Object.keys(h.changed ?? {})].sort();
                return (
                  <li key={h.host}>
                    <code>{h.host}</code> {h.status}
                    {keys.length > 0 && <span className="muted"> ({keys.join(", ")})</span>}
                  </li>
                );
              })}
            </ul>
          )}
          {preview.notes.map((n, i) => (
            <p key={i} className="muted hint">
              {n}
            </p>
          ))}
        </>
      )}
      <div className="editor-foot">
        <button type="button" className="danger" disabled={!preview || busy} onClick={confirm}>
          {busy ? "Working…" : `Confirm revert of ${label}`}
        </button>
        <button type="button" className="secondary" onClick={onCancel}>
          Cancel
        </button>
        {preview && <span className="muted">based on revision {preview.revision}</span>}
      </div>
    </section>
  );
}

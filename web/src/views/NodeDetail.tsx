import { useCallback, useEffect, useState } from "react";
import { api, ApiError, errorText } from "../api";
import { Notice, Provenance, Spinner, StrategyBadge, Value } from "../components";
import { useApp } from "../context";
import { href } from "../router";
import type { InvNode, RenderResult, WriteResult } from "../types";
import { SYNTHETIC_KEYS, canEdit } from "../types";
import { same } from "../value";
import { NodeActions } from "./NodeActions";
import { VarEditor } from "./VarEditor";

interface Editing {
  key: string | null; // null = a key not set here yet
  value: unknown;
  preset?: string; // key name to prefill for a new key ("Override here")
}

type Msg = { kind: "ok" | "warn" | "error"; text: string } | null;

/** One node: its own variables and everything it inherits, all read from one
 * `POST /render {path, template: false}` so values and the revision shown are consistent
 * (renders are read-only; snapshots are written by the server's background pass). */
export function NodeDetail({ node, onStructure }: { node: InvNode; onStructure: (path: string, message: string) => void }) {
  const { session, defs } = useApp();
  const editable = canEdit(session.role);
  const [data, setData] = useState<RenderResult | null>(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState<Msg>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api<RenderResult>("/api/v1/render", { method: "POST", body: { path: node.path, template: false } }));
      setError("");
    } catch (e) {
      setError(errorText(e));
    }
  }, [node.path]);

  useEffect(() => {
    load();
  }, [load]);

  /** Shared by save and delete. Returns true when the editor should stay open. */
  const onWriteError = (e: unknown): boolean => {
    if (e instanceof ApiError && e.stale) {
      setMsg({
        kind: "warn",
        text: `Someone else changed the inventory: this page showed revision ${e.stale.expected}, the server is at ${e.stale.current}. The latest values are loaded; check them and apply your change again.`,
      });
      load();
      return true;
    }
    setMsg({ kind: "error", text: `Refused: ${errorText(e)}` });
    return true;
  };

  const onSaved = (key: string, r: WriteResult) => {
    setEditing(null);
    setMsg(
      r.changed
        ? { kind: "ok", text: `Saved ${key}: revision ${r.revision}.` }
        : { kind: "ok", text: `No change: ${key} already had that value (revision ${r.revision}).` },
    );
    load();
  };

  const remove = async (key: string) => {
    if (!data) return;
    setConfirmDelete(null);
    try {
      const r = await api<WriteResult>(`/api/v1/nodes/${node.id}/vars/${encodeURIComponent(key)}`, { method: "DELETE", ifMatch: data.revision });
      setMsg({ kind: "ok", text: `Removed ${key} from ${node.path}: revision ${r.revision}.` });
      load();
    } catch (e) {
      onWriteError(e);
    }
  };

  if (error) return <Notice kind="error">{error}</Notice>;
  if (!data) return <Spinner />;

  const keys = Object.keys(data.resolved)
    .filter((k) => !SYNTHETIC_KEYS.has(k))
    .sort();
  const ownEntry = (k: string) => (data.provenance[k] ?? []).find((c) => c.path === node.path);
  const own = keys.filter((k) => ownEntry(k));
  const inherited = keys.filter((k) => !ownEntry(k));

  return (
    <div className="detail">
      <header className="detail-head">
        <div>
          <h1>{node.name}</h1>
          <p className="muted">
            <code>{node.path}</code> · {node.kind}
            {node.inventory_name && (
              <>
                {" "}
                · group <code>{node.inventory_name}</code>
              </>
            )}{" "}
            · id {node.id}
          </p>
        </div>
        <div className="head-actions">
          <span className="rev" title="Writes from this page are based on this revision (If-Match)">
            revision {data.revision}
          </span>
          <a className="button secondary" href={href("render", node.kind === "host" ? { host: node.name } : { path: node.path })}>
            Render
          </a>
          {editable && (
            <a className="button secondary" href={href("audit", { node: node.path })} title="Changes to this node and its variables, across moves">
              History
            </a>
          )}
        </div>
      </header>
      {editable && <NodeActions node={node} revision={data.revision} onDone={onStructure} onError={onWriteError} />}

      {msg && (
        <Notice kind={msg.kind} onClose={() => setMsg(null)}>
          {msg.text}
        </Notice>
      )}

      {Object.keys(node.meta ?? {}).length > 0 && (
        <details className="meta">
          <summary>meta</summary>
          <Value value={node.meta} block />
        </details>
      )}

      <section aria-labelledby="own-title">
        <div className="section-head">
          <h2 id="own-title">Variables set here</h2>
          {editable && !editing && (
            <button type="button" onClick={() => setEditing({ key: null, value: "" })}>
              Add variable
            </button>
          )}
        </div>
        {editing && editing.key === null && (
          <VarEditor node={node} keyName={null} presetKey={editing.preset} initial={editing.value} revision={data.revision} onSaved={onSaved} onError={onWriteError} onCancel={() => setEditing(null)} />
        )}
        {own.length === 0 ? (
          <p className="muted">Nothing set at this level.</p>
        ) : (
          <table className="vars">
            <thead>
              <tr>
                <th>Key</th>
                <th>Value here</th>
                <th>Effective</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {own.map((k) => {
                const mine = ownEntry(k)!.value;
                const effective = data.resolved[k];
                return editing?.key === k ? (
                  <tr key={k}>
                    <td colSpan={4}>
                      <VarEditor node={node} keyName={k} initial={mine} revision={data.revision} onSaved={onSaved} onError={onWriteError} onCancel={() => setEditing(null)} />
                    </td>
                  </tr>
                ) : (
                  <tr key={k} data-key={k}>
                    <th scope="row">
                      <code>{k}</code> <StrategyBadge name={k} />
                    </th>
                    <td>
                      <Value value={mine} />
                    </td>
                    <td>{same(mine, effective) ? <span className="muted">same</span> : <Value value={effective} />}</td>
                    <td className="actions">
                      <a className="small" href={href("search", { key: k })} aria-label={`Where is ${k} set`} title="Every node that sets this key and every host's value">
                        where set
                      </a>
                      {editable && (
                        <>
                          <button type="button" className="small" onClick={() => setEditing({ key: k, value: mine })} aria-label={`Edit ${k}`}>
                            Edit
                          </button>
                          {confirmDelete === k ? (
                            <button type="button" className="small danger" onClick={() => remove(k)} aria-label={`Confirm removing ${k}`}>
                              Confirm remove
                            </button>
                          ) : (
                            <button type="button" className="small secondary" onClick={() => setConfirmDelete(k)} aria-label={`Remove ${k}`}>
                              Remove
                            </button>
                          )}
                          <a className="small" href={href("audit", { node: node.path, key: k })} title="Who changed this key here (audit log, across moves and renames)">
                            history
                          </a>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section aria-labelledby="inh-title">
        <h2 id="inh-title">Inherited</h2>
        {inherited.length === 0 ? (
          <p className="muted">Nothing inherited.</p>
        ) : (
          <table className="vars inherited">
            <thead>
              <tr>
                <th>Key</th>
                <th>Value</th>
                <th>From</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {inherited.map((k) => (
                <tr key={k} data-key={k}>
                  <th scope="row">
                    <code>{k}</code> <StrategyBadge name={k} />
                  </th>
                  <td>
                    <Value value={data.resolved[k]} />
                  </td>
                  <td>
                    <Provenance entries={data.provenance[k] ?? []} />
                  </td>
                  <td className="actions">
                    <a className="small" href={href("search", { key: k })} aria-label={`Where is ${k} set`} title="Every node that sets this key and every host's value">
                      where set
                    </a>
                    {editable && !editing && (() => {
                      // A fold variable adds to what is inherited, so start from an empty contribution.
                      const fold = (defs.get(k)?.merge_strategy ?? "replace") !== "replace";
                      const v = data.resolved[k];
                      const start = fold ? (Array.isArray(v) ? [] : v && typeof v === "object" ? {} : v) : v;
                      const label = fold ? "Add here" : "Override here";
                      return (
                        <button type="button" className="small secondary" onClick={() => setEditing({ key: null, value: start, preset: k })} aria-label={`${label}: ${k}`}>
                          {label}
                        </button>
                      );
                    })()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

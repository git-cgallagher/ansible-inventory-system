import { Fragment, useCallback, useEffect, useState } from "react";
import { api, ApiError, errorText } from "../api";
import { ImpactSummary, Notice, Provenance, Spinner, StrategyBadge, Value } from "../components";
import { useApp } from "../context";
import { href } from "../router";
import type { InvNode, ProfileRow, RenderResult, WriteResult } from "../types";
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
  const [removal, setRemoval] = useState<WriteResult | null>(null); // the dry run of removing confirmDelete

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
    const hint = r.warnings?.find((w) => w.key === key);
    setMsg(
      !r.changed
        ? { kind: "ok", text: `No change: ${key} already had that value (revision ${r.revision}).` }
        : hint
          ? {
              kind: "warn",
              text: `Saved ${key}: revision ${r.revision}. ${hint.message} If ${key} is right, declare it on the Variables page to stop this warning.`,
            }
          : { kind: "ok", text: `Saved ${key}: revision ${r.revision}.` },
    );
    load();
  };

  const varUrl = (key: string) => `/api/v1/nodes/${node.id}/vars/${encodeURIComponent(key)}`;

  /** Remove, step 1: a dry run based on the page's revision says how many hosts it changes. */
  const askRemove = async (key: string) => {
    if (!data) return;
    setConfirmDelete(key);
    setRemoval(null);
    try {
      setRemoval(await api<WriteResult>(varUrl(key), { method: "DELETE", ifMatch: data.revision, query: { dry_run: true } }));
    } catch (e) {
      setConfirmDelete(null);
      onWriteError(e);
    }
  };

  /** Remove, step 2: the write, based on the revision the preview showed. */
  const remove = async (key: string) => {
    if (!data || !removal) return;
    setConfirmDelete(null);
    setRemoval(null);
    try {
      const r = await api<WriteResult>(varUrl(key), { method: "DELETE", ifMatch: removal.revision });
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
  // Own = set on this node itself; a profile's value applied here is inherited-like (its provenance says so).
  const ownEntry = (k: string) => (data.provenance[k] ?? []).find((c) => c.path === node.path && !c.profile);
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
                  <Fragment key={k}>
                  <tr data-key={k}>
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
                            <>
                              <button type="button" className="small danger" disabled={!removal} onClick={() => remove(k)} aria-label={`Confirm removing ${k}`}>
                                Confirm remove
                              </button>
                              <button
                                type="button"
                                className="small secondary"
                                onClick={() => {
                                  setConfirmDelete(null);
                                  setRemoval(null);
                                }}
                                aria-label={`Keep ${k}`}
                              >
                                Keep
                              </button>
                            </>
                          ) : (
                            <button type="button" className="small secondary" onClick={() => askRemove(k)} aria-label={`Remove ${k}`}>
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
                  {confirmDelete === k && (
                    <tr className="preview-row" aria-label={`Removing ${k}`}>
                      <td colSpan={4}>
                        {removal?.impact ? (
                          <>
                            <ImpactSummary impact={removal.impact} />
                            <span className="muted">based on revision {removal.revision}</span>
                          </>
                        ) : (
                          <span className="muted">Asking the server what removing {k} would change…</span>
                        )}
                      </td>
                    </tr>
                  )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      {node.kind !== "host" && (
        <NodeProfiles node={node} attached={data.profiles?.[node.path] ?? []} revision={data.revision} editable={editable} onDone={(text) => { setMsg({ kind: "ok", text }); load(); }} onError={onWriteError} />
      )}

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

/** The profiles attached to a node (issue 22), in application order, with attach and detach.
 * Both preview first (a dry run with `If-Match` = the page's revision) and the confirmation
 * carries the preview's revision, like value edits. */
function NodeProfiles(props: {
  node: InvNode;
  attached: string[];
  revision: number;
  editable: boolean;
  onDone: (text: string) => void;
  onError: (e: unknown) => boolean;
}) {
  const { node, attached, revision, editable } = props;
  const [names, setNames] = useState<string[] | null>(null);
  const [choice, setChoice] = useState("");
  const [pending, setPending] = useState<{ kind: "attach" | "detach"; name: string; preview: WriteResult | null } | null>(null);

  useEffect(() => {
    if (!editable) return;
    api<ProfileRow[]>("/api/v1/profiles")
      .then((rows) => setNames(rows.map((r) => r.name)))
      .catch(() => setNames([]));
  }, [editable]);

  const url = (name: string) => `/api/v1/nodes/${node.id}/profiles/${encodeURIComponent(name)}`;
  const ask = async (kind: "attach" | "detach", name: string) => {
    setPending({ kind, name, preview: null });
    try {
      const preview = await api<WriteResult>(url(name), { method: kind === "attach" ? "PUT" : "DELETE", body: kind === "attach" ? {} : undefined, ifMatch: revision, query: { dry_run: true } });
      setPending({ kind, name, preview });
    } catch (e) {
      setPending(null);
      props.onError(e);
    }
  };
  const confirm = async () => {
    if (!pending?.preview) return;
    const { kind, name, preview } = pending;
    setPending(null);
    try {
      const r = await api<WriteResult>(url(name), { method: kind === "attach" ? "PUT" : "DELETE", body: kind === "attach" ? {} : undefined, ifMatch: preview.revision });
      setChoice("");
      props.onDone(`${kind === "attach" ? "Attached" : "Detached"} profile ${name} ${kind === "attach" ? "to" : "from"} ${node.path}: revision ${r.revision}.`);
    } catch (e) {
      props.onError(e);
    }
  };
  const available = (names ?? []).filter((n) => !attached.includes(n));

  return (
    <section aria-labelledby="prof-title">
      <h2 id="prof-title">Profiles</h2>
      <p className="muted hint">Applied at this node in this order (a later profile beats an earlier one); the node's own variables beat them all.</p>
      {attached.length === 0 ? (
        <p className="muted">No profile attached.</p>
      ) : (
        <ol className="profiles" aria-label="Attached profiles">
          {attached.map((name) => (
            <li key={name} data-profile={name}>
              <a href={href("profiles", { name })}>{name}</a>
              {editable && (
                <button type="button" className="small secondary" disabled={pending !== null} onClick={() => ask("detach", name)} aria-label={`Detach ${name}`}>
                  Detach
                </button>
              )}
            </li>
          ))}
        </ol>
      )}
      {editable && available.length > 0 && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (choice) ask("attach", choice);
          }}
        >
          <label>
            Profile{" "}
            <select value={choice} onChange={(e) => { setChoice(e.target.value); setPending(null); }} aria-label="Profile to attach">
              <option value="">(choose)</option>
              {available.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>{" "}
          <button type="submit" className="small" disabled={!choice || pending !== null}>
            Attach
          </button>
        </form>
      )}
      {pending && (
        <div className="preview-row" aria-label={`${pending.kind === "attach" ? "Attaching" : "Detaching"} ${pending.name}`}>
          {pending.preview?.impact ? (
            <>
              <ImpactSummary impact={pending.preview.impact} />
              <button type="button" className="small" onClick={confirm}>
                Confirm {pending.kind}
              </button>{" "}
              <button type="button" className="small secondary" onClick={() => setPending(null)}>
                Cancel
              </button>{" "}
              <span className="muted">based on revision {pending.preview.revision}</span>
            </>
          ) : (
            <span className="muted">Asking the server what this would change…</span>
          )}
        </div>
      )}
    </section>
  );
}

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, errorText } from "../api";
import { ImpactSummary, Notice, Spinner, StrategyBadge, Value } from "../components";
import { useApp } from "../context";
import { latestRevision } from "../hooks";
import { NodePicker, NON_HOST_KINDS } from "../NodePicker";
import { href, navigate } from "../router";
import type { InvNode, ProfileDoc, ProfileRow, Revisioned, WriteResult } from "../types";
import { canEdit } from "../types";
import { VarEditor } from "./VarEditor";

const NAME_RE = /^[A-Za-z0-9_-]{1,255}$/; // the node name rule; the server decides
type Msg = { kind: "ok" | "warn" | "error"; text: string } | null;

/** Profiles (issue 22): named bundles of variables attached to nodes. `#/profiles` lists
 * them; `#/profiles?name=gpu-a100` edits one: its values (the same editor and preview as a
 * node's), and where it is attached. A value change is one write that reaches every node the
 * profile is attached to; its preview lists those hosts. */
export function ProfilesView({ params }: { params: URLSearchParams }) {
  const name = params.get("name");
  return name ? <ProfileDetail key={name} name={name} /> : <ProfileList />;
}

function useStale(load: () => void, setMsg: (m: Msg) => void) {
  return (e: unknown): boolean => {
    if (e instanceof ApiError && e.stale) {
      setMsg({ kind: "warn", text: `Someone else changed the inventory (this page showed revision ${e.stale.expected}, the server is at ${e.stale.current}). Reloaded; apply your change again.` });
      load();
      return true;
    }
    setMsg({ kind: "error", text: `Refused: ${errorText(e)}` });
    return true;
  };
}

function ProfileList() {
  const { session } = useApp();
  const editable = canEdit(session.role);
  const [data, setData] = useState<Revisioned<ProfileRow[]> | null>(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState<Msg>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");

  const load = useCallback(async () => {
    try {
      const [revision, rows] = await Promise.all([latestRevision(), api<ProfileRow[]>("/api/v1/profiles")]);
      setData({ revision, rows });
      setError("");
    } catch (e) {
      setError(errorText(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);
  const onError = useStale(load, setMsg);

  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (!data) return;
    if (!NAME_RE.test(newName)) {
      setMsg({ kind: "error", text: "A profile name has letters, digits, _ and - only." });
      return;
    }
    try {
      await api<WriteResult>(`/api/v1/profiles/${encodeURIComponent(newName)}`, { method: "PUT", body: { description: newDescription || null, vars: {} }, ifMatch: data.revision });
      navigate("profiles", { name: newName });
    } catch (err) {
      onError(err);
    }
  };

  if (error) return <Notice kind="error">{error}</Notice>;
  if (!data) return <Spinner />;
  return (
    <div className="page">
      <div className="section-head">
        <h1>Profiles</h1>
        <span className="rev">revision {data.revision}</span>
      </div>
      <p className="muted">
        A profile is a named bundle of variables attached to any number of groups. At each node it applies as a level between the node's parent and the node: below the node's own variables, with the same merge strategies. Change a value once and every node it is attached to gets it. Use one when the same settings repeat in several branches of the tree; when they apply to everything under one node, set them on that node instead.
      </p>
      {msg && (
        <Notice kind={msg.kind} onClose={() => setMsg(null)}>
          {msg.text}
        </Notice>
      )}
      {editable && !creating && (
        <button type="button" onClick={() => setCreating(true)}>
          New profile
        </button>
      )}
      {creating && (
        <form className="editor" onSubmit={create} aria-label="New profile">
          <label>
            Name <input value={newName} onChange={(e) => setNewName(e.target.value.trim())} aria-label="Profile name" required autoFocus />
          </label>{" "}
          <label>
            Description <input value={newDescription} onChange={(e) => setNewDescription(e.target.value)} aria-label="Description" />
          </label>
          <div className="editor-foot">
            <button type="submit">Create</button>
            <button type="button" className="secondary" onClick={() => setCreating(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {data.rows.length === 0 ? (
        <p className="muted">No profiles yet.</p>
      ) : (
        <table className="list profiles">
          <thead>
            <tr>
              <th>Profile</th>
              <th>Description</th>
              <th>Keys</th>
              <th>Attached to</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((p) => (
              <tr key={p.name} data-name={p.name}>
                <th scope="row">
                  <a href={href("profiles", { name: p.name })}>{p.name}</a>
                </th>
                <td>{p.description}</td>
                <td>{p.keys.map((k) => <code key={k}>{k} </code>)}</td>
                <td>
                  {p.attached} node{p.attached === 1 ? "" : "s"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

type Pending = { label: string; url: string; method: "PUT" | "DELETE"; body?: unknown; preview: WriteResult | null; done: (r: WriteResult) => string };

function ProfileDetail({ name }: { name: string }) {
  const { session } = useApp();
  const editable = canEdit(session.role);
  const [doc, setDoc] = useState<ProfileDoc | null>(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState<Msg>(null);
  const [editing, setEditing] = useState<{ key: string | null; value: unknown } | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [target, setTarget] = useState("");
  const [description, setDescription] = useState<string | null>(null);

  const base = `/api/v1/profiles/${encodeURIComponent(name)}`;
  const load = useCallback(async () => {
    try {
      setDoc(await api<ProfileDoc>(base));
      setError("");
    } catch (e) {
      setError(errorText(e));
    }
  }, [base]);
  useEffect(() => {
    load();
  }, [load]);
  const onError = useStale(load, setMsg);

  /** Every write here: a dry run based on the page's revision, then the confirmation based on the preview's. */
  const ask = async (p: Omit<Pending, "preview">) => {
    if (!doc) return;
    setPending({ ...p, preview: null });
    try {
      const preview = await api<WriteResult>(p.url, { method: p.method, body: p.body, ifMatch: doc.revision, query: { dry_run: true } });
      setPending({ ...p, preview });
    } catch (e) {
      setPending(null);
      onError(e);
    }
  };
  const confirm = async () => {
    if (!pending?.preview) return;
    const p = pending;
    setPending(null);
    try {
      const r = await api<WriteResult>(p.url, { method: p.method, body: p.body, ifMatch: p.preview!.revision });
      setMsg({ kind: "ok", text: p.done(r) });
      if (p.method === "DELETE" && p.url === `${base}?detach=true`) navigate("profiles");
      else load();
    } catch (e) {
      onError(e);
    }
  };

  const attach = async (e: FormEvent) => {
    e.preventDefault();
    if (!target) return;
    try {
      const node = await api<InvNode>(`/api/v1/nodes/${target}`);
      ask({ label: `Attach to ${node.path}`, url: `/api/v1/nodes/${node.id}/profiles/${encodeURIComponent(name)}`, method: "PUT", body: {}, done: (r) => `Attached ${name} to ${node.path}: revision ${r.revision}.` });
    } catch (err) {
      onError(err);
    }
  };
  const detach = async (path: string) => {
    try {
      const node = await api<InvNode>(`/api/v1/nodes/${path}`);
      ask({ label: `Detach from ${path}`, url: `/api/v1/nodes/${node.id}/profiles/${encodeURIComponent(name)}`, method: "DELETE", done: (r) => `Detached ${name} from ${path}: revision ${r.revision}.` });
    } catch (err) {
      onError(err);
    }
  };

  if (error) return <Notice kind="error">{error}</Notice>;
  if (!doc) return <Spinner />;
  const keys = Object.keys(doc.vars).sort();
  return (
    <div className="detail">
      <header className="detail-head">
        <div>
          <h1>{doc.name}</h1>
          <p className="muted">
            profile · <a href={href("profiles")}>all profiles</a>
          </p>
        </div>
        <div className="head-actions">
          <span className="rev" title="Writes from this page are based on this revision (If-Match)">
            revision {doc.revision}
          </span>
          {editable && (
            <a className="button secondary" href={href("audit", { action: "profile." })} title="Profile changes (audit log)">
              History
            </a>
          )}
        </div>
      </header>
      {msg && (
        <Notice kind={msg.kind} onClose={() => setMsg(null)}>
          {msg.text}
        </Notice>
      )}
      {description === null ? (
        <p>
          {doc.description ?? <span className="muted">No description.</span>}{" "}
          {editable && (
            <button type="button" className="small secondary" onClick={() => setDescription(doc.description ?? "")}>
              Edit description
            </button>
          )}
        </p>
      ) : (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            const text = description;
            setDescription(null);
            ask({ label: "Change the description", url: base, method: "PUT", body: { description: text || null }, done: (r) => `Saved the description: revision ${r.revision}.` });
          }}
        >
          <input value={description} onChange={(e) => setDescription(e.target.value)} aria-label="Description" autoFocus />{" "}
          <button type="submit" className="small">
            Save
          </button>
        </form>
      )}

      {pending && (
        <div className="preview-row" aria-label={pending.label}>
          <strong>{pending.label}</strong>{" "}
          {pending.preview?.impact ? (
            <>
              <ImpactSummary impact={pending.preview.impact} />
              <button type="button" className="small" onClick={confirm}>
                Confirm
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

      <section aria-labelledby="pvars-title">
        <div className="section-head">
          <h2 id="pvars-title">Values</h2>
          {editable && !editing && (
            <button type="button" onClick={() => setEditing({ key: null, value: "" })}>
              Add value
            </button>
          )}
        </div>
        {editing && editing.key === null && (
          <VarEditor
            url={`${base}/vars`}
            keyName={null}
            initial={editing.value}
            revision={doc.revision}
            onSaved={(k, r) => {
              setEditing(null);
              setMsg({ kind: "ok", text: r.changed ? `Saved ${k}: revision ${r.revision}.` : `No change: ${k} already had that value.` });
              load();
            }}
            onError={onError}
            onCancel={() => setEditing(null)}
          />
        )}
        {keys.length === 0 ? (
          <p className="muted">No values yet.</p>
        ) : (
          <table className="vars profile-vars">
            <thead>
              <tr>
                <th>Key</th>
                <th>Value</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {keys.map((k) =>
                editing?.key === k ? (
                  <tr key={k}>
                    <td colSpan={3}>
                      <VarEditor
                        url={`${base}/vars`}
                        keyName={k}
                        initial={doc.vars[k]}
                        revision={doc.revision}
                        onSaved={(key, r) => {
                          setEditing(null);
                          setMsg({ kind: "ok", text: r.changed ? `Saved ${key}: revision ${r.revision}.` : `No change: ${key} already had that value.` });
                          load();
                        }}
                        onError={onError}
                        onCancel={() => setEditing(null)}
                      />
                    </td>
                  </tr>
                ) : (
                  <tr key={k} data-key={k}>
                    <th scope="row">
                      <code>{k}</code> <StrategyBadge name={k} />
                    </th>
                    <td>
                      <Value value={doc.vars[k]} />
                    </td>
                    <td className="actions">
                      <a className="small" href={href("search", { key: k })} aria-label={`Where is ${k} set`}>
                        where set
                      </a>
                      {editable && (
                        <>
                          <button type="button" className="small" onClick={() => setEditing({ key: k, value: doc.vars[k] })} aria-label={`Edit ${k}`}>
                            Edit
                          </button>
                          <button
                            type="button"
                            className="small secondary"
                            disabled={pending !== null}
                            onClick={() => ask({ label: `Remove ${k}`, url: `${base}/vars/${encodeURIComponent(k)}`, method: "DELETE", done: (r) => `Removed ${k}: revision ${r.revision}.` })}
                            aria-label={`Remove ${k}`}
                          >
                            Remove
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        )}
      </section>

      <section aria-labelledby="att-title">
        <h2 id="att-title">Attached to</h2>
        {doc.attached.length === 0 ? (
          <p className="muted">Attached nowhere: these values reach no host.</p>
        ) : (
          <table className="list attached">
            <thead>
              <tr>
                <th>Node</th>
                <th>Kind</th>
                <th title="Its place in that node's profiles (0 = applied first, lowest precedence)">Position</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {doc.attached.map((a) => (
                <tr key={a.path} data-path={a.path}>
                  <td>
                    <a href={href("tree", { path: a.path })}>{a.path}</a>
                  </td>
                  <td>{a.kind}</td>
                  <td>{a.position}</td>
                  <td className="actions">
                    {editable && (
                      <button type="button" className="small secondary" disabled={pending !== null} onClick={() => detach(a.path)} aria-label={`Detach from ${a.path}`}>
                        Detach
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {editable && (
          <form className="inline-form" onSubmit={attach} aria-label="Attach to a node">
            <NodePicker label="Attach to" value={target} onChange={setTarget} kinds={NON_HOST_KINDS} pick="path" placeholder="a group path" />{" "}
            <button type="submit" className="small" disabled={!target || pending !== null}>
              Attach
            </button>
          </form>
        )}
      </section>

      {editable && (
        <section aria-labelledby="del-title">
          <h2 id="del-title">Delete</h2>
          <button
            type="button"
            className="danger"
            disabled={pending !== null}
            onClick={() => ask({ label: `Delete ${name}${doc.attached.length ? `, detaching it from ${doc.attached.length} node(s)` : ""}`, url: `${base}?detach=true`, method: "DELETE", done: (r) => `Deleted profile ${name}: revision ${r.revision}.` })}
          >
            Delete profile
          </button>
        </section>
      )}
    </div>
  );
}

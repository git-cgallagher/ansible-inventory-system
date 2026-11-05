import { Fragment, useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, errorText } from "../api";
import { ImpactSummary, Notice, Spinner, StrategyBadge, Value } from "../components";
import { useApp } from "../context";
import { latestRevision } from "../hooks";
import { href } from "../router";
import type { Definition, Revisioned, Strategy, WriteResult } from "../types";
import { STRATEGIES, canEdit } from "../types";
import { formatValue, parseValue } from "../value";

const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
type Msg = { kind: "ok" | "warn" | "error"; text: string } | null;

/** Variable definitions (`/variables`): merge strategy, description, JSON Schema. Undeclared
 * variables are plain replace. Definitions are not temporal, so writes carry the revision read
 * together with the list. A strategy change can change every host that has the key, so Save
 * and Undeclare first show the server's dry run ("this changes N hosts"); the confirmation
 * carries the preview's revision. */
export function VariablesView() {
  const { session, reloadDefs } = useApp();
  const editable = canEdit(session.role);
  const [data, setData] = useState<Revisioned<Definition[]> | null>(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState<Msg>(null);
  const [editing, setEditing] = useState<Definition | "new" | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [undeclaring, setUndeclaring] = useState<WriteResult | null>(null); // the dry run of undeclaring `confirm`

  const load = useCallback(async () => {
    try {
      const [revision, rows] = await Promise.all([latestRevision(), api<Definition[]>("/api/v1/variables")]);
      setData({ revision, rows });
      setError("");
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onError = (e: unknown) => {
    if (e instanceof ApiError && e.stale) {
      setMsg({ kind: "warn", text: `Someone else changed the inventory (this page showed revision ${e.stale.expected}, the server is at ${e.stale.current}). Reloaded; apply your change again.` });
      load();
    } else setMsg({ kind: "error", text: `Refused: ${errorText(e)}` });
  };

  const done = (text: string) => {
    setEditing(null);
    setConfirm(null);
    setMsg({ kind: "ok", text });
    load();
    reloadDefs();
  };

  const askRemove = async (name: string) => {
    if (!data) return;
    setConfirm(name);
    setUndeclaring(null);
    try {
      setUndeclaring(await api<WriteResult>(`/api/v1/variables/${encodeURIComponent(name)}`, { method: "DELETE", ifMatch: data.revision, query: { dry_run: true } }));
    } catch (e) {
      setConfirm(null);
      onError(e);
    }
  };

  const remove = async (name: string) => {
    if (!data || !undeclaring) return;
    try {
      const r = await api<WriteResult>(`/api/v1/variables/${encodeURIComponent(name)}`, { method: "DELETE", ifMatch: undeclaring.revision });
      done(`Undeclared ${name} (now plain replace): revision ${r.revision}.`);
    } catch (e) {
      onError(e);
    }
  };

  if (error) return <Notice kind="error">{error}</Notice>;
  if (!data) return <Spinner />;
  return (
    <div className="page">
      <div className="section-head">
        <h1>Variables</h1>
        <span className="rev">revision {data.revision}</span>
      </div>
      <p className="muted">
        Declared variables and how they combine down the tree. Anything not declared here is plain replace: the deepest node wins.
      </p>
      {msg && (
        <Notice kind={msg.kind} onClose={() => setMsg(null)}>
          {msg.text}
        </Notice>
      )}
      {editable && editing === null && (
        <button type="button" onClick={() => setEditing("new")}>
          Declare a variable
        </button>
      )}
      {editing === "new" && <DefinitionForm revision={data.revision} onDone={done} onError={onError} onCancel={() => setEditing(null)} />}
      <table className="list definitions">
        <thead>
          <tr>
            <th>Name</th>
            <th>Strategy</th>
            <th>Description</th>
            <th>Schema</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {data.rows.map((d) =>
            editing !== "new" && editing?.name === d.name ? (
              <tr key={d.name}>
                <td colSpan={5}>
                  <DefinitionForm initial={d} revision={data.revision} onDone={done} onError={onError} onCancel={() => setEditing(null)} />
                </td>
              </tr>
            ) : (
              <Fragment key={d.name}>
              <tr data-name={d.name}>
                <th scope="row">
                  <code>{d.name}</code>
                </th>
                <td>
                  <StrategyBadge name={d.name} />
                </td>
                <td>{d.description}</td>
                <td>{d.json_schema ? <Value value={d.json_schema} block /> : <span className="muted">none</span>}</td>
                <td className="actions">
                  <a className="small" href={href("search", { key: d.name })} aria-label={`Where is ${d.name} set`} title="The nodes that set it and every host's resolved value">
                    where set
                  </a>
                  {editable && (
                    <>
                      <button type="button" className="small" onClick={() => setEditing(d)} aria-label={`Edit ${d.name}`}>
                        Edit
                      </button>
                      {confirm === d.name ? (
                        <button type="button" className="small danger" disabled={!undeclaring} onClick={() => remove(d.name)} aria-label={`Confirm undeclaring ${d.name}`}>
                          Confirm
                        </button>
                      ) : (
                        <button type="button" className="small secondary" onClick={() => askRemove(d.name)} aria-label={`Undeclare ${d.name}`}>
                          Undeclare
                        </button>
                      )}
                    </>
                  )}
                </td>
              </tr>
              {confirm === d.name && (
                <tr className="preview-row" aria-label={`Undeclaring ${d.name}`}>
                  <td colSpan={5}>
                    {undeclaring?.impact ? <ImpactSummary impact={undeclaring.impact} /> : <span className="muted">Asking the server what undeclaring {d.name} would change…</span>}
                  </td>
                </tr>
              )}
              </Fragment>
            ),
          )}
        </tbody>
      </table>
      {data.rows.length === 0 && <p className="muted">Nothing declared.</p>}
    </div>
  );
}

function DefinitionForm(props: { initial?: Definition; revision: number; onDone: (text: string) => void; onError: (e: unknown) => void; onCancel: () => void }) {
  const init = props.initial;
  const [name, setName] = useState(init?.name ?? "");
  const [strategy, setStrategy] = useState<Strategy>(init?.merge_strategy ?? "replace");
  const [description, setDescription] = useState(init?.description ?? "");
  const [schema, setSchema] = useState(init?.json_schema ? formatValue(init.json_schema, "json") : "");
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<WriteResult | null>(null);
  const edited = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setPreview(null);
  };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!KEY_RE.test(name)) {
      setProblem("Name must be a valid Ansible variable name.");
      return;
    }
    let json_schema: unknown = null;
    if (schema.trim()) {
      try {
        json_schema = parseValue(schema, "json");
      } catch (err) {
        setProblem(`Schema: ${(err as Error).message}`);
        return;
      }
      if (!json_schema || typeof json_schema !== "object" || Array.isArray(json_schema)) {
        setProblem("Schema must be a JSON object.");
        return;
      }
    }
    setBusy(true);
    setProblem("");
    const url = `/api/v1/variables/${encodeURIComponent(name)}`;
    const body = { merge_strategy: strategy, description: description.trim() || null, json_schema };
    try {
      if (preview === null) {
        const dry = await api<WriteResult>(url, { method: "PUT", body, ifMatch: props.revision, query: { dry_run: true } });
        if (dry.changed) setPreview(dry);
        else props.onDone(`No change to ${name}.`);
      } else {
        const r = await api<WriteResult>(url, { method: "PUT", body, ifMatch: preview.revision });
        props.onDone(r.changed ? `Saved ${name}: revision ${r.revision}.` : `No change to ${name}.`);
      }
    } catch (err) {
      setPreview(null);
      props.onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="editor" onSubmit={save} onKeyDown={(e) => e.key === "Escape" && props.onCancel()} aria-label={init ? `Edit definition ${init.name}` : "Declare a variable"}>
      <div className="editor-head">
        {init ? (
          <code className="key">{init.name}</code>
        ) : (
          <label>
            Name <input value={name} onChange={(e) => edited(setName)(e.target.value.trim())} required autoFocus aria-label="Variable name" />
          </label>
        )}
        <label>
          Strategy{" "}
          <select value={strategy} onChange={(e) => edited(setStrategy)(e.target.value as Strategy)} aria-label="Merge strategy">
            {STRATEGIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label>
        Description
        <input value={description} onChange={(e) => edited(setDescription)(e.target.value)} aria-label="Description" />
      </label>
      <label>
        JSON Schema (optional; every stored value of this variable must match it)
        <textarea value={schema} onChange={(e) => edited(setSchema)(e.target.value)} rows={4} spellCheck={false} aria-label="JSON Schema" placeholder='{"type": "integer", "minimum": 1}' />
      </label>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      {preview?.impact && <ImpactSummary impact={preview.impact} />}
      <div className="editor-foot">
        <button type="submit" disabled={busy}>
          {busy ? (preview ? "Saving…" : "Checking…") : preview ? "Confirm save" : "Save"}
        </button>
        <button type="button" className="secondary" onClick={props.onCancel}>
          Cancel
        </button>
        <span className="muted">based on revision {preview?.revision ?? props.revision}</span>
      </div>
    </form>
  );
}

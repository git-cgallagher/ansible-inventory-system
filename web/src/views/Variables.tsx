import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, errorText } from "../api";
import { Notice, Spinner, StrategyBadge, Value } from "../components";
import { useApp } from "../context";
import { latestRevision } from "../hooks";
import type { Definition, Revisioned, Strategy, WriteResult } from "../types";
import { STRATEGIES, canEdit } from "../types";
import { formatValue, parseValue } from "../value";

const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
type Msg = { kind: "ok" | "warn" | "error"; text: string } | null;

/** Variable definitions (`/variables`): merge strategy, description, JSON Schema. Undeclared
 * variables are plain replace. Definitions are not temporal, so writes carry the revision read
 * together with the list. */
export function VariablesView() {
  const { session, reloadDefs } = useApp();
  const editable = canEdit(session.role);
  const [data, setData] = useState<Revisioned<Definition[]> | null>(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState<Msg>(null);
  const [editing, setEditing] = useState<Definition | "new" | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);

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

  const remove = async (name: string) => {
    if (!data) return;
    try {
      const r = await api<WriteResult>(`/api/v1/variables/${encodeURIComponent(name)}`, { method: "DELETE", ifMatch: data.revision });
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
            {editable && <th aria-label="Actions" />}
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
              <tr key={d.name} data-name={d.name}>
                <th scope="row">
                  <code>{d.name}</code>
                </th>
                <td>
                  <StrategyBadge name={d.name} />
                </td>
                <td>{d.description}</td>
                <td>{d.json_schema ? <Value value={d.json_schema} block /> : <span className="muted">none</span>}</td>
                {editable && (
                  <td className="actions">
                    <button type="button" className="small" onClick={() => setEditing(d)} aria-label={`Edit ${d.name}`}>
                      Edit
                    </button>
                    {confirm === d.name ? (
                      <button type="button" className="small danger" onClick={() => remove(d.name)} aria-label={`Confirm undeclaring ${d.name}`}>
                        Confirm
                      </button>
                    ) : (
                      <button type="button" className="small secondary" onClick={() => setConfirm(d.name)} aria-label={`Undeclare ${d.name}`}>
                        Undeclare
                      </button>
                    )}
                  </td>
                )}
              </tr>
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
    try {
      const r = await api<WriteResult>(`/api/v1/variables/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: { merge_strategy: strategy, description: description.trim() || null, json_schema },
        ifMatch: props.revision,
      });
      props.onDone(r.changed ? `Saved ${name}: revision ${r.revision}.` : `No change to ${name}.`);
    } catch (err) {
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
            Name <input value={name} onChange={(e) => setName(e.target.value.trim())} required autoFocus aria-label="Variable name" />
          </label>
        )}
        <label>
          Strategy{" "}
          <select value={strategy} onChange={(e) => setStrategy(e.target.value as Strategy)} aria-label="Merge strategy">
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
        <input value={description} onChange={(e) => setDescription(e.target.value)} aria-label="Description" />
      </label>
      <label>
        JSON Schema (optional; every stored value of this variable must match it)
        <textarea value={schema} onChange={(e) => setSchema(e.target.value)} rows={4} spellCheck={false} aria-label="JSON Schema" placeholder='{"type": "integer", "minimum": 1}' />
      </label>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      <div className="editor-foot">
        <button type="submit" disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button type="button" className="secondary" onClick={props.onCancel}>
          Cancel
        </button>
        <span className="muted">based on revision {props.revision}</span>
      </div>
    </form>
  );
}

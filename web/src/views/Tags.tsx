import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, errorText } from "../api";
import { Notice, Spinner } from "../components";
import { useApp } from "../context";
import { latestRevision } from "../hooks";
import { href } from "../router";
import type { Revisioned, TagRow, WriteResult } from "../types";
import { canEdit } from "../types";

const TAG_RE = /^[A-Za-z0-9_]{1,255}$/;
type Msg = { kind: "ok" | "warn" | "error"; text: string } | null;

/** Tags (`/tags`): extra var-less groups under `all` for targeting (`--limit canary`). A member
 * is a node path; a group member contributes every host under it. Not temporal, so writes carry
 * the revision read together with the list. */
export function TagsView() {
  const { session } = useApp();
  const editable = canEdit(session.role);
  const [data, setData] = useState<Revisioned<TagRow[]> | null>(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState<Msg>(null);
  const [editing, setEditing] = useState<TagRow | "new" | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [revision, rows] = await Promise.all([latestRevision(), api<TagRow[]>("/api/v1/tags")]);
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
  };

  const remove = async (name: string) => {
    if (!data) return;
    try {
      const r = await api<WriteResult>(`/api/v1/tags/${encodeURIComponent(name)}`, { method: "DELETE", ifMatch: data.revision });
      done(`Deleted tag ${name}: revision ${r.revision}.`);
    } catch (e) {
      onError(e);
    }
  };

  if (error) return <Notice kind="error">{error}</Notice>;
  if (!data) return <Spinner />;
  return (
    <div className="page">
      <div className="section-head">
        <h1>Tags</h1>
        <span className="rev">revision {data.revision}</span>
      </div>
      <p className="muted">
        Extra groups for targeting only: they never carry variables. The automatic <code>business_</code>, <code>tier_</code> and <code>datacenter_</code> groups are not listed here.
      </p>
      {msg && (
        <Notice kind={msg.kind} onClose={() => setMsg(null)}>
          {msg.text}
        </Notice>
      )}
      {editable && editing === null && (
        <button type="button" onClick={() => setEditing("new")}>
          New tag
        </button>
      )}
      {editing === "new" && <TagForm revision={data.revision} onDone={done} onError={onError} onCancel={() => setEditing(null)} />}
      <table className="list tags">
        <thead>
          <tr>
            <th>Tag</th>
            <th>Description</th>
            <th>Members</th>
            {editable && <th aria-label="Actions" />}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((t) =>
            editing !== "new" && editing?.name === t.name ? (
              <tr key={t.name}>
                <td colSpan={4}>
                  <TagForm initial={t} revision={data.revision} onDone={done} onError={onError} onCancel={() => setEditing(null)} />
                </td>
              </tr>
            ) : (
              <tr key={t.name} data-name={t.name}>
                <th scope="row">
                  <code>{t.name}</code>
                </th>
                <td>{t.description}</td>
                <td>
                  {t.members.length === 0 && <span className="muted">none (not emitted)</span>}
                  {t.members.map((m) => (
                    <div key={m}>
                      <a href={href("tree", { path: m })}>{m}</a>
                    </div>
                  ))}
                </td>
                {editable && (
                  <td className="actions">
                    <button type="button" className="small" onClick={() => setEditing(t)} aria-label={`Edit tag ${t.name}`}>
                      Edit
                    </button>
                    {confirm === t.name ? (
                      <button type="button" className="small danger" onClick={() => remove(t.name)} aria-label={`Confirm deleting tag ${t.name}`}>
                        Confirm
                      </button>
                    ) : (
                      <button type="button" className="small secondary" onClick={() => setConfirm(t.name)} aria-label={`Delete tag ${t.name}`}>
                        Delete
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ),
          )}
        </tbody>
      </table>
      {data.rows.length === 0 && <p className="muted">No tags yet.</p>}
    </div>
  );
}

function TagForm(props: { initial?: TagRow; revision: number; onDone: (text: string) => void; onError: (e: unknown) => void; onCancel: () => void }) {
  const init = props.initial;
  const [name, setName] = useState(init?.name ?? "");
  const [description, setDescription] = useState(init?.description ?? "");
  const [members, setMembers] = useState((init?.members ?? []).join("\n"));
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!TAG_RE.test(name)) {
      setProblem("Tag names are letters, digits and '_' only.");
      return;
    }
    const paths = members
      .split(/[\s,]+/)
      .map((m) => m.trim())
      .filter(Boolean);
    setBusy(true);
    setProblem("");
    try {
      const r = await api<WriteResult>(`/api/v1/tags/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: { description: description.trim() || null, members: paths },
        ifMatch: props.revision,
      });
      props.onDone(r.changed ? `Saved tag ${name}: revision ${r.revision}.` : `No change to tag ${name}.`);
    } catch (err) {
      props.onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="editor" onSubmit={save} onKeyDown={(e) => e.key === "Escape" && props.onCancel()} aria-label={init ? `Edit tag ${init.name}` : "New tag"}>
      <div className="editor-head">
        {init ? (
          <code className="key">{init.name}</code>
        ) : (
          <label>
            Name <input value={name} onChange={(e) => setName(e.target.value.trim())} required autoFocus aria-label="Tag name" />
          </label>
        )}
      </div>
      <label>
        Description
        <input value={description} onChange={(e) => setDescription(e.target.value)} aria-label="Description" />
      </label>
      <label>
        Members (node paths, one per line; a group brings every host under it)
        <textarea value={members} onChange={(e) => setMembers(e.target.value)} rows={4} spellCheck={false} aria-label="Members" placeholder="all.commercial.prod.dc1.web" />
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

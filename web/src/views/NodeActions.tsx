import { useState, type FormEvent } from "react";
import { api } from "../api";
import { ImpactSummary } from "../components";
import type { DeleteResult, InvNode, MoveResult, WriteResult } from "../types";
import { CHILD_KINDS } from "../types";

type Mode = "child" | "move" | "delete" | null;

const NAME_RE = /^[A-Za-z0-9_-]{1,255}$/;

/** Structure writes on one node: add a child, move/rename, delete (with its subtree).
 * Each carries `If-Match` = the revision the page shows; errors go to the page's handler,
 * which turns a stale 409 into "reloaded, try again". `onDone(path)` gets the path to show
 * next (the new child, the moved node, or the deleted node's parent).
 *
 * Move and delete first ask the server for a dry run (nothing is written; `If-Match` = the
 * page's revision, so a stale page refuses here already) and show what would change: the
 * hosts (`impact`), and for a delete how many nodes and hosts would go. The confirmation sends
 * `If-Match` = the preview's revision (and for a delete `recursive=true` and `expect_count` =
 * the count shown), so the server refuses (409) if anything changed since. Editing the move's
 * target drops its preview. */
export function NodeActions(props: {
  node: InvNode;
  revision: number;
  onDone: (path: string, message: string) => void;
  onError: (e: unknown) => void;
}) {
  const { node, revision } = props;
  const [mode, setMode] = useState<Mode>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("");
  const [parentPath, setParentPath] = useState("");
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<DeleteResult | null>(null);
  const [movePreview, setMovePreview] = useState<MoveResult | null>(null);
  const parentOf = (p: string) => p.slice(0, p.lastIndexOf("."));
  const isRoot = node.kind === "all";
  const isHost = node.kind === "host";

  const open = (m: Mode) => {
    setMode(m);
    setProblem("");
    setName(m === "move" ? node.name : "");
    setKind("");
    setParentPath(m === "move" ? parentOf(node.path) : "");
    setPreview(null);
    setMovePreview(null);
    if (m === "delete") {
      api<DeleteResult>(`/api/v1/nodes/${node.id}`, { method: "DELETE", ifMatch: revision, query: { dry_run: true, recursive: true } })
        .then(setPreview)
        .catch((err) => {
          setMode(null);
          props.onError(err);
        });
    }
  };

  const run = async (e: FormEvent) => {
    e.preventDefault();
    if (mode !== "delete" && !NAME_RE.test(name)) {
      setProblem("Names are letters, digits, '_' and '-' only.");
      return;
    }
    setBusy(true);
    setProblem("");
    try {
      if (mode === "child") {
        const body: Record<string, unknown> = { parent_id: node.id, name };
        if (kind) body.kind = kind;
        const r = await api<WriteResult & { node: InvNode }>("/api/v1/nodes", { method: "POST", body, ifMatch: revision });
        props.onDone(r.node.path, `Created ${r.node.path} (${r.node.kind}): revision ${r.revision}.`);
      } else if (mode === "move" && movePreview === null) {
        setMovePreview(
          await api<MoveResult>(`/api/v1/nodes/${node.id}/move`, {
            method: "POST",
            body: { parent_path: parentPath.trim(), name },
            ifMatch: revision,
            query: { dry_run: true },
          }),
        );
        return; // stay open: the user confirms what the preview shows
      } else if (mode === "move" && movePreview) {
        const r = await api<MoveResult>(`/api/v1/nodes/${node.id}/move`, {
          method: "POST",
          body: { parent_path: parentPath.trim(), name },
          ifMatch: movePreview.revision,
        });
        props.onDone(r.node.path, `Moved ${node.path} to ${r.node.path} with ${r.moved.length} node(s): revision ${r.revision}.`);
      } else if (mode === "delete" && preview) {
        const r = await api<DeleteResult>(`/api/v1/nodes/${node.id}`, {
          method: "DELETE",
          query: { recursive: true, expect_count: preview.nodes },
          ifMatch: preview.revision,
        });
        props.onDone(parentOf(node.path), `Deleted ${r.nodes} node(s) (${r.hosts} host(s)) under ${node.path}: revision ${r.revision}.`);
      }
      setMode(null);
    } catch (err) {
      setMovePreview(null);
      props.onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="node-actions">
      {mode === null && (
        <div className="inline">
          {!isHost && (
            <button type="button" className="small secondary" onClick={() => open("child")}>
              Add child
            </button>
          )}
          {!isRoot && (
            <button type="button" className="small secondary" onClick={() => open("move")}>
              Move / rename
            </button>
          )}
          {!isRoot && (
            <button type="button" className="small secondary" onClick={() => open("delete")}>
              Delete
            </button>
          )}
        </div>
      )}
      {mode !== null && (
        <form className="editor" onSubmit={run} onKeyDown={(e) => e.key === "Escape" && setMode(null)} aria-label={mode === "child" ? "Add a child node" : mode === "move" ? "Move or rename" : "Delete node"}>
          {mode === "child" && (
            <div className="editor-head">
              <label>
                Name <input value={name} onChange={(e) => setName(e.target.value.trim())} required autoFocus aria-label="Child name" />
              </label>
              <label>
                Kind{" "}
                <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Child kind">
                  <option value="">from the depth</option>
                  {CHILD_KINDS.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
          {mode === "move" && (
            <>
              <div className="editor-head">
                <label>
                  New parent{" "}
                  <input
                    value={parentPath}
                    onChange={(e) => {
                      setParentPath(e.target.value);
                      setMovePreview(null);
                    }}
                    required
                    aria-label="New parent path"
                    size={40}
                  />
                </label>
                <label>
                  New name{" "}
                  <input
                    value={name}
                    onChange={(e) => {
                      setName(e.target.value.trim());
                      setMovePreview(null);
                    }}
                    required
                    aria-label="New name"
                  />
                </label>
              </div>
              <p className="muted hint">The whole subtree moves with its variables and tags. Node ids change; earlier revisions keep the old place.</p>
              {movePreview?.impact && <ImpactSummary impact={movePreview.impact} />}
            </>
          )}
          {mode === "delete" && (
            <>
              {preview === null && !problem && <p className="muted">Counting what the delete would take…</p>}
              {preview !== null && preview.nodes === 1 && (
                <p>
                  Delete <code>{node.path}</code>? Earlier revisions keep it.
                </p>
              )}
              {preview !== null && preview.nodes > 1 && (
                <p>
                  Delete <code>{node.path}</code> and everything under it:{" "}
                  <strong data-testid="delete-count">
                    {preview.nodes} nodes, {preview.hosts} {preview.hosts === 1 ? "host" : "hosts"}
                  </strong>
                  ? Earlier revisions keep them, and a restore (<code>POST /api/v1/nodes/restore</code>) brings them back.
                </p>
              )}
              {preview?.impact && <ImpactSummary impact={preview.impact} />}
            </>
          )}
          {problem && (
            <p className="error" role="alert">
              {problem}
            </p>
          )}
          <div className="editor-foot">
            <button type="submit" className={mode === "delete" ? "danger" : ""} disabled={busy || (mode === "delete" && preview === null)}>
              {busy
                ? "Working…"
                : mode === "child"
                  ? "Create"
                  : mode === "move"
                    ? movePreview
                      ? "Confirm move"
                      : "Move"
                    : preview && preview.nodes > 1
                      ? `Delete ${preview.nodes} nodes`
                      : "Delete node"}
            </button>
            <button type="button" className="secondary" onClick={() => setMode(null)}>
              Cancel
            </button>
            <span className="muted">based on revision {movePreview?.revision ?? preview?.revision ?? revision}</span>
          </div>
        </form>
      )}
    </div>
  );
}

import { useState, type KeyboardEvent } from "react";
import { api } from "../api";
import { StrategyBadge } from "../components";
import { useApp } from "../context";
import type { InvNode, WriteResult } from "../types";
import { formatValue, parseValue, type Format } from "../value";

const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Edit one key at one node as JSON or YAML. The write carries `If-Match` with the
 * revision the page shows; on a stale 409 the parent reloads and this form keeps the
 * text so the change can be re-applied. */
export function VarEditor(props: {
  node: InvNode;
  keyName: string | null;
  presetKey?: string;
  initial: unknown;
  revision: number;
  onSaved: (key: string, r: WriteResult) => void;
  onError: (e: unknown) => boolean;
  onCancel: () => void;
}) {
  const { defs } = useApp();
  const [key, setKey] = useState(props.keyName ?? props.presetKey ?? "");
  const [format, setFormat] = useState<Format>("json");
  const [text, setText] = useState(() => formatValue(props.initial, "json"));
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const def = defs.get(key);

  const switchTo = (next: Format) => {
    if (next === format) return;
    try {
      setText(formatValue(parseValue(text, format), next));
      setFormat(next);
      setProblem("");
    } catch (e) {
      setProblem(`Fix the ${format.toUpperCase()} before switching: ${(e as Error).message}`);
    }
  };

  const save = async (e?: { preventDefault(): void }) => {
    e?.preventDefault();
    if (!KEY_RE.test(key)) {
      setProblem("Key must be a valid Ansible variable name (letters, digits, _; not starting with a digit).");
      return;
    }
    let value: unknown;
    try {
      value = parseValue(text, format);
    } catch (err) {
      setProblem((err as Error).message);
      return;
    }
    setProblem("");
    setBusy(true);
    try {
      const r = await api<WriteResult>(`/api/v1/nodes/${props.node.id}/vars/${encodeURIComponent(key)}`, {
        method: "PUT",
        body: { value },
        ifMatch: props.revision,
      });
      props.onSaved(key, r);
    } catch (err) {
      props.onError(err);
    } finally {
      setBusy(false);
    }
  };

  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") props.onCancel();
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) save(e);
  };

  return (
    <form className="editor" onSubmit={save} onKeyDown={onKey} aria-label={`Edit variable ${key || "(new)"}`}>
      <div className="editor-head">
        {props.keyName ? (
          <code className="key">{props.keyName}</code>
        ) : (
          <label>
            Key{" "}
            <input value={key} onChange={(e) => setKey(e.target.value.trim())} list="declared-vars" required autoFocus={!props.presetKey} aria-label="Key" />
            <datalist id="declared-vars">
              {[...defs.keys()].map((d) => (
                <option key={d} value={d} />
              ))}
            </datalist>
          </label>
        )}
        {key && <StrategyBadge name={key} />}
        <span className="seg" role="group" aria-label="Format">
          {(["json", "yaml"] as const).map((f) => (
            <button key={f} type="button" className={format === f ? "on" : ""} aria-pressed={format === f} onClick={() => switchTo(f)}>
              {f.toUpperCase()}
            </button>
          ))}
        </span>
      </div>
      {def?.description && <p className="muted hint">{def.description}</p>}
      {def && def.merge_strategy !== "replace" && (
        <p className="muted hint">
          {def.merge_strategy === "merge" ? "Merged key by key" : def.merge_strategy === "append" ? "Appended" : "Appended without duplicates"} onto what this node inherits; write only this level's contribution.
        </p>
      )}
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={Math.min(16, Math.max(3, text.split("\n").length + 1))}
        spellCheck={false}
        aria-label="Value"
        autoFocus={Boolean(props.keyName || props.presetKey)}
      />
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
        <span className="muted">
          based on revision {props.revision} · Esc cancels · Ctrl/⌘+Enter saves
        </span>
      </div>
    </form>
  );
}

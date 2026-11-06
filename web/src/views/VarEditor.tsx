import { useState, type KeyboardEvent } from "react";
import { api } from "../api";
import { ImpactSummary, StrategyBadge } from "../components";
import { useApp } from "../context";
import type { InvNode, WriteResult } from "../types";
import { formatValue, parseValue, type Format } from "../value";

const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Edit one key at one node as JSON or YAML. Save first asks the server for a dry run
 * (`If-Match` = the revision the page shows) and shows how many hosts the value changes;
 * "Confirm save" then writes with `If-Match` = that preview's revision, so a change made in
 * between refuses (409). On a stale 409 the parent reloads and this form keeps the text so
 * the change can be re-applied. Any edit drops the preview. */
export function VarEditor(props: {
  /** The node the value is set on; or `url`, the value's write URL without the key (a profile's). */
  node?: InvNode;
  url?: string;
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
  const [preview, setPreview] = useState<WriteResult | null>(null);
  const def = defs.get(key);

  const switchTo = (next: Format) => {
    if (next === format) return;
    try {
      setText(formatValue(parseValue(text, format), next));
      setFormat(next);
      setPreview(null);
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
    const url = `${props.url ?? `/api/v1/nodes/${props.node?.id}/vars`}/${encodeURIComponent(key)}`;
    try {
      if (preview === null) {
        const dry = await api<WriteResult>(url, { method: "PUT", body: { value }, ifMatch: props.revision, query: { dry_run: true } });
        if (dry.changed) setPreview(dry);
        else props.onSaved(key, dry); // the value is already there: nothing to confirm
      } else {
        const r = await api<WriteResult>(url, { method: "PUT", body: { value }, ifMatch: preview.revision });
        props.onSaved(key, r);
      }
    } catch (err) {
      setPreview(null);
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
            <input
              value={key}
              onChange={(e) => {
                setKey(e.target.value.trim());
                setPreview(null);
              }}
              list="declared-vars"
              required
              autoFocus={!props.presetKey}
              aria-label="Key"
            />
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
          {def.merge_strategy === "merge" ? "Merged key by key" : def.merge_strategy === "append" ? "Appended" : "Appended without duplicates"} onto what {props.url ? "each node it is attached to inherits" : "this node inherits"}; write only this level's contribution.
        </p>
      )}
      <textarea
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setPreview(null);
        }}
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
      {preview?.impact && <ImpactSummary impact={preview.impact} />}
      {preview?.warnings
        ?.filter((w) => w.key === key)
        .map((w) => (
          <p key={`${w.key}@${w.path}`} className="muted hint">
            {w.message} Saving stores it anyway; declare it on the Variables page if it is right.
          </p>
        ))}
      <div className="editor-foot">
        <button type="submit" disabled={busy}>
          {busy ? (preview ? "Saving…" : "Checking…") : preview ? "Confirm save" : "Save"}
        </button>
        <button type="button" className="secondary" onClick={props.onCancel}>
          Cancel
        </button>
        <span className="muted">
          based on revision {preview?.revision ?? props.revision} · Esc cancels · Ctrl/⌘+Enter {preview ? "confirms" : "saves"}
        </span>
      </div>
    </form>
  );
}

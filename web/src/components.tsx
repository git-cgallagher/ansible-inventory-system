import type { ReactNode } from "react";
import { useApp } from "./context";
import type { Contribution, Strategy } from "./types";
import { href } from "./router";
import { formatValue, short } from "./value";

export function StrategyBadge({ name }: { name: string }) {
  const { defs } = useApp();
  const def = defs.get(name);
  const strategy: Strategy = def?.merge_strategy ?? "replace";
  const label = def ? strategy.replace("_", " ") : "undeclared";
  const title = def
    ? `${strategy}${def.description ? `: ${def.description}` : ""}`
    : "Not declared in /variables: plain replace (deepest node wins)";
  return (
    <span className={`badge badge-${def ? strategy : "undeclared"}`} title={title}>
      {label}
    </span>
  );
}

export function Value({ value, block = false }: { value: unknown; block?: boolean }) {
  if (!block && (value === null || typeof value !== "object")) return <code className="value">{short(value)}</code>;
  return <pre className="value">{formatValue(value, "yaml")}</pre>;
}

/** Where a key's value comes from: the winning node for replace, every contributor for a fold. */
export function Provenance({ entries }: { entries: Contribution[] }) {
  if (!entries.length) return null;
  const strategy = entries[entries.length - 1].strategy;
  const shown = strategy === "replace" ? entries.slice(-1) : entries;
  return (
    <span className="prov">
      {strategy !== "replace" && <span className="muted">folded from </span>}
      {shown.map((c, i) => (
        <span key={`${c.path}-${i}`}>
          {i > 0 && <span className="muted"> + </span>}
          {c.path ? (
            <a href={href("tree", { path: c.path })} title={`group ${c.group}`}>
              {c.path}
            </a>
          ) : (
            <em>{c.group}</em>
          )}
        </span>
      ))}
    </span>
  );
}

export function Notice({ kind, children, onClose }: { kind: "ok" | "warn" | "error"; children: ReactNode; onClose?: () => void }) {
  return (
    <div className={`notice notice-${kind}`} role={kind === "ok" ? "status" : "alert"}>
      <span>{children}</span>
      {onClose && (
        <button type="button" className="link" onClick={onClose} aria-label="Dismiss">
          ×
        </button>
      )}
    </div>
  );
}

export function Spinner() {
  return <span className="muted">Loading…</span>;
}

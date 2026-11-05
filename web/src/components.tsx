import type { ReactNode } from "react";
import { useApp } from "./context";
import type { Contribution, Impact, ImpactHost, Strategy } from "./types";
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

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function hostChanges(h: ImpactHost): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(h.changed ?? {})) parts.push(`${k}: ${short(v.from)} → ${short(v.to)}`);
  for (const [k, v] of Object.entries(h.added ?? {})) parts.push(`+${k}: ${short(v)}`);
  for (const k of Object.keys(h.removed ?? {})) parts.push(`−${k}`);
  const groups = [...(h.joined ?? []).map((g) => `+${g}`), ...(h.left ?? []).map((g) => `−${g}`)];
  if (groups.length) parts.push(`groups ${groups.join(" ")}`);
  if (h.moved_from) parts.push(`from ${h.moved_from}`);
  return parts.join("; ");
}

/** A dry run's `impact`: "this changes N hosts", then the hosts (capped by the server) on demand. */
export function ImpactSummary({ impact }: { impact: Impact }) {
  const kinds = [
    impact.vars_changed ? `${impact.vars_changed} get different variables` : "",
    impact.added ? `${impact.added} added` : "",
    impact.removed ? `${impact.removed} removed` : "",
    impact.regrouped ? `${impact.regrouped} change groups` : "",
    impact.moved ? `${impact.moved} move` : "",
  ].filter(Boolean);
  return (
    <div className="impact" data-testid="impact">
      <p>
        <strong data-testid="impact-total">{impact.total === 0 ? "This changes no host." : `This changes ${plural(impact.total, "host")}`}</strong>
        {kinds.length > 0 && <span className="muted">: {kinds.join(", ")}.</span>}
      </p>
      {impact.hosts.length > 0 && (
        <details>
          <summary>{impact.truncated ? `Show the first ${impact.hosts.length}` : `Show ${impact.hosts.length === 1 ? "it" : `all ${impact.hosts.length}`}`}</summary>
          <ul className="ops" aria-label="Hosts this changes">
            {impact.hosts.map((h) => (
              <li key={h.host}>
                <code>{h.host}</code> {h.status}
                {hostChanges(h) && <span className="muted"> ({hostChanges(h)})</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

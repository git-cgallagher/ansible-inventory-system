import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, errorText } from "../api";
import { Notice, Provenance, StrategyBadge, Value } from "../components";
import { useAllNodes } from "../hooks";
import { href } from "../router";
import type { RenderResult } from "../types";
import { SYNTHETIC_KEYS } from "../types";
import { parseObject, same } from "../value";

/** `POST /render` for a real host or a mock host at a path, with overrides and facts. */
export function RenderView({ params }: { params: URLSearchParams }) {
  const { nodes, error: nodesError } = useAllNodes();
  const [mode, setMode] = useState<"host" | "path">(params.get("path") ? "path" : "host");
  const [host, setHost] = useState(params.get("host") ?? "");
  const [path, setPath] = useState(params.get("path") ?? "");
  const [overrides, setOverrides] = useState("");
  const [facts, setFacts] = useState("");
  const [revision, setRevision] = useState("");
  const [template, setTemplate] = useState(true);
  const [result, setResult] = useState<RenderResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const hosts = useMemo(() => nodes.filter((n) => n.kind === "host").sort((a, b) => a.name.localeCompare(b.name)), [nodes]);
  const paths = useMemo(() => nodes.filter((n) => n.kind !== "host").map((n) => n.path), [nodes]);

  const run = async (e?: FormEvent) => {
    e?.preventDefault();
    setError("");
    let body: Record<string, unknown>;
    try {
      body = {
        ...(mode === "host" ? { host } : { path }),
        overrides: parseObject(overrides, "Overrides"),
        facts: parseObject(facts, "Facts"),
        template,
      };
      if (revision.trim()) body.revision = Number(revision);
    } catch (err) {
      setError((err as Error).message);
      return;
    }
    if (mode === "host" ? !host : !path) {
      setError(mode === "host" ? "Pick a host." : "Type a path, e.g. all.commercial.prod.dc1.web");
      return;
    }
    setBusy(true);
    try {
      setResult(await api<RenderResult>("/api/v1/render", { method: "POST", body }));
      // Keep the URL shareable without re-triggering a render.
      history.replaceState(null, "", href("render", mode === "host" ? { host } : { path }));
    } catch (err) {
      setResult(null);
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (params.get("host") || params.get("path")) run();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const keys = result ? Object.keys(result.resolved).sort((a, b) => Number(SYNTHETIC_KEYS.has(a)) - Number(SYNTHETIC_KEYS.has(b)) || a.localeCompare(b)) : [];
  const errorCount = result?.errors ? Object.keys(result.errors).length : 0;

  return (
    <div className="page">
      <h1>Render</h1>
      <p className="muted">What a playbook sees for a real host, or for a mock host placed at any node.</p>
      {nodesError && <Notice kind="error">{nodesError}</Notice>}
      <form className="render-form" onSubmit={run}>
        <fieldset className="seg-field">
          <legend>Target</legend>
          <label>
            <input type="radio" name="mode" checked={mode === "host"} onChange={() => setMode("host")} /> Host
          </label>
          <label>
            <input type="radio" name="mode" checked={mode === "path"} onChange={() => setMode("path")} /> Mock host at a path
          </label>
        </fieldset>
        {mode === "host" ? (
          <label>
            Host
            <select value={host} onChange={(e) => setHost(e.target.value)} aria-label="Host">
              <option value="">— pick a host —</option>
              {hosts.map((h) => (
                <option key={h.id} value={h.name}>
                  {h.name} ({h.path})
                </option>
              ))}
            </select>
          </label>
        ) : (
          <label>
            Path
            <input value={path} onChange={(e) => setPath(e.target.value.trim())} list="node-paths" placeholder="all.commercial.prod.dc1.web" aria-label="Path" />
            <datalist id="node-paths">
              {paths.map((p) => (
                <option key={p} value={p} />
              ))}
            </datalist>
          </label>
        )}
        <div className="two">
          <label>
            Overrides <span className="muted">(YAML or JSON mapping, applied as host vars)</span>
            <textarea value={overrides} onChange={(e) => setOverrides(e.target.value)} rows={4} spellCheck={false} aria-label="Overrides" />
          </label>
          <label>
            Facts <span className="muted">(mock facts, with or without the ansible_ prefix)</span>
            <textarea value={facts} onChange={(e) => setFacts(e.target.value)} rows={4} spellCheck={false} aria-label="Facts" placeholder="default_ipv4: {address: 10.1.1.99}" />
          </label>
        </div>
        <div className="inline">
          <label>
            Revision <input type="number" min={1} value={revision} onChange={(e) => setRevision(e.target.value)} placeholder="latest" className="narrow" aria-label="Revision" />
          </label>
          <label>
            <input type="checkbox" checked={template} onChange={(e) => setTemplate(e.target.checked)} /> Evaluate Jinja
          </label>
          <button type="submit" disabled={busy}>
            {busy ? "Rendering…" : "Render"}
          </button>
        </div>
      </form>
      {error && <Notice kind="error">{error}</Notice>}
      {result && (
        <section className="render-result" aria-label="Render result">
          <p>
            <strong>{result.host ?? `mock host at ${result.path}`}</strong> · revision {result.revision}
            {errorCount > 0 && <span className="error"> · {errorCount} template error{errorCount > 1 ? "s" : ""}</span>}
          </p>
          <p className="chain" aria-label="Group chain">
            {result.chain.map((g, i) => (
              <span key={g}>
                {i > 0 && " › "}
                <code>{g}</code>
              </span>
            ))}
          </p>
          <table className="vars">
            <thead>
              <tr>
                <th>Key</th>
                <th>Resolved</th>
                {result.rendered && <th>Rendered (Jinja)</th>}
                <th>From</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => {
                const err = result.errors?.[k];
                const rendered = result.rendered?.[k];
                return (
                  <tr key={k} data-key={k} className={`${err ? "has-error" : ""}${SYNTHETIC_KEYS.has(k) ? " synthetic" : ""}`}>
                    <th scope="row">
                      <code>{k}</code> {!SYNTHETIC_KEYS.has(k) && <StrategyBadge name={k} />}
                    </th>
                    <td>
                      <Value value={result.resolved[k]} />
                    </td>
                    {result.rendered && (
                      <td>
                        {err ? (
                          <span className="error" title={err.type}>
                            {err.message}
                          </span>
                        ) : same(rendered, result.resolved[k]) ? (
                          <span className="muted">same</span>
                        ) : (
                          <Value value={rendered} />
                        )}
                      </td>
                    )}
                    <td>
                      <Provenance entries={result.provenance[k] ?? []} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

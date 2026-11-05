import { useEffect, useState, type FormEvent } from "react";
import { api, errorText } from "../api";
import { Notice, Spinner, StrategyBadge, Value } from "../components";
import { useApp } from "../context";
import { href, navigate } from "../router";
import type { VarHosts, VarNodes } from "../types";
import { canEdit } from "../types";
import { parseValue } from "../value";

const PAGE = 100;
const FIELDS = ["key", "value", "subtree", "revision"] as const;
type Fields = Record<(typeof FIELDS)[number], string>;

/** A value as JSON, so the string "550.90" and the number 550.9 look different. */
function Json({ value }: { value: unknown }) {
  if (value !== null && typeof value === "object") return <Value value={value} block />;
  return <code className="value">{JSON.stringify(value)}</code>;
}

/** The value box is YAML (JSON is YAML): `550.90` is a number, `"550.90"` a string, `nvidia-550`
 * a string, `[a, b]` a list. Sent to the server as JSON, which compares with the type. */
function queryValue(text: string): string | undefined {
  return text.trim() ? JSON.stringify(parseValue(text, "yaml")) : undefined;
}

/** `GET /vars` (where a key is set) and `GET /vars/hosts` (every host's resolved value).
 * The search lives in the URL, so "where set" links from the Variables and node pages work. */
export function SearchView({ params }: { params: URLSearchParams }) {
  const { session, defs } = useApp();
  const fromParams = (): Fields => Object.fromEntries(FIELDS.map((f) => [f, params.get(f) ?? ""])) as Fields;
  const [fields, setFields] = useState<Fields>(fromParams);
  const [nodes, setNodes] = useState<VarNodes | null>(null);
  const [hosts, setHosts] = useState<VarHosts | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const query = params.toString();

  const base = (f: Fields): Record<string, string | number | undefined> => ({
    key: f.key,
    value: queryValue(f.value),
    subtree: f.subtree || undefined,
    revision: f.revision || undefined,
  });

  useEffect(() => {
    const f = fromParams();
    setFields(f);
    setNodes(null);
    setHosts(null);
    setError("");
    if (!f.key) return;
    let q: Record<string, string | number | undefined>;
    try {
      q = base(f);
    } catch (e) {
      setError(`Value: ${(e as Error).message}`);
      return;
    }
    setBusy(true);
    (async () => {
      try {
        const n = await api<VarNodes>("/api/v1/vars", { query: { ...q, limit: PAGE } });
        // Hosts at the revision the nodes were read at, so both lists describe one state.
        const h = await api<VarHosts>("/api/v1/vars/hosts", { query: { ...q, revision: n.revision, limit: PAGE } });
        setNodes(n);
        setHosts(h);
      } catch (e) {
        setError(errorText(e));
      } finally {
        setBusy(false);
      }
    })();
  }, [query]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = (e: FormEvent) => {
    e.preventDefault();
    try {
      queryValue(fields.value);
    } catch (err) {
      setError(`Value: ${(err as Error).message}`);
      return;
    }
    navigate("search", fields);
  };

  const moreNodes = async () => {
    if (!nodes) return;
    try {
      const page = await api<VarNodes>("/api/v1/vars", { query: { ...base(fields), revision: nodes.revision, limit: PAGE, offset: nodes.nodes.length } });
      setNodes({ ...nodes, nodes: [...nodes.nodes, ...page.nodes] });
    } catch (e) {
      setError(errorText(e));
    }
  };

  const moreHosts = async () => {
    if (!hosts) return;
    try {
      const page = await api<VarHosts>("/api/v1/vars/hosts", { query: { ...base(fields), revision: hosts.revision, limit: PAGE, offset: hosts.hosts.length } });
      setHosts({ ...hosts, hosts: [...hosts.hosts, ...page.hosts] });
    } catch (e) {
      setError(errorText(e));
    }
  };

  const active = fromParams();
  const fold = nodes ? nodes.strategy !== "replace" : false;

  return (
    <div className="page">
      <div className="section-head">
        <h1>Search variables</h1>
        {nodes && <span className="rev">revision {nodes.revision}</span>}
      </div>
      <p className="muted">
        Where a variable is set, and which hosts end up with which value. A node matches on its own value; a host on its resolved value (for merge and append
        variables, the folded result).
      </p>
      <form className="filters" onSubmit={submit} aria-label="Variable search">
        <label>
          key
          <input value={fields.key} onChange={(e) => setFields({ ...fields, key: e.target.value.trim() })} list="search-vars" required aria-label="Variable key" placeholder="gpu_driver" />
          <datalist id="search-vars">
            {[...defs.keys()].map((d) => (
              <option key={d} value={d} />
            ))}
          </datalist>
        </label>
        <label>
          value
          <input value={fields.value} onChange={(e) => setFields({ ...fields, value: e.target.value })} aria-label="Value" placeholder='"550.90" (YAML/JSON)' title='YAML or JSON: 550.90 is a number, "550.90" a string' />
        </label>
        <label>
          subtree
          <input value={fields.subtree} onChange={(e) => setFields({ ...fields, subtree: e.target.value.trim() })} aria-label="Subtree" placeholder="all.commercial.prod" />
        </label>
        <label>
          revision
          <input value={fields.revision} onChange={(e) => setFields({ ...fields, revision: e.target.value.trim() })} aria-label="Revision" inputMode="numeric" placeholder="current" />
        </label>
        <div className="inline">
          <button type="submit">Search</button>
          <button type="button" className="secondary" onClick={() => navigate("search")}>
            Clear
          </button>
        </div>
      </form>
      {error && <Notice kind="error">{error}</Notice>}
      {busy && <Spinner />}

      {nodes && hosts && (
        <>
          <p>
            <code>{nodes.key}</code> <StrategyBadge name={nodes.key} />
            {!nodes.declared && <span className="muted"> (not declared: plain replace)</span>}
            {canEdit(session.role) && (
              <>
                {" · "}
                <a href={href("audit", { key: nodes.key, subtree: active.subtree })}>history</a>
              </>
            )}
          </p>

          <section aria-labelledby="set-title">
            <h2 id="set-title" data-testid="set-count">
              Set on {nodes.total} node{nodes.total === 1 ? "" : "s"}
            </h2>
            {nodes.total === 0 ? (
              <p className="muted">No node sets {nodes.key}{active.value ? " to that value" : ""}.</p>
            ) : (
              <table className="list setters">
                <thead>
                  <tr>
                    <th>Path</th>
                    <th>Kind</th>
                    <th>Value here</th>
                    {fold && <th>Emits (folded)</th>}
                  </tr>
                </thead>
                <tbody>
                  {nodes.nodes.map((n) => (
                    <tr key={n.path} data-path={n.path}>
                      <td>
                        <a href={href("tree", { path: n.path })}>{n.path}</a>
                        {n.above_subtree && <span className="muted"> (above the subtree)</span>}
                      </td>
                      <td>{n.kind}</td>
                      <td>
                        <Json value={n.value} />
                      </td>
                      {fold && (
                        <td>
                          <Json value={n.effective} />
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {nodes.nodes.length < nodes.total && (
              <button type="button" className="secondary" onClick={moreNodes}>
                More nodes ({nodes.total - nodes.nodes.length} left)
              </button>
            )}
          </section>

          <section aria-labelledby="hosts-title">
            <h2 id="hosts-title" data-testid="host-count">
              {active.value ? `${hosts.total} of ${hosts.with_key} hosts match` : `${hosts.with_key} hosts have it`}
              {hosts.without_key > 0 && <span className="muted"> · {hosts.without_key} without it</span>}
            </h2>
            {hosts.values.length > 0 && (
              <ul className="ops" aria-label="Values">
                {hosts.values.map((v) => (
                  <li key={JSON.stringify(v.value)}>
                    <a href={href("search", { ...active, value: JSON.stringify(v.value) })} title="Only the hosts with this value">
                      <Json value={v.value} />
                    </a>{" "}
                    <span className="muted">
                      on {v.hosts} host{v.hosts === 1 ? "" : "s"}
                    </span>
                  </li>
                ))}
                {hosts.values_truncated && <li className="muted">… and less common values</li>}
              </ul>
            )}
            {hosts.hosts.length > 0 && (
              <table className="list host-values">
                <thead>
                  <tr>
                    <th>Host</th>
                    <th>Value</th>
                    <th>From</th>
                  </tr>
                </thead>
                <tbody>
                  {hosts.hosts.map((h) => (
                    <tr key={h.path} data-host={h.host}>
                      <td>
                        <a href={href("tree", { path: h.path })} title={h.path}>
                          {h.host}
                        </a>
                      </td>
                      <td>
                        <Json value={h.value} />
                      </td>
                      <td>
                        {(h.sources ?? [h.from]).map((p, i) => (
                          <span key={p}>
                            {i > 0 && <span className="muted"> + </span>}
                            <a href={href("tree", { path: p })}>{p}</a>
                          </span>
                        ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {hosts.hosts.length < hosts.total && (
              <button type="button" className="secondary" onClick={moreHosts}>
                More hosts ({hosts.total - hosts.hosts.length} left)
              </button>
            )}
          </section>
        </>
      )}
    </div>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, errorText } from "../api";
import { Notice, Spinner } from "../components";
import { navigate } from "../router";
import type { InvNode } from "../types";
import { NodeDetail } from "./NodeDetail";

const expandable = (n: InvNode) => n.kind !== "host";

/** Lazy tree (children fetched per node on expand) with WAI-ARIA tree keyboard support:
 * Up/Down move, Right expands or enters, Left collapses or goes to the parent,
 * Home/End jump, Enter/Space open the node. The selection lives in the URL by path. */
export function TreeView({ path }: { path: string | null }) {
  const selectedPath = path || "all";
  const [nodes, setNodes] = useState<Map<number, InvNode>>(new Map());
  const [kids, setKids] = useState<Map<number, number[]>>(new Map());
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState<Set<number>>(new Set());
  const [rootId, setRootId] = useState<number | null>(null);
  const [focusId, setFocusId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const nodesRef = useRef(nodes);
  const kidsRef = useRef(kids);
  const itemRefs = useRef(new Map<number, HTMLLIElement>());
  const moveFocus = useRef(false);
  nodesRef.current = nodes;
  kidsRef.current = kids;

  const addNodes = useCallback((list: InvNode[]) => {
    const next = new Map(nodesRef.current);
    for (const n of list) next.set(n.id, n);
    nodesRef.current = next;
    setNodes(next);
  }, []);

  const loadChildren = useCallback(
    async (id: number) => {
      if (kidsRef.current.has(id)) return;
      setLoading((s) => new Set(s).add(id));
      try {
        const rows = await api<InvNode[]>(`/api/v1/nodes/${id}/children`);
        addNodes(rows);
        const next = new Map(kidsRef.current).set(
          id,
          rows.map((r) => r.id),
        );
        kidsRef.current = next;
        setKids(next);
      } finally {
        setLoading((s) => {
          const n = new Set(s);
          n.delete(id);
          return n;
        });
      }
    },
    [addNodes],
  );

  const setOpen = useCallback(
    (id: number, open: boolean) => {
      setExpanded((s) => {
        const n = new Set(s);
        if (open) n.add(id);
        else n.delete(id);
        return n;
      });
      if (open) loadChildren(id).catch((e) => setError(errorText(e)));
    },
    [loadChildren],
  );

  const findByPath = (p: string) => [...nodesRef.current.values()].find((n) => n.path === p);

  /** Load and expand every ancestor of `target`, so deep links and provenance links work. */
  const reveal = useCallback(
    async (target: string) => {
      const parts = target.split(".");
      let node: InvNode | undefined;
      for (let i = 1; i <= parts.length; i++) {
        const p = parts.slice(0, i).join(".");
        node = findByPath(p);
        if (!node) {
          node = await api<InvNode>(`/api/v1/nodes/${encodeURIComponent(p)}`);
          addNodes([node]);
        }
        if (i === 1) setRootId(node.id);
        if (i < parts.length) {
          const id = node.id; // not `node` itself: the updater runs after the loop moved on
          await loadChildren(id);
          setExpanded((s) => new Set(s).add(id));
        }
      }
      if (node) setFocusId(node.id);
      return node;
    },
    [addNodes, loadChildren], // eslint-disable-line react-hooks/exhaustive-deps
  );

  useEffect(() => {
    setError("");
    reveal(selectedPath)
      .then((n) => {
        // Open the root on first load so the first level is visible.
        if (n && n.kind === "all") setOpen(n.id, true);
      })
      .catch((e) => setError(`${selectedPath}: ${errorText(e)}`));
  }, [selectedPath, reveal, setOpen]);

  useEffect(() => {
    if (moveFocus.current && focusId !== null) {
      itemRefs.current.get(focusId)?.focus();
      moveFocus.current = false;
    }
  }, [focusId]);

  const visible = useMemo(() => {
    const out: number[] = [];
    const walk = (id: number) => {
      out.push(id);
      if (expanded.has(id)) for (const k of kids.get(id) ?? []) walk(k);
    };
    if (rootId !== null && nodes.has(rootId)) walk(rootId);
    return out;
  }, [rootId, nodes, kids, expanded]);

  const selected = useMemo(() => [...nodes.values()].find((n) => n.path === selectedPath) ?? null, [nodes, selectedPath]);

  const onKeyDown = (e: KeyboardEvent) => {
    if (focusId === null) return;
    const i = visible.indexOf(focusId);
    const n = nodes.get(focusId);
    if (i < 0 || !n) return;
    const go = (id: number | undefined) => {
      if (id === undefined) return;
      moveFocus.current = true;
      setFocusId(id);
    };
    switch (e.key) {
      case "ArrowDown":
        go(visible[i + 1]);
        break;
      case "ArrowUp":
        go(visible[i - 1]);
        break;
      case "Home":
        go(visible[0]);
        break;
      case "End":
        go(visible[visible.length - 1]);
        break;
      case "ArrowRight":
        if (!expandable(n)) break;
        if (!expanded.has(n.id)) setOpen(n.id, true);
        else go((kids.get(n.id) ?? [])[0]);
        break;
      case "ArrowLeft":
        if (expandable(n) && expanded.has(n.id)) setOpen(n.id, false);
        else if (n.parent_id !== null && nodes.has(n.parent_id)) go(n.parent_id);
        break;
      case "Enter":
      case " ":
        navigate("tree", { path: n.path });
        break;
      default:
        return;
    }
    e.preventDefault();
  };

  const renderItem = (id: number): ReactNode => {
    const n = nodes.get(id);
    if (!n) return null;
    const open = expanded.has(id);
    const children = kids.get(id);
    return (
      <li
        key={id}
        role="treeitem"
        aria-level={n.depth + 1}
        aria-expanded={expandable(n) ? open : undefined}
        aria-selected={n.path === selectedPath}
        aria-label={n.name}
        tabIndex={id === focusId ? 0 : -1}
        ref={(el) => {
          if (el) itemRefs.current.set(id, el);
          else itemRefs.current.delete(id);
        }}
        onFocus={(e) => {
          if (e.target === e.currentTarget) setFocusId(id);
        }}
      >
        <div
          className={`row${n.path === selectedPath ? " selected" : ""}`}
          style={{ paddingLeft: `${n.depth * 1.1 + 0.3}rem` }}
          onClick={() => {
            setFocusId(id);
            navigate("tree", { path: n.path });
          }}
        >
          <span
            className="twisty"
            aria-hidden="true"
            onClick={(e) => {
              if (!expandable(n)) return;
              e.stopPropagation();
              setOpen(id, !open);
            }}
          >
            {expandable(n) ? (open ? "▾" : "▸") : "·"}
          </span>
          <span className="name">{n.name}</span>
          <span className={`kind kind-${n.kind}`}>{n.kind}</span>
          {n.inventory_name && <span className="muted"> as {n.inventory_name}</span>}
        </div>
        {open && (
          <ul role="group">
            {loading.has(id) && !children && (
              <li role="none" className="muted row" style={{ paddingLeft: `${(n.depth + 1) * 1.1 + 1.3}rem` }}>
                Loading…
              </li>
            )}
            {children?.length === 0 && (
              <li role="none" className="muted row" style={{ paddingLeft: `${(n.depth + 1) * 1.1 + 1.3}rem` }}>
                no children
              </li>
            )}
            {children?.map(renderItem)}
          </ul>
        )}
      </li>
    );
  };

  return (
    <div className="split">
      <aside className="tree-pane">
        {rootId === null && !error && <Spinner />}
        {rootId !== null && (
          <ul role="tree" aria-label="Inventory tree" className="tree" onKeyDown={onKeyDown}>
            {renderItem(rootId)}
          </ul>
        )}
      </aside>
      <section className="detail-pane">
        {error && <Notice kind="error">{error}</Notice>}
        {selected && <NodeDetail key={selected.id} node={selected} />}
      </section>
    </div>
  );
}

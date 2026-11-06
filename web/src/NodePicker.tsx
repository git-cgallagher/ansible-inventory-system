import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { api, errorText } from "./api";
import type { InvNode } from "./types";

/** Kinds a mock host or a new parent can sit under: everything but hosts. */
export const NON_HOST_KINDS: readonly InvNode["kind"][] = ["all", "business", "tier", "datacenter", "group"];

const SHOWN = 20;
const DEBOUNCE_MS = 150;

/** A node typeahead: an ARIA 1.2 combobox (editable, list autocomplete) over the bounded
 * lookup `GET /api/v1/nodes?q=…&kind=…&limit=21` (ais.nodesearch). It never loads the whole
 * node list (AGENTS.md): each pause in typing asks for at most 21 nodes and shows 20, with
 * the path under each name. The text stays free: a value that came from a deep link, or one
 * typed in full, is used as it is and costs no lookup until the user types or presses Down.
 *
 * Keys (the APG combobox pattern): Down/Up open the list and move through it (wrapping),
 * Enter picks the highlighted node (with nothing highlighted it submits the form as usual),
 * Escape closes the list (and only the list), Tab leaves. A click picks with the mouse. */
export function NodePicker(props: {
  /** Accessible name of the text box, e.g. "Host". */
  label: string;
  value: string;
  /** Every change of the text: typing and picking. */
  onChange: (text: string) => void;
  /** A node was picked from the list (after `onChange` got its name or path). */
  onPick?: (node: InvNode) => void;
  kinds?: readonly InvNode["kind"][];
  /** What a pick puts in the box: the node's `name` (hosts) or its `path` (default). */
  pick?: "name" | "path";
  subtree?: string;
  placeholder?: string;
  required?: boolean;
  size?: number;
}) {
  const { label, value, onChange, onPick, kinds, subtree, placeholder, required, size } = props;
  const pick = props.pick ?? "path";
  const id = useId();
  const listId = `${id}-list`;
  const optionId = (i: number) => `${id}-opt-${i}`;
  const [open, setOpen] = useState(false);
  const [results, setResults] = useState<InvNode[]>([]);
  const [more, setMore] = useState(false);
  const [active, setActive] = useState(-1);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);
  const timer = useRef<number | undefined>(undefined);
  const listRef = useRef<HTMLUListElement>(null);

  useEffect(
    () => () => {
      window.clearTimeout(timer.current);
      seq.current++;
    },
    [],
  );

  useEffect(() => {
    if (active >= 0) listRef.current?.querySelector(`#${CSS.escape(optionId(active))}`)?.scrollIntoView({ block: "nearest" });
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Ask the server for `text` after `delay` ms; only the newest answer is shown. */
  const lookup = (text: string, delay: number) => {
    window.clearTimeout(timer.current);
    const mine = ++seq.current;
    setBusy(true);
    timer.current = window.setTimeout(async () => {
      try {
        const rows = await api<InvNode[]>("/api/v1/nodes", { query: { q: text.trim(), kind: kinds ?? [], subtree, limit: SHOWN + 1 } });
        if (mine !== seq.current) return;
        const shown = rows.slice(0, SHOWN);
        setResults(shown);
        setMore(rows.length > SHOWN);
        setActive(-1);
        setOpen(true);
        setStatus(shown.length === 0 ? "No matches." : rows.length > SHOWN ? `More than ${SHOWN} matches; keep typing to narrow them.` : `${shown.length} match${shown.length > 1 ? "es" : ""}.`);
      } catch (e) {
        if (mine !== seq.current) return;
        setResults([]);
        setMore(false);
        setOpen(true);
        setStatus(`Lookup failed: ${errorText(e)}`);
      } finally {
        if (mine === seq.current) setBusy(false);
      }
    }, delay);
  };

  const close = () => {
    window.clearTimeout(timer.current);
    seq.current++;
    setBusy(false);
    setOpen(false);
    setActive(-1);
  };

  const choose = (n: InvNode) => {
    close();
    onChange(pick === "name" ? n.name : n.path);
    onPick?.(n);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    switch (e.key) {
      case "ArrowDown":
      case "ArrowUp": {
        e.preventDefault();
        if (!open) {
          lookup(value, 0);
          return;
        }
        if (results.length === 0) return;
        const step = e.key === "ArrowDown" ? 1 : -1;
        setActive((a) => (a < 0 ? (step > 0 ? 0 : results.length - 1) : (a + step + results.length) % results.length));
        return;
      }
      case "Enter":
        if (open && active >= 0 && results[active]) {
          e.preventDefault();
          choose(results[active]);
        } else if (open) {
          close();
        }
        return;
      case "Escape":
        if (open) {
          e.preventDefault();
          e.stopPropagation(); // close the list, not the form around it
          close();
        }
        return;
      case "Tab":
        if (open) close();
        return;
    }
  };

  const shown = open && (results.length > 0 || status !== "");
  return (
    <div className="combo">
      <input
        role="combobox"
        aria-label={label}
        aria-autocomplete="list"
        aria-expanded={shown}
        aria-controls={listId}
        aria-activedescendant={open && active >= 0 ? optionId(active) : undefined}
        aria-busy={busy || undefined}
        autoComplete="off"
        spellCheck={false}
        value={value}
        placeholder={placeholder}
        required={required}
        size={size}
        onChange={(e) => {
          onChange(e.target.value);
          lookup(e.target.value, DEBOUNCE_MS);
        }}
        onKeyDown={onKeyDown}
        onBlur={close}
      />
      <ul id={listId} ref={listRef} role="listbox" aria-label={`${label} suggestions`} className="combo-list" hidden={!shown}>
        {results.map((n, i) => (
          <li
            key={n.id}
            id={optionId(i)}
            role="option"
            aria-selected={i === active}
            aria-label={pick === "name" ? n.name : n.path}
            className={i === active ? "active" : undefined}
            // mousedown would blur the input (and close the list) before the click lands
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => choose(n)}
            onMouseMove={() => i !== active && setActive(i)}
          >
            <span className="combo-name">
              {n.name} <span className={`kind kind-${n.kind}`}>{n.kind}</span>
            </span>
            <span className="combo-path">{n.path}</span>
          </li>
        ))}
        {(results.length === 0 || more) && status && (
          <li role="presentation" className="combo-note muted">
            {status}
          </li>
        )}
      </ul>
      {/* aria-live, not role="status": pages already use that role for their own notices */}
      <span aria-live="polite" className="visually-hidden">
        {open ? status : ""}
      </span>
    </div>
  );
}

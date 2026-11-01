import { useEffect, useState } from "react";

/** Hash routes (`#/tree?path=all.commercial`) so the server serves one index.html and
 * every API route stays untouched. Paths, not ids, go in URLs: a move changes ids. */
export interface Route {
  view: string;
  params: URLSearchParams;
}

function parse(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [view, query = ""] = raw.split("?", 2);
  return { view: view || "tree", params: new URLSearchParams(query) };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const onChange = () => setRoute(parse());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function href(view: string, params: Record<string, string | number | null | undefined> = {}): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  const qs = q.toString();
  return `#/${view}${qs ? `?${qs}` : ""}`;
}

export function navigate(view: string, params: Record<string, string | number | null | undefined> = {}): void {
  window.location.hash = href(view, params);
}

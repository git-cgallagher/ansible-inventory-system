import { useEffect, useState } from "react";
import { api, errorText } from "./api";
import type { InvNode } from "./types";

/** Every live node (one `GET /nodes`), for host pickers and path suggestions. */
export function useAllNodes(): { nodes: InvNode[]; error: string } {
  const [nodes, setNodes] = useState<InvNode[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api<InvNode[]>("/api/v1/nodes")
      .then(setNodes)
      .catch((e) => setError(errorText(e)));
  }, []);
  return { nodes, error };
}

export function when(ts: string | null | undefined): string {
  if (!ts) return "";
  // The API returns naive UTC timestamps from SQLite and aware ones from PostgreSQL.
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : `${ts}Z`);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString();
}

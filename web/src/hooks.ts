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

/** The server's current revision (0 for an empty inventory). */
export async function latestRevision(): Promise<number> {
  const rows = await api<{ id: number }[]>("/api/v1/revisions", { query: { limit: 1 } });
  return rows[0]?.id ?? 0;
}

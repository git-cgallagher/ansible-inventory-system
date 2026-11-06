import { api } from "./api";

// No hook loads the whole node list (`GET /api/v1/nodes` without `q`/`limit`): at 100k hosts
// that is ~21 MB. Pickers use NodePicker, the bounded lookup (AGENTS.md, issue #27).

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

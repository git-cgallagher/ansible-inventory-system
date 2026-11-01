export type Role = "viewer" | "editor" | "admin";
export type Strategy = "replace" | "merge" | "append" | "unique_append";

export interface Session {
  token: string;
  email: string;
  role: Role;
  expires_at?: string | null;
}

export interface InvNode {
  id: number;
  parent_id: number | null;
  kind: "all" | "business" | "tier" | "datacenter" | "group" | "host";
  name: string;
  inventory_name: string | null;
  path: string;
  depth: number;
  meta: Record<string, unknown>;
  created_rev: number | null;
  deleted_rev: number | null;
  vars?: Record<string, unknown>;
}

export interface Definition {
  name: string;
  merge_strategy: Strategy;
  description: string | null;
  json_schema: unknown;
}

export interface Contribution {
  path: string | null;
  group: string;
  strategy: Strategy;
  value: unknown;
}

export interface RenderResult {
  revision: number;
  host: string | null;
  path: string;
  chain: string[];
  resolved: Record<string, unknown>;
  provenance: Record<string, Contribution[]>;
  rendered?: Record<string, unknown>;
  errors?: Record<string, { type: string; message: string }>;
}

export interface WriteResult {
  revision: number;
  changed: boolean;
}

export interface RevisionRow {
  id: number;
  actor: string;
  source: string;
  ts: string;
  message: string | null;
}

export interface SnapshotRow {
  revision: number;
  resolved_hash: string;
  created_at: string;
}

export interface DiffResult {
  host: string;
  from: number;
  to: number;
  identical: boolean;
  added: Record<string, unknown>;
  removed: Record<string, unknown>;
  changed: Record<string, { from: unknown; to: unknown }>;
}

export interface AuditRow {
  id: number;
  ts: string;
  actor: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  before: unknown;
  after: unknown;
  source: string | null;
  request_id: string | null;
  revision: number | null;
}

/** Inventory keys the server adds itself; never shown as a variable. */
export const SYNTHETIC_KEYS = new Set(["inventory_revision"]);

export function canEdit(role: Role | undefined): boolean {
  return role === "editor" || role === "admin";
}

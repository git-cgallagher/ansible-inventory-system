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

/** One host a write would affect (`impact.hosts`, server issue 25). */
export interface ImpactHost {
  host: string;
  path: string;
  status: "changed" | "added" | "removed";
  added?: Record<string, unknown>;
  removed?: Record<string, unknown>;
  changed?: Record<string, { from: unknown; to: unknown }>;
  joined?: string[];
  left?: string[];
  moved_from?: string;
}

/** What a dry run says a write would do to the hosts. Counts are exact; the lists are capped. */
export interface Impact {
  total: number;
  vars_changed: number;
  added: number;
  removed: number;
  regrouped: number;
  moved: number;
  hosts: ImpactHost[];
  groups: { group: string; joined: number; left: number }[];
  groups_total: number;
  limit: number;
  truncated: boolean;
}

export interface WriteResult {
  revision: number;
  changed: boolean;
  /** True for a preview (`?dry_run=true`): nothing was written, `revision` is the one it is based on. */
  dry_run?: boolean;
  impact?: Impact;
  /** Undeclared keys the write stored that look like typos of known ones (server issue 24). */
  warnings?: KeyWarning[];
  warnings_total?: number;
}

export interface KeyWarning {
  key: string;
  path: string;
  did_you_mean: string;
  declared: boolean;
  message: string;
}

export interface RevisionRow {
  id: number;
  actor: string;
  source: string;
  ts: string;
  message: string | null;
  /** The writer's change id (`X-AIS-Change`), if it gave one. */
  change: string | null;
}

/** One inverse operation of a revert: the audit row it records (or would, in a dry run). */
export interface RevertOperation {
  reverts: number;
  action: string;
  target_type: string;
  target_id: string;
  summary: string;
  before: unknown;
  after: unknown;
}

export interface RevertConflict {
  revision: number;
  action: string;
  target: string;
  actor: string;
  reverting: number;
  on: string;
  already_reverted: boolean;
}

/** `POST /revisions/{id}/revert` and `/changes/{change}/revert` (dry run: `hosts` too). */
export interface RevertResult {
  revision: number;
  dry_run: boolean;
  reverted: number[];
  operations: RevertOperation[];
  forced_over: RevertConflict[];
  notes: string[];
  hosts_changed?: number;
  hosts?: { host: string; status: string; added?: Record<string, unknown>; removed?: Record<string, unknown>; changed?: Record<string, unknown> }[];
}

/** A revision where a host's resolved variables changed (`GET /hosts/{name}/snapshots`). */
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
  change: string | null;
  key: string | null; // the variable a node_variable / variable_definition row is about
}

/** `GET /vars` and `/vars/hosts`: what every variable search answers with. */
interface VarSearchHead {
  revision: number;
  key: string;
  strategy: Strategy;
  declared: boolean;
  subtree: string | null;
  value?: unknown;
  total: number;
  limit: number;
  offset: number;
}

export interface SettingNode {
  id: number;
  path: string;
  kind: InvNode["kind"];
  value: unknown; // the node's own value
  effective?: unknown; // fold strategies: the cumulative value the node emits
  above_subtree?: boolean;
}

export interface VarNodes extends VarSearchHead {
  nodes: SettingNode[];
}

export interface HostValue {
  host: string;
  path: string;
  value: unknown; // resolved
  from: string; // the deepest node that sets it
  sources?: string[]; // fold strategies: every contributor, root first
}

export interface VarHosts extends VarSearchHead {
  with_key: number;
  without_key: number;
  values: { value: unknown; hosts: number }[];
  values_truncated: boolean;
  hosts: HostValue[];
}

/** Inventory keys the server adds itself; never shown as a variable. */
export const SYNTHETIC_KEYS = new Set(["inventory_revision"]);

export function canEdit(role: Role | undefined): boolean {
  return role === "editor" || role === "admin";
}

export interface TagRow {
  name: string;
  description: string | null;
  members: string[];
}

export interface TokenRow {
  id: number;
  user_id: number;
  name: string;
  prefix: string;
  kind: "personal" | "session";
  role: Role | null;
  expires_at: string | null;
  last_used: string | null;
  revoked_at: string | null;
  created_at: string;
}

/** `DELETE /nodes/{id}` (and its `dry_run`): what goes, counted. */
export interface DeleteResult extends WriteResult {
  dry_run: boolean;
  deleted: string[];
  nodes: number;
  hosts: number;
}

export interface MoveResult extends WriteResult {
  node: InvNode;
  moved: { old_id: number; id: number; from: string; to: string }[];
}

export const STRATEGIES: Strategy[] = ["replace", "merge", "append", "unique_append"];
export const CHILD_KINDS: InvNode["kind"][] = ["business", "tier", "datacenter", "group", "host"];

/** The latest revision, for pages whose values have no revision of their own (definitions,
 * tags): writes from them carry the revision read together with the values on screen. */
export interface Revisioned<T> {
  revision: number;
  rows: T;
}

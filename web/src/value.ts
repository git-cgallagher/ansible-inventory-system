import YAML from "yaml";

export type Format = "json" | "yaml";

export function formatValue(value: unknown, format: Format): string {
  if (format === "json") return JSON.stringify(value, null, 2);
  return YAML.stringify(value, { indent: 2, lineWidth: 0 }).replace(/\n$/, "");
}

/** Parse editor text; throws an Error with a readable message. */
export function parseValue(text: string, format: Format): unknown {
  if (!text.trim()) throw new Error("empty value: write null to store an explicit null");
  if (format === "json") {
    try {
      return JSON.parse(text);
    } catch (e) {
      throw new Error(`JSON: ${(e as Error).message}`);
    }
  }
  const doc = YAML.parseDocument(text);
  if (doc.errors.length) throw new Error(`YAML: ${doc.errors[0].message}`);
  return doc.toJS();
}

/** Optional object input (overrides, facts): blank means none. JSON is valid YAML. */
export function parseObject(text: string, label: string): Record<string, unknown> | undefined {
  if (!text.trim()) return undefined;
  const value = parseValue(text, "yaml");
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be a mapping`);
  return value as Record<string, unknown>;
}

/** Compact one-line rendering for tables. */
export function short(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

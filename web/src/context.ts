import { createContext, useContext } from "react";
import type { Definition, Session } from "./types";

export interface AppState {
  session: Session;
  defs: Map<string, Definition>;
  /** Re-read `/variables` after a definition changed (badges and hints use it). */
  reloadDefs: () => void;
}

export const AppContext = createContext<AppState | null>(null);

export function useApp(): AppState {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("AppContext missing");
  return ctx;
}

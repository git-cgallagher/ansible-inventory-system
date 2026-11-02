import { useCallback, useEffect, useState } from "react";
import { api, currentSession, errorText, setSession, setUnauthorizedHandler } from "./api";
import { AppContext } from "./context";
import { href, useRoute } from "./router";
import type { Definition, Role, Session } from "./types";
import { canEdit } from "./types";
import { Login } from "./views/Login";
import { TreeView } from "./views/Tree";
import { RenderView } from "./views/Render";
import { DiffView } from "./views/Diff";
import { RevisionsView } from "./views/Revisions";
import { AuditView } from "./views/Audit";
import { VariablesView } from "./views/Variables";
import { TagsView } from "./views/Tags";
import { TokensView } from "./views/Tokens";
import { Notice } from "./components";

const VIEWS: { id: string; label: string; editorOnly?: boolean }[] = [
  { id: "tree", label: "Tree" },
  { id: "render", label: "Render" },
  { id: "variables", label: "Variables" },
  { id: "tags", label: "Tags" },
  { id: "diff", label: "Diff" },
  { id: "revisions", label: "Revisions" },
  { id: "audit", label: "Audit", editorOnly: true },
  { id: "tokens", label: "Tokens" },
];

export default function App() {
  const [session, setSess] = useState<Session | null>(currentSession);
  const [defs, setDefs] = useState<Map<string, Definition>>(new Map());
  const [error, setError] = useState("");
  const route = useRoute();

  const reloadDefs = useCallback(() => {
    api<Definition[]>("/api/v1/variables")
      .then((rows) => setDefs(new Map(rows.map((d) => [d.name, d]))))
      .catch((e) => setError(errorText(e)));
  }, []);

  useEffect(() => setUnauthorizedHandler(() => setSess(null)), []);

  const signIn = useCallback((s: Session) => {
    setSession(s);
    setSess(s);
  }, []);

  useEffect(() => {
    if (!session) return;
    // The token may carry a lower role than the user (a capped token); trust /auth/me.
    api<{ role: Role; user: { email: string } }>("/api/v1/auth/me")
      .then((me) => {
        if (me.role !== session.role || me.user.email !== session.email) signIn({ ...session, role: me.role, email: me.user.email });
      })
      .catch((e) => setError(errorText(e)));
    reloadDefs();
  }, [session?.token]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!session) return <Login onLogin={signIn} />;

  const forget = () => {
    setSession(null);
    setSess(null);
  };

  const signOut = async () => {
    try {
      await api("/api/v1/auth/logout", { method: "POST" });
    } catch {
      /* already expired: forget it locally anyway */
    }
    setSession(null);
    setSess(null);
  };

  const views = VIEWS.filter((v) => !v.editorOnly || canEdit(session.role));
  let body;
  switch (route.view) {
    case "render":
      body = <RenderView params={route.params} />;
      break;
    case "diff":
      body = <DiffView params={route.params} />;
      break;
    case "revisions":
      body = <RevisionsView />;
      break;
    case "audit":
      body = <AuditView params={route.params} />;
      break;
    case "variables":
      body = <VariablesView />;
      break;
    case "tags":
      body = <TagsView />;
      break;
    case "tokens":
      body = <TokensView onSignedOut={forget} />;
      break;
    default:
      body = <TreeView path={route.params.get("path")} />;
  }

  return (
    <AppContext.Provider value={{ session, defs, reloadDefs }}>
      <header className="topbar">
        <a className="brand" href={href("tree")}>
          Ansible Inventory
        </a>
        <nav aria-label="Views">
          {views.map((v) => (
            <a key={v.id} href={href(v.id)} aria-current={route.view === v.id || (v.id === "tree" && !VIEWS.some((x) => x.id === route.view)) ? "page" : undefined}>
              {v.label}
            </a>
          ))}
        </nav>
        <span className="who">
          {session.email} <span className="muted">({session.role})</span>
          <button type="button" className="link" onClick={signOut}>
            Sign out
          </button>
        </span>
      </header>
      {error && (
        <Notice kind="error" onClose={() => setError("")}>
          {error}
        </Notice>
      )}
      <main>{body}</main>
    </AppContext.Provider>
  );
}

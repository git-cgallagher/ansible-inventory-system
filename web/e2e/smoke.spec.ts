import { expect, test } from "@playwright/test";

// Placeholder credentials created by e2e/serve.sh for a throwaway SQLite database.
const EMAIL = process.env.E2E_EMAIL ?? "e2e@example.com";
const PASSWORD = process.env.E2E_PASSWORD ?? "example-not-secret";

test("login, keyboard tree, inherited vars, locked edit with a conflict, render, diff, audit", async ({ page }) => {
  // --- login
  await page.goto("/");
  await page.getByLabel("E-mail").fill(EMAIL);
  await page.getByLabel("Password").fill("wrong-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("Wrong e-mail or password");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // --- tree, by keyboard only: commercial → expand → lab → prod → open
  const tree = page.getByRole("tree", { name: "Inventory tree" });
  const commercial = tree.getByRole("treeitem", { name: "commercial", exact: true });
  await expect(commercial).toBeVisible();
  await commercial.focus();
  await page.keyboard.press("ArrowRight");
  await expect(tree.getByRole("treeitem", { name: "prod", exact: true })).toBeVisible();
  await page.keyboard.press("ArrowRight");
  await expect(tree.getByRole("treeitem", { name: "lab", exact: true })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "prod" })).toBeVisible();
  await expect(page).toHaveURL(/path=all\.commercial\.prod/);

  // --- own vs inherited, with the winning node named
  const own = page.locator("table.vars:not(.inherited)");
  const inherited = page.locator("table.vars.inherited");
  await expect(own.locator('tr[data-key="log_level"]')).toContainText("warning");
  const compliance = inherited.locator('tr[data-key="compliance_profile"]');
  await expect(compliance).toContainText("cis-level1");
  await expect(compliance.getByRole("link", { name: "all.commercial", exact: true })).toBeVisible();
  await expect(inherited.locator('tr[data-key="packages"]')).toContainText("folded from");
  await expect(inherited.locator('tr[data-key="packages"]')).toContainText("unique append");
  await expect(page.locator(".rev")).toHaveText("revision 1");

  // --- edit with If-Match: revision 1 → 2
  await page.getByRole("button", { name: "Edit log_level" }).click();
  await page.getByRole("textbox", { name: "Value" }).fill('"error"');
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status")).toContainText("revision 2");
  await expect(own.locator('tr[data-key="log_level"]')).toContainText("error");

  // --- someone else writes while the editor is open → 409 → reload, text kept, retry
  await page.getByRole("button", { name: "Edit log_level" }).click();
  await page.getByRole("textbox", { name: "Value" }).fill('"warning"');
  await page.evaluate(async () => {
    const s = JSON.parse(sessionStorage.getItem("ais.session")!);
    const auth = { Authorization: `Bearer ${s.token}` };
    const node = await (await fetch("/api/v1/nodes/all.commercial", { headers: auth })).json();
    const r = await fetch(`/api/v1/nodes/${node.id}/vars/e2e_marker`, {
      method: "PUT",
      headers: { ...auth, "If-Match": '"2"', "Content-Type": "application/json" },
      body: JSON.stringify({ value: "bumped" }),
    });
    if (!r.ok) throw new Error(`concurrent write failed: ${r.status}`);
  });
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("alert")).toContainText("server is at 3");
  await expect(page.locator(".rev")).toHaveText("revision 3");
  await expect(inherited.locator('tr[data-key="e2e_marker"]')).toContainText("bumped");
  await expect(page.getByRole("textbox", { name: "Value" })).toHaveValue('"warning"');
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status")).toContainText("revision 4");
  await expect(own.locator('tr[data-key="log_level"]')).toContainText("warning");

  // --- render a real host with overrides and facts
  await page.getByRole("navigation", { name: "Views" }).getByRole("link", { name: "Render" }).click();
  await page.getByRole("combobox", { name: "Host" }).selectOption("web02");
  await page.getByRole("textbox", { name: "Facts" }).fill("default_ipv4: {address: 10.1.1.12}");
  await page.getByRole("textbox", { name: "Overrides" }).fill("greeting: '{{ inventory_hostname }} in {{ site_code }}'\nbroken: '{{ nope }}'");
  await page.getByRole("button", { name: "Render", exact: true }).click();
  const result = page.getByRole("region", { name: "Render result" });
  const packages = result.locator('tr[data-key="packages"]');
  await expect(packages).toContainText("tcpdump");
  await expect(packages.getByRole("link", { name: "all.commercial.prod.dc1.web", exact: true })).toBeVisible();
  await expect(result.locator('tr[data-key="greeting"]')).toContainText("web02 in DC1");
  await expect(result.locator('tr[data-key="broken"]')).toHaveClass(/has-error/);

  // --- and a mock host at a path
  await page.getByLabel("Mock host at a path").check();
  await page.getByRole("combobox", { name: "Path" }).fill("all.commercial.lab.dc1.web");
  await page.getByRole("textbox", { name: "Overrides" }).fill("");
  await page.getByRole("button", { name: "Render", exact: true }).click();
  await expect(result).toContainText("mock host at all.commercial.lab.dc1.web");
  await expect(result.locator('tr[data-key="http_port"]')).toContainText("8081");

  // --- diff: the concurrent write shows up for web01 between revisions 1 and 4
  await page.goto("/#/diff?host=web01&from=1&to=4");
  const diff = page.getByRole("region", { name: "Diff result" });
  await expect(diff.locator("tr.added")).toContainText("e2e_marker");

  // --- audit: the UI's writes are recorded with source "ui"
  await page.goto("/#/audit?action=variable.set");
  await expect(page.locator("table.audit tbody tr")).toHaveCount(3);
  await expect(page.locator("table.audit tbody tr").first()).toContainText("ui");

  // --- a deep link (and every provenance link) opens the tree down to the node
  await page.goto("/#/tree?path=all.federal.prod.dc2.web.edge.fed-edge01");
  const edge = page.getByRole("treeitem", { name: "fed-edge01", exact: true });
  await expect(edge).toBeVisible();
  await expect(edge).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { level: 1, name: "fed-edge01" })).toBeVisible();

});

// Runs after the first test on the same server (one worker, file order): it asserts no revision
// numbers, so it does not care what the first one wrote.
test("structure, definitions, tags and tokens from the UI; history follows a move", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("E-mail").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // --- add a child under dc1, give it a variable
  await page.goto("/#/tree?path=all.commercial.prod.dc1");
  await expect(page.getByRole("heading", { level: 1, name: "dc1" })).toBeVisible();
  await page.getByRole("button", { name: "Add child" }).click();
  await page.getByRole("textbox", { name: "Child name" }).fill("e2e_cache");
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "e2e_cache" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("Created all.commercial.prod.dc1.e2e_cache (group)");
  await expect(page.getByRole("treeitem", { name: "e2e_cache", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Add variable" }).click();
  await page.getByRole("combobox", { name: "Key" }).fill("redis_port");
  await page.getByRole("textbox", { name: "Value" }).fill("6379");
  await page.getByRole("button", { name: "Save" }).click();
  const own = page.locator("table.vars:not(.inherited)");
  await expect(own.locator('tr[data-key="redis_port"]')).toContainText("6379");

  // --- move and rename it: ids change, the tree and the URL follow
  await page.getByRole("button", { name: "Move / rename" }).click();
  await page.getByRole("textbox", { name: "New parent path" }).fill("all.commercial.lab.dc1");
  await page.getByRole("textbox", { name: "New name" }).fill("e2e_cache2");
  await page.getByRole("button", { name: "Move", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "e2e_cache2" })).toBeVisible();
  await expect(page).toHaveURL(/path=all\.commercial\.lab\.dc1\.e2e_cache2/);
  await expect(page.getByRole("status")).toContainText("Moved all.commercial.prod.dc1.e2e_cache");
  await expect(own.locator('tr[data-key="redis_port"]')).toContainText("6379");

  // --- the key's history link still finds the write made before the move
  await own.locator('tr[data-key="redis_port"]').getByRole("link", { name: "history" }).click();
  await expect(page).toHaveURL(/#\/audit\?node=all\.commercial\.lab\.dc1\.e2e_cache2&key=redis_port/);
  await expect(page.locator("table.audit tbody tr")).toHaveCount(1);
  await expect(page.locator("table.audit tbody tr").first()).toContainText("variable.set");

  // --- delete the (leaf) node: no count to confirm, back at the parent
  await page.goto("/#/tree?path=all.commercial.lab.dc1.e2e_cache2");
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("form", { name: "Delete node" })).toContainText("Delete all.commercial.lab.dc1.e2e_cache2?");
  await page.getByRole("button", { name: "Delete node" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "dc1" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("Deleted 1 node(s)");
  await expect(page.getByRole("treeitem", { name: "e2e_cache2", exact: true })).toHaveCount(0);

  // --- declare a variable with a schema; a value it rejects is refused
  await page.getByRole("navigation", { name: "Views" }).getByRole("link", { name: "Variables" }).click();
  await page.getByRole("button", { name: "Declare a variable" }).click();
  await page.getByRole("textbox", { name: "Variable name" }).fill("e2e_port");
  await page.getByRole("combobox", { name: "Merge strategy" }).selectOption("replace");
  await page.getByRole("textbox", { name: "JSON Schema" }).fill('{"type": "integer"}');
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status")).toContainText("Saved e2e_port");
  await expect(page.locator('tr[data-name="e2e_port"]')).toContainText("integer");
  await page.goto("/#/tree?path=all.commercial.prod");
  await page.getByRole("button", { name: "Add variable" }).click();
  await page.getByRole("combobox", { name: "Key" }).fill("e2e_port");
  await page.getByRole("textbox", { name: "Value" }).fill('"eighty"');
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("alert")).toContainText("json_schema");

  // --- a tag over a group
  await page.getByRole("navigation", { name: "Views" }).getByRole("link", { name: "Tags" }).click();
  await page.getByRole("button", { name: "New tag" }).click();
  await page.getByRole("textbox", { name: "Tag name" }).fill("canary");
  await page.getByRole("textbox", { name: "Members" }).fill("all.commercial.prod.dc1.web");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status")).toContainText("Saved tag canary");
  await expect(page.locator('tr[data-name="canary"]')).toContainText("all.commercial.prod.dc1.web");

  // --- mint a token (shown once), then revoke it
  await page.getByRole("navigation", { name: "Views" }).getByRole("link", { name: "Tokens" }).click();
  await page.getByRole("textbox", { name: "Token name" }).fill("e2e-ci");
  await page.getByRole("button", { name: "Create token" }).click();
  await expect(page.getByLabel("New token", { exact: true }).locator("xpath=self::code")).toHaveText(/^ais_/);
  const row = page.locator('tr[data-name="e2e-ci"]');
  await row.getByRole("button", { name: "Revoke e2e-ci" }).click();
  await row.getByRole("button", { name: "Confirm revoking e2e-ci" }).click();
  await expect(row).toContainText("revoked");
});

// Issue 20: a group with descendants shows what would go, and the confirmation sends that count.
test("deleting a group shows the node and host counts and confirms them", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("E-mail").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // all.federal.prod.dc2.web: itself, edge, fed-edge01, fed-web01 (seed.yml)
  await page.goto("/#/tree?path=all.federal.prod.dc2.web");
  await expect(page.getByRole("heading", { level: 1, name: "web" })).toBeVisible();
  const rev = Number((await page.locator(".rev").textContent())?.replace("revision ", ""));
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByTestId("delete-count")).toHaveText("4 nodes, 2 hosts");
  // the preview wrote nothing
  await expect(page.locator(".rev")).toHaveText(`revision ${rev}`);
  // a refusal shows the server's message, not its JSON. The UI can only meet one through a
  // race (If-Match would 409 first), so the server's answer is stubbed for this one click.
  const refusal = "deleting all.federal.prod.dc2.web would delete 5 node(s) (3 host(s)), not the 4 expect_count says.";
  const confirmUrl = (u: URL) => u.pathname.startsWith("/api/v1/nodes/") && u.searchParams.has("expect_count");
  await page.route(confirmUrl, (route) =>
    route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { error: "count mismatch", message: refusal, nodes: 5, hosts: 3 } }) }),
  );
  await page.getByRole("button", { name: "Delete 4 nodes" }).click();
  await expect(page.getByRole("alert")).toContainText(`Refused: ${refusal} (HTTP 409)`);
  await page.unroute(confirmUrl);
  const sent = page.waitForRequest((r) => r.method() === "DELETE" && !r.url().includes("dry_run"));
  await page.getByRole("button", { name: "Delete 4 nodes" }).click();
  const url = new URL((await sent).url());
  expect(url.searchParams.get("recursive")).toBe("true");
  expect(url.searchParams.get("expect_count")).toBe("4");
  expect((await sent).headers()["if-match"]).toBe(`"${rev}"`);
  await expect(page.getByRole("heading", { level: 1, name: "dc2" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("Deleted 4 node(s) (2 host(s)) under all.federal.prod.dc2.web");
  await expect(page.getByRole("treeitem", { name: "web", exact: true })).toHaveCount(0);
});

// Issue 21: writes tagged with a change id are listed and reverted together from the Revisions
// page, after a dry-run preview; a revert is itself revertible.
test("revert a change set and then the revert, from the Revisions page", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("E-mail").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("tree", { name: "Inventory tree" })).toBeVisible();

  // two writes carrying one change id, the way a playbook sends them (X-AIS-Change on each)
  await page.evaluate(async () => {
    const s = JSON.parse(sessionStorage.getItem("ais.session")!);
    const headers = { Authorization: `Bearer ${s.token}`, "Content-Type": "application/json", "X-AIS-Change": "E2E-21" };
    let rev = (await (await fetch("/api/v1/revisions?limit=1", { headers })).json())[0].id;
    const web = await (await fetch("/api/v1/nodes/all.commercial.lab.dc1.web", { headers })).json();
    let r = await fetch(`/api/v1/nodes/${web.id}/vars/http_port`, {
      method: "PUT",
      headers: { ...headers, "If-Match": `"${rev}"` },
      body: JSON.stringify({ value: 9443 }),
    });
    if (!r.ok) throw new Error(`set failed: ${r.status}`);
    rev = (await r.json()).revision;
    r = await fetch("/api/v1/nodes", {
      method: "POST",
      headers: { ...headers, "If-Match": `"${rev}"` },
      body: JSON.stringify({ parent_path: "all.commercial.lab.dc1.web", name: "lab-web02", kind: "host" }),
    });
    if (!r.ok) throw new Error(`create failed: ${r.status}`);
  });

  await page.goto("/#/revisions?change=E2E-21");
  const rows = page.locator("table.list tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.first()).toContainText("E2E-21");

  // the preview: what it would do and to which hosts; it writes nothing
  await page.getByRole("button", { name: "Revert change E2E-21" }).click();
  const panel = page.getByRole("region", { name: "Revert change E2E-21" });
  await expect(panel.getByTestId("revert-ops")).toHaveText("2 operation(s)");
  await expect(panel).toContainText("delete all.commercial.lab.dc1.web.lab-web02");
  await expect(panel).toContainText("set http_port on all.commercial.lab.dc1.web to 8081");
  await expect(panel.getByTestId("revert-hosts")).toHaveText("2 hosts");
  const hosts = panel.getByRole("list", { name: "Hosts that would change" });
  await expect(hosts).toContainText("lab-web02 removed");
  await expect(hosts).toContainText("lab-web01 changed (http_port)");
  const based = Number((await panel.getByText(/^based on revision \d+$/).textContent())?.replace("based on revision ", ""));
  const sent = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/v1/changes/E2E-21/revert") && !(r.postData() ?? "").includes("dry_run"));
  await panel.getByRole("button", { name: "Confirm revert of change E2E-21" }).click();
  expect((await sent).headers()["if-match"]).toBe(`"${based}"`);
  await expect(page.getByRole("status")).toContainText("Reverted change E2E-21 as revision ");
  await expect(page.getByRole("status")).toContainText(": 2 operation(s)");

  // the audit log filters by the change id too
  await page.goto("/#/audit?change=E2E-21");
  await expect(page.locator("table.audit tbody tr")).toHaveCount(2);

  // revert the revert from the unfiltered list: the host comes back
  await page.goto("/#/revisions");
  const newest = page.locator("table.list tbody tr").first();
  await expect(newest).toContainText("revert change E2E-21");
  const id = await newest.getAttribute("data-revision");
  await newest.getByRole("button", { name: `Revert revision ${id}` }).click();
  const again = page.getByRole("region", { name: `Revert revision ${id}` });
  await expect(again.getByTestId("revert-ops")).toHaveText("2 operation(s)");
  await again.getByRole("button", { name: `Confirm revert of revision ${id}` }).click();
  await expect(page.getByRole("status")).toContainText(`Reverted revision ${id}`);
  const back = await page.evaluate(async () => {
    const s = JSON.parse(sessionStorage.getItem("ais.session")!);
    return (await fetch("/api/v1/nodes/all.commercial.lab.dc1.web.lab-web02", { headers: { Authorization: `Bearer ${s.token}` } })).status;
  });
  expect(back).toBe(200);

  // reverting the change once more now conflicts: the panel says why and offers force
  await page.goto("/#/revisions?change=E2E-21");
  await page.getByRole("button", { name: "Revert change E2E-21" }).click();
  const refused = page.getByRole("region", { name: "Revert change E2E-21" });
  await expect(refused.getByRole("alert")).toContainText("later revisions changed the same things");
  await expect(refused.getByRole("checkbox", { name: "Force" })).toBeVisible();
  await refused.getByRole("button", { name: "Cancel" }).click();
});

// Issue 23: where is a key set, which hosts have which value, and its history by key alone.
// Last in the file and asserts no revision numbers; it writes its own e2e_gpu key first.
test("variable search: where set, hosts by value, links from the tree, variables and node pages", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("E-mail").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("tree", { name: "Inventory tree" })).toBeVisible();

  // the string "560.35" for all.commercial.prod (web01, web02, db01), "550.90" on dc1.db (db01)
  await page.evaluate(async () => {
    const s = JSON.parse(sessionStorage.getItem("ais.session")!);
    const headers = { Authorization: `Bearer ${s.token}`, "Content-Type": "application/json" };
    let rev = (await (await fetch("/api/v1/revisions?limit=1", { headers })).json())[0].id;
    for (const [path, value] of [
      ["all.commercial.prod", "560.35"],
      ["all.commercial.prod.dc1.db", "550.90"],
    ]) {
      const node = await (await fetch(`/api/v1/nodes/${path}`, { headers })).json();
      const r = await fetch(`/api/v1/nodes/${node.id}/vars/e2e_gpu`, { method: "PUT", headers: { ...headers, "If-Match": `"${rev}"` }, body: JSON.stringify({ value }) });
      if (!r.ok) throw new Error(`set failed: ${r.status}`);
      rev = (await r.json()).revision;
    }
  });

  // --- the tree's find box searches under the selected node
  await page.goto("/#/tree?path=all.commercial");
  await expect(page.getByRole("heading", { level: 1, name: "commercial" })).toBeVisible();
  await page.getByRole("searchbox", { name: "Find a variable" }).fill("e2e_gpu");
  await page.getByRole("searchbox", { name: "Find a variable" }).press("Enter");
  await expect(page).toHaveURL(/#\/search\?key=e2e_gpu&subtree=all\.commercial$/);
  await expect(page.getByTestId("set-count")).toHaveText("Set on 2 nodes");
  const setters = page.locator("table.setters tbody tr");
  await expect(setters).toHaveCount(2);
  await expect(page.locator('table.setters tr[data-path="all.commercial.prod.dc1.db"]')).toContainText('"550.90"');
  await expect(page.getByTestId("host-count")).toHaveText(/^3 hosts have it · \d+ without it$/); // the lab hosts have none
  await expect(page.getByRole("list", { name: "Values" })).toContainText('"560.35" on 2 hosts');
  await expect(page.locator('table.host-values tr[data-host="db01"]')).toContainText("all.commercial.prod.dc1.db");
  await expect(page.locator('table.host-values tr[data-host="web01"]')).toContainText("all.commercial.prod");

  // --- a value from the distribution filters the hosts to it
  await page.getByRole("list", { name: "Values" }).getByRole("link", { name: '"550.90"' }).click();
  await expect(page.getByTestId("host-count")).toHaveText(/^1 of 3 hosts match/);
  await expect(page.locator("table.host-values tbody tr")).toHaveCount(1);
  await expect(page.locator('table.host-values tr[data-host="db01"]')).toBeVisible();

  // --- a number is not the string: typed values follow YAML/JSON
  await page.getByRole("textbox", { name: "Value" }).fill("550.90");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("set-count")).toHaveText("Set on 0 nodes");
  await expect(page.getByTestId("host-count")).toHaveText(/^0 of 3 hosts match/);

  // --- history by key alone (editor): both writes, on two different nodes
  await page.getByRole("textbox", { name: "Value" }).fill("");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("link", { name: "history" }).click();
  await expect(page).toHaveURL(/#\/audit\?key=e2e_gpu&subtree=all\.commercial$/);
  await expect(page.locator("table.audit tbody tr")).toHaveCount(2);
  await page.goto("/#/audit?key=e2e_gpu");
  await expect(page.locator("table.audit tbody tr")).toHaveCount(2);
  await expect(page.locator("table.audit tbody tr").first()).toContainText("variable.set");

  // --- "where set" from a node page and from the Variables page
  await page.goto("/#/tree?path=all.commercial.prod.dc1.web");
  await page.locator('table.vars:not(.inherited) tr[data-key="http_port"]').getByRole("link", { name: "Where is http_port set" }).click();
  await expect(page).toHaveURL(/#\/search\?key=http_port$/);
  await expect(page.locator('table.setters tr[data-path="all.commercial.prod.dc1.web"]')).toContainText("8080");
  await page.getByRole("navigation", { name: "Views" }).getByRole("link", { name: "Variables" }).click();
  await page.getByRole("link", { name: "Where is packages set" }).click();
  await expect(page).toHaveURL(/#\/search\?key=packages$/);
  await expect(page.getByRole("columnheader", { name: "Emits (folded)" })).toBeVisible();
  await expect(page.locator('table.setters tr[data-path="all"]')).toContainText("- vim");
});

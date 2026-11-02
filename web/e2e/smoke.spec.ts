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

  // --- delete the subtree: back at the parent
  await page.goto("/#/tree?path=all.commercial.lab.dc1.e2e_cache2");
  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByRole("button", { name: "Delete subtree" }).click();
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

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

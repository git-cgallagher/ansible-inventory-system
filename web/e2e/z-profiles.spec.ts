import { expect, test } from "@playwright/test";

// Profiles (issue 22). Runs after smoke.spec.ts on the same server (file name order), so it
// writes freely and never assumes a revision number. The seed attaches web-baseline at
// all.commercial.prod.dc1.web (web01, web02) and all.federal.prod.dc2.web, which the smoke
// deletes, so one attachment is left when this runs.
const EMAIL = process.env.E2E_EMAIL ?? "e2e@example.com";
const PASSWORD = process.env.E2E_PASSWORD ?? "example-not-secret";

test("profiles: one value change reaches every attachment, provenance, attach and detach with previews", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("E-mail").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("tree", { name: "Inventory tree" })).toBeVisible();

  // --- the list, then the seed's profile
  await page.getByRole("navigation", { name: "Views" }).getByRole("link", { name: "Profiles" }).click();
  const row = page.locator('table.profiles tr[data-name="web-baseline"]');
  await expect(row).toContainText("1 node");
  await row.getByRole("link", { name: "web-baseline" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "web-baseline" })).toBeVisible();
  await expect(page.locator('table.attached tr[data-path="all.commercial.prod.dc1.web"]')).toBeVisible();

  // --- one edit, previewed: every host under every attachment
  await page.getByRole("button", { name: "Edit nginx_worker_processes" }).click();
  await page.getByRole("textbox", { name: "Value" }).fill("8");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("impact-total")).toHaveText("This changes 2 hosts");
  await page.getByRole("button", { name: "Confirm save" }).click();
  await expect(page.getByRole("status")).toContainText("Saved nginx_worker_processes");
  await expect(page.locator('tr[data-key="nginx_worker_processes"]')).toContainText("8");

  // --- a node it is attached to: the list, and the value's provenance names the profile
  await page.goto("/#/tree?path=all.commercial.prod.dc1.web");
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "web" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Attached profiles" })).toContainText("web-baseline");
  const inherited = page.locator("table.vars.inherited tr[data-key='nginx_worker_processes']");
  await expect(inherited).toContainText("8");
  await expect(inherited.getByRole("link", { name: "profile:web-baseline" })).toBeVisible();
  await expect(page.locator("table.vars:not(.inherited) tr[data-key='nginx_worker_processes']")).toHaveCount(0); // not "set here"

  // --- a new profile (the smoke may have added lab hosts, so counts are not pinned): create, add a value, attach through the node picker (previewed)
  await page.getByRole("navigation", { name: "Views" }).getByRole("link", { name: "Profiles" }).click();
  await page.getByRole("button", { name: "New profile" }).click();
  await page.getByRole("textbox", { name: "Profile name" }).fill("e2e-role");
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "e2e-role" })).toBeVisible();
  await page.getByRole("button", { name: "Add value" }).click();
  await page.getByRole("combobox", { name: "Key" }).fill("e2e_flag");
  await page.getByRole("textbox", { name: "Value" }).fill("true");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("impact-total")).toHaveText("This changes no host."); // attached nowhere yet
  await page.getByRole("button", { name: "Confirm save" }).click();
  await expect(page.locator('tr[data-key="e2e_flag"]')).toBeVisible();
  await page.getByRole("combobox", { name: "Attach to" }).fill("lab.dc1.w");
  await page.getByRole("option", { name: "all.commercial.lab.dc1.web", exact: true }).click();
  await page.getByRole("button", { name: "Attach", exact: true }).click();
  const attaching = page.getByLabel("Attach to all.commercial.lab.dc1.web", { exact: true });
  await expect(attaching.getByTestId("impact-total")).toHaveText(/^This changes \d+ hosts?$/);
  await attaching.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByRole("status")).toContainText("Attached e2e-role to all.commercial.lab.dc1.web");
  await expect(page.locator('table.attached tr[data-path="all.commercial.lab.dc1.web"]')).toBeVisible();

  // --- the variable search says where it comes from
  await page.goto("/#/search?key=e2e_flag");
  await page.reload();
  await expect(page.getByTestId("key-profiles")).toContainText("e2e-role");
  await expect(page.locator('tr[data-host="lab-web01"]')).toContainText("profile:e2e-role");

  // --- detach from the node page, previewed
  await page.goto("/#/tree?path=all.commercial.lab.dc1.web");
  await page.reload();
  await page.getByRole("button", { name: "Detach e2e-role" }).click();
  const detaching = page.getByLabel("Detaching e2e-role", { exact: true });
  await expect(detaching.getByTestId("impact-total")).toHaveText(/^This changes \d+ hosts?$/);
  await detaching.getByRole("button", { name: "Confirm detach" }).click();
  await expect(page.getByRole("status")).toContainText("Detached profile e2e-role");
  await expect(page.getByText("No profile attached.")).toBeVisible();

  // --- attach back from the node page, then delete the profile (detaching it everywhere)
  await page.getByRole("combobox", { name: "Profile to attach" }).selectOption("e2e-role");
  await page.getByRole("button", { name: "Attach", exact: true }).click();
  await page.getByLabel("Attaching e2e-role", { exact: true }).getByRole("button", { name: "Confirm attach" }).click();
  await expect(page.getByRole("list", { name: "Attached profiles" })).toContainText("e2e-role");
  await page.goto("/#/profiles?name=e2e-role");
  await page.reload();
  await page.getByRole("button", { name: "Delete profile" }).click();
  const deleting = page.getByLabel(/^Delete e2e-role/);
  await expect(deleting.getByTestId("impact-total")).toHaveText(/^This changes \d+ hosts?$/);
  await deleting.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Profiles" })).toBeVisible();
  await expect(page.locator('table.profiles tr[data-name="e2e-role"]')).toHaveCount(0);
});

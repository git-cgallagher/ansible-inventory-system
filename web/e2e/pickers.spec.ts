import { expect, test, type Page } from "@playwright/test";

// Node pickers (issue #27): typeaheads over the bounded lookup GET /api/v1/nodes?q=…, never the
// whole node list. Read-only on purpose: Playwright runs this file before smoke.spec.ts on the
// same fresh server, and the smoke counts revisions from 1.
const EMAIL = process.env.E2E_EMAIL ?? "e2e@example.com";
const PASSWORD = process.env.E2E_PASSWORD ?? "example-not-secret";

/** Every GET of /api/v1/nodes the page makes, split into lookups and unbounded lists. */
function watchNodeRequests(page: Page) {
  const lookups: URL[] = [];
  const unbounded: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (r.method() !== "GET" || u.pathname !== "/api/v1/nodes") return;
    if (u.searchParams.has("q") || u.searchParams.has("limit")) lookups.push(u);
    else unbounded.push(r.url());
  });
  return { lookups, unbounded };
}

/** Open a deep link as a fresh page load (the views are not keyed by their parameters). */
async function open(page: Page, hash: string) {
  await page.goto(`/${hash}`);
  await page.reload();
}

test("host and path pickers: keyboard, mouse, deep links, and never the whole node list", async ({ page }) => {
  const seen = watchNodeRequests(page);
  await page.goto("/");
  await page.getByLabel("E-mail").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("tree", { name: "Inventory tree" })).toBeVisible();

  // --- Render, by keyboard: type, Down Down, Enter picks; Enter again submits
  await page.getByRole("navigation", { name: "Views" }).getByRole("link", { name: "Render" }).click();
  const host = page.getByRole("combobox", { name: "Host" });
  const hostList = page.getByRole("listbox", { name: "Host suggestions" });
  await expect(host).toHaveAttribute("aria-expanded", "false");
  await host.pressSequentially("web");
  const options = hostList.getByRole("option");
  // exact/prefix names first, then names containing "web", then hosts under a web group
  await expect(options).toHaveText([/^web01 host\s*all\.commercial\.prod\.dc1\.web\.web01$/, /^web02 /, /^lab-web01 /, /^fed-web01 /, /^fed-edge01 /]);
  await expect(host).toHaveAttribute("aria-expanded", "true");
  const last = seen.lookups.at(-1)!;
  expect([last.searchParams.get("q"), last.searchParams.getAll("kind"), last.searchParams.get("limit")]).toEqual(["web", ["host"], "21"]);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  const web02 = hostList.getByRole("option", { name: "web02", exact: true });
  await expect(web02).toHaveAttribute("aria-selected", "true");
  await expect(host).toHaveAttribute("aria-activedescendant", (await web02.getAttribute("id"))!);
  await page.keyboard.press("ArrowUp");
  await expect(hostList.getByRole("option", { name: "web01", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(host).toHaveValue("web02");
  await expect(hostList).toBeHidden();
  await expect(host).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Enter");
  const result = page.getByRole("region", { name: "Render result" });
  await expect(result).toContainText("web02 · revision 1");
  await expect(page).toHaveURL(/#\/render\?host=web02$/);

  // --- by mouse, matching a substring of the name
  await host.fill("edge");
  await hostList.getByRole("option", { name: "fed-edge01", exact: true }).click();
  await expect(host).toHaveValue("fed-edge01");
  await page.getByRole("button", { name: "Render", exact: true }).click();
  await expect(result).toContainText("fed-edge01 · revision 1");

  // --- Escape closes only the list; a name typed in full renders with Enter
  await host.fill("db");
  await expect(hostList.getByRole("option", { name: "db01", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(hostList).toBeHidden();
  await expect(host).toHaveValue("db");
  await host.fill("db01");
  await expect(hostList).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(result).toContainText("db01 · revision 1");
  await host.fill("zzz");
  await expect(hostList).toContainText("No matches.");

  // --- a mock host at a path: the path picker offers non-host nodes by path
  await page.getByLabel("Mock host at a path").check();
  const path = page.getByRole("combobox", { name: "Path" });
  await path.fill("lab.dc1");
  const pathList = page.getByRole("listbox", { name: "Path suggestions" });
  await expect(pathList.getByRole("option")).toHaveText([/all\.commercial\.lab\.dc1$/, /all\.commercial\.lab\.dc1\.web$/]);
  await pathList.getByRole("option", { name: "all.commercial.lab.dc1.web", exact: true }).click();
  await expect(path).toHaveValue("all.commercial.lab.dc1.web");
  await page.getByRole("button", { name: "Render", exact: true }).click();
  await expect(result).toContainText("mock host at all.commercial.lab.dc1.web");
  await expect(result.locator('tr[data-key="http_port"]')).toContainText("8081");

  // --- deep links render without any node lookup at all
  const before = seen.lookups.length;
  await open(page, "#/render?host=web01");
  await expect(result).toContainText("web01 · revision 1");
  await expect(page.getByRole("combobox", { name: "Host" })).toHaveValue("web01");
  await open(page, "#/render?path=all.federal.prod.dc2.web");
  await expect(result).toContainText("mock host at all.federal.prod.dc2.web");
  await expect(page.getByRole("combobox", { name: "Path" })).toHaveValue("all.federal.prod.dc2.web");
  await open(page, "#/diff?host=web01&from=1&to=1");
  await expect(page.getByText("web01 resolves identically at revisions 1 and 1.")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Host" })).toHaveValue("web01");
  expect(seen.lookups.length).toBe(before);

  // --- Diff: pick a host by keyboard, compare
  await open(page, "#/diff");
  const diffHost = page.getByRole("combobox", { name: "Host" });
  await diffHost.pressSequentially("lab");
  await expect(page.getByRole("option", { name: "lab-web01", exact: true })).toBeVisible();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(diffHost).toHaveValue("lab-web01");
  await page.getByRole("combobox", { name: "From revision" }).selectOption("1");
  await page.getByRole("button", { name: "Compare" }).click();
  await expect(page.getByText("lab-web01 resolves identically at revisions 1 and 1.")).toBeVisible();
  await expect(page).toHaveURL(/#\/diff\?host=lab-web01&from=1&to=1$/);

  // --- Search's subtree picker
  await open(page, "#/search");
  await page.getByRole("combobox", { name: "Variable key" }).fill("http_port");
  await page.getByRole("combobox", { name: "Subtree" }).fill("federal");
  await page.getByRole("option", { name: "all.federal", exact: true }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page).toHaveURL(/subtree=all\.federal/);
  await expect(page.getByTestId("set-count")).toHaveText("Set on 1 node");

  // Neither Render nor Diff (nor anything else above) ever asked for the whole node list.
  expect(seen.unbounded).toEqual([]);
});

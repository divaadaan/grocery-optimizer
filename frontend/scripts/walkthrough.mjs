#!/usr/bin/env node
/**
 * Headless walkthrough capture: registers a demo profile, walks the planner
 * end to end on live data, and saves one screenshot per step.
 *
 *   DEMO_POSTAL=... node scripts/walkthrough.mjs [--out DIR] [--diet vegetarian]
 *        [--meals 3] [--user-id N]   (reuse a user, skip registration+generation)
 *
 * Needs the API on :8000 and Vite on :3000. The postal code is a secret
 * (CLAUDE.md "Test postal code"): pass it via DEMO_POSTAL, never on the command
 * line. Every frame is redacted before capture, and a frame whose text or
 * input values still contain the code is refused and the run aborts.
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => {
    if (a.startsWith("--")) acc.push([a.slice(2), all[i + 1]?.startsWith("--") ? "1" : all[i + 1] ?? "1"]);
    return acc;
  }, []),
);
const BASE = process.env.DEMO_BASE_URL ?? "http://localhost:3000";
const OUT = resolve(args.out ?? "../docs/walkthrough/img");
const DIET = args.diet ?? "vegetarian";
const MEALS = Number(args.meals ?? 3);
const REUSE_ID = args["user-id"] ? Number(args["user-id"]) : null;
const POSTAL = (process.env.DEMO_POSTAL ?? "").replace(/\s+/g, "").toUpperCase();
const MASK = "••• •••";
const GEN_TIMEOUT_MS = Number(process.env.DEMO_GEN_TIMEOUT_MS ?? 15 * 60_000);

if (POSTAL.length < 6) {
  console.error("Set DEMO_POSTAL (e.g. from .env TEST_POSTAL_CODE).");
  process.exit(2);
}
mkdirSync(OUT, { recursive: true });

const postalPatterns = [POSTAL, `${POSTAL.slice(0, 3)} ${POSTAL.slice(3)}`];

/** Replace the postal code in text nodes and input values, in place. */
async function redact(page) {
  await page.evaluate(
    ({ patterns, mask }) => {
      const re = new RegExp(patterns.map((p) => p.replace(/\s/g, "\\s?")).join("|"), "gi");
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (re.test(n.nodeValue)) n.nodeValue = n.nodeValue.replace(re, mask);
        re.lastIndex = 0;
      }
      for (const el of document.querySelectorAll("input, textarea")) {
        if (re.test(el.value)) el.value = el.value.replace(re, mask);
        re.lastIndex = 0;
      }
    },
    { patterns: postalPatterns, mask: MASK },
  );
}

async function assertClean(page, label) {
  const leaked = await page.evaluate((patterns) => {
    const values = [...document.querySelectorAll("input, textarea")].map((e) => e.value);
    const hay = (document.body.innerText + "\n" + values.join("\n")).toUpperCase().replace(/\s+/g, "");
    return patterns.some((p) => hay.includes(p.replace(/\s+/g, "")));
  }, postalPatterns);
  if (leaked) throw new Error(`Refusing to save "${label}": postal code visible after redaction.`);
}

let step = 0;
const shots = [];
async function shot(page, name, { fullPage = true, locator } = {}) {
  // A sticky header repeats mid-image in full-page captures.
  await page.addStyleTag({ content: ".topbar{position:static !important}" });
  await page.evaluate(() => document.fonts.ready);
  await redact(page);
  await assertClean(page, name);
  const file = `${String(++step).padStart(2, "0")}-${name}.png`;
  if (locator) await locator.screenshot({ path: join(OUT, file) });
  else await page.screenshot({ path: join(OUT, file), fullPage });
  shots.push(file);
  console.log("saved", file);
}

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1360, height: 900 },
  deviceScaleFactor: 2,
  colorScheme: "light",
});
const page = await context.newPage();
page.setDefaultTimeout(60_000);

try {
  // 1. Landing ----------------------------------------------------------
  await page.goto(BASE);
  await page.evaluate(() => localStorage.clear());
  await page.goto(BASE);
  await page.waitForLoadState("networkidle");
  await shot(page, "landing", { fullPage: false });

  // 2. Registration -----------------------------------------------------
  await page.goto(`${BASE}/setup`);
  if (REUSE_ID) {
    await page.getByPlaceholder("User ID").fill(String(REUSE_ID));
    await page.getByRole("button", { name: /load profile/i }).click();
    await page.waitForURL(`${BASE}/`);
  } else {
    await page.getByLabel("Email").fill(`demo-${DIET}-${Date.now()}@example.com`);
    await page.getByLabel("Postal code").fill(POSTAL);
    await page.getByLabel("Household size").fill("2");
    await page.getByLabel(new RegExp(`^${DIET}$`, "i")).check();
    await shot(page, "register", { fullPage: false });
    await page.getByRole("button", { name: /register/i }).click();
    await page.waitForURL(`${BASE}/`);
  }
  await page.waitForLoadState("networkidle");
  await shot(page, "home-profile", { fullPage: false });

  // 3. Deals ------------------------------------------------------------
  await page.goto(`${BASE}/deals`);
  await page.locator(".deal-card").first().waitFor();
  await shot(page, "deals", { fullPage: false });

  // 4. Trays + pantry ---------------------------------------------------
  await page.goto(`${BASE}/planner`);
  const slider = page.locator('input[type="range"]');
  await slider.fill(String(MEALS));
  await page.locator(".tray-card").first().waitFor({ timeout: 120_000 });
  await page.waitForLoadState("networkidle");
  await shot(page, "planner-trays");
  // Reject one side to show the carousel is a review step.
  const sides = page.locator('[aria-label="Sides tray"] .tray-card');
  if ((await sides.count()) > 3) await sides.nth(2).click();
  await shot(page, "tray-reject", { locator: page.locator(".tray").nth(1) });

  // 5. Generation -------------------------------------------------------
  if (!REUSE_ID) {
    await page.getByRole("button", { name: /^plan \d+ meals?$/i }).click();
    await page.locator(".pipeline").waitFor();
    await page.waitForTimeout(4_000);
    await shot(page, "generating", { fullPage: false });
    await page
      .locator(".recipe-card, .banner-error")
      .first()
      .waitFor({ timeout: GEN_TIMEOUT_MS });
    if (await page.locator(".banner-error").count()) {
      await shot(page, "generation-error");
      throw new Error("generation failed: " + (await page.locator(".banner-error").first().innerText()));
    }
    const summary = page.locator(".card", { hasText: "Your plan" });
    await summary.scrollIntoViewIfNeeded();
    await shot(page, "plan-result");
    await page.locator(".recipe-card").first().getByRole("button", { name: /show details/i }).click();
    await shot(page, "recipe-detail", { locator: page.locator(".recipe-card").first() });
  }

  // 6. Saved recipes + shopping list ------------------------------------
  await page.goto(`${BASE}/recipes`);
  await page.locator(".recipe-card, .empty-state").first().waitFor();
  await shot(page, "saved-recipes", { fullPage: false });
  await page.goto(`${BASE}/shopping-list`);
  await page.locator(".card, .empty-state, .banner").first().waitFor();
  await page.waitForLoadState("networkidle");
  await shot(page, "shopping-list");

  const userId = await page.evaluate(() => {
    for (const k of Object.keys(localStorage)) {
      try {
        const v = JSON.parse(localStorage.getItem(k));
        if (v?.user_id) return v.user_id;
      } catch {}
    }
    return null;
  });
  writeFileSync(join(OUT, "manifest.json"), JSON.stringify({ diet: DIET, meals: MEALS, userId, shots }, null, 2));
  console.log("done; user", userId);
} finally {
  await browser.close();
}

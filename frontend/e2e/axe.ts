import { readFileSync } from "node:fs";

import type { Page } from "@playwright/test";

const source = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

type Violation = { id: string; impact: string | null; help: string; targets: string[] };

/** Runs axe-core in the page for WCAG 2 A and AA rules. */
export default class AxeBuilder {
  constructor(private page: Page) {}

  async run(): Promise<Violation[]> {
    await this.page.addScriptTag({ content: source });
    return this.page.evaluate(async () => {
      // @ts-expect-error axe is added to the page above
      const result = await window.axe.run(document, { runOnly: ["wcag2a", "wcag2aa"] });
      return result.violations.map((v: { id: string; impact: string | null; help: string;
                                         nodes: { target: string[] }[] }) =>
        ({ id: v.id, impact: v.impact, help: v.help, targets: v.nodes.slice(0, 3).map((n) => n.target.join(" ")) }));
    });
  }
}

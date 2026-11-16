import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

const { scripts } = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
);

function runVerification(
  buildExit: number,
  browserExit: number,
  args: string[] = [],
) {
  const directory = mkdtempSync(join(tmpdir(), "wisp-browser-verification-"));
  try {
    writeFileSync(
      join(directory, "package.json"),
      JSON.stringify({
        private: true,
        scripts: {
          build: "node step.cjs build",
          "test:browser": "node step.cjs browser",
          "verify:browser": scripts["verify:browser"],
        },
      }),
    );
    writeFileSync(
      join(directory, "step.cjs"),
      `const { appendFileSync } = require("node:fs");
const [step, ...args] = process.argv.slice(2);
appendFileSync("steps.jsonl", JSON.stringify({ step, args }) + "\\n");
process.exit(Number(step === "build" ? process.env.BUILD_EXIT : process.env.BROWSER_EXIT));
`,
    );
    const result = spawnSync("npm", ["run", "verify:browser", "--", ...args], {
      cwd: directory,
      env: {
        ...process.env,
        BUILD_EXIT: String(buildExit),
        BROWSER_EXIT: String(browserExit),
      },
      encoding: "utf8",
      timeout: 15_000,
      shell: process.platform === "win32",
    });
    assert.ifError(result.error);
    const steps = readFileSync(join(directory, "steps.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    return {
      status: result.status,
      steps,
      output: result.stdout + result.stderr,
    };
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("browser verification builds first and forwards arguments only to tests", () => {
  const args = ["tests/browser/example with spaces.spec.ts", "--workers=1"];
  const result = runVerification(0, 0, args);
  assert.equal(result.status, 0, result.output);
  assert.deepEqual(result.steps, [
    { step: "build", args: [] },
    { step: "browser", args },
  ]);
});

test("a failed build prevents browser tests and preserves the build exit code", () => {
  const result = runVerification(7, 0);
  assert.equal(result.status, 7, result.output);
  assert.deepEqual(result.steps, [{ step: "build", args: [] }]);
});

test("browser verification preserves a failing test exit code", () => {
  const result = runVerification(0, 9);
  assert.equal(result.status, 9, result.output);
  assert.deepEqual(result.steps, [
    { step: "build", args: [] },
    { step: "browser", args: [] },
  ]);
});

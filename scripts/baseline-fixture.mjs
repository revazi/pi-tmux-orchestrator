import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export async function verifyBaselineFixture({
  baseline,
  fixturePath,
  mismatchMessage,
  verifiedMessage,
}) {
  if (process.argv.includes("--write")) {
    await writeFile(fixturePath, `${JSON.stringify(baseline, null, 2)}\n`, "utf8");
    process.stdout.write(`Wrote ${fixturePath}\n`);
    return;
  }
  const expected = JSON.parse(await readFile(fixturePath, "utf8"));
  if (JSON.stringify(expected) !== JSON.stringify(baseline)) {
    throw new Error(mismatchMessage);
  }
  process.stdout.write(`${verifiedMessage}\n`);
}

export function runBaselineMain(moduleUrl, main) {
  if (!process.argv[1] || resolve(process.argv[1]) !== fileURLToPath(moduleUrl)) return;
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}

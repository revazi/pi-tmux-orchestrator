#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT=${1:-"$ROOT/.coverage/coverage-final.json"}

if [[ "$OUTPUT" != /* ]]; then
  OUTPUT="$ROOT/$OUTPUT"
fi
mkdir -p "$(dirname "$OUTPUT")"
rm -f "$OUTPUT"

PI_TMUX_COVERAGE_FILE="$OUTPUT" node \
  --experimental-test-coverage \
  --test-reporter="$ROOT/scripts/node-coverage-reporter.mjs" \
  --test "$ROOT/tests/extension.test.mjs"

node - "$ROOT" "$OUTPUT" <<'NODE'
import { readFileSync } from "node:fs";
import { relative, resolve } from "node:path";

const root = resolve(process.argv[2]);
const output = resolve(process.argv[3]);
const coverage = JSON.parse(readFileSync(output, "utf8"));
const files = Object.values(coverage);
if (files.length === 0) throw new Error("coverage report contains no files");
if (files.some((file) => relative(root, file.path).startsWith(".."))) {
  throw new Error("coverage report contains a file outside the repository");
}
const functions = files.reduce((total, file) => total + Object.keys(file.f).length, 0);
const covered = files.reduce(
  (total, file) => total + Object.values(file.f).filter((count) => count > 0).length,
  0,
);
console.log(`Wrote measured Node test coverage for ${files.length} files (${covered}/${functions} functions) to ${output}`);
NODE

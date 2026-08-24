#!/usr/bin/env node
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixturePath = resolve(root, "tests/fixtures/execution-profile-baseline.json");

export function buildExecutionProfileBaseline() {
  return {
    schema_version: 1,
    benchmark_kind: "execution-profile-policy-and-evidence-availability",
    fixed_cases: ["simple", "medium", "multi-round"],
    packaged_default: "thorough",
    packaged_profiles: {
      economy: {
        implementer: "medium", reviewer: "medium", probe: "low",
        playwright: "medium", django: "medium",
      },
      balanced: {
        implementer: "high", reviewer: "high", probe: "medium",
        playwright: "medium", django: "medium",
      },
      thorough: {
        implementer: "xhigh", reviewer: "high", probe: "high",
        playwright: "high", django: "high",
      },
    },
    compatibility: {
      preserves_pre_profile_packaged_thinking: true,
      default_change_supported_by_comparative_evidence: false,
    },
    comparative_evidence: {
      provider_usage: {
        availability: "unavailable",
        required_metrics: [
          "provider_calls", "input_tokens", "cache_read_tokens", "cache_write_tokens",
          "output_tokens", "reasoning_tokens", "cost_total",
        ],
      },
      quality: {
        availability: "unavailable",
        required_metrics: [
          "acceptance_tests", "reviewer_verdict", "finding_count", "revision_rounds",
        ],
      },
    },
    claims: {
      provider_token_savings: false,
      billing_savings: false,
      quality_equivalence: false,
      recommended_default: false,
    },
  };
}

async function main() {
  const baseline = buildExecutionProfileBaseline();
  if (process.argv.includes("--write")) {
    await writeFile(fixturePath, `${JSON.stringify(baseline, null, 2)}\n`, "utf8");
    process.stdout.write(`Wrote ${fixturePath}\n`);
    return;
  }
  const expected = JSON.parse(await readFile(fixturePath, "utf8"));
  if (JSON.stringify(expected) !== JSON.stringify(baseline)) {
    throw new Error("Execution-profile baseline changed; inspect and recapture with --write");
  }
  process.stdout.write("Verified execution-profile policy and unavailable comparative evidence.\n");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}

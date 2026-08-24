#!/usr/bin/env node
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { testHooks as workerHooks } from "../extensions/orchestrator-worker.js";
import { runBaselineMain, verifyBaselineFixture } from "./baseline-fixture.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixturePath = resolve(root, "tests/fixtures/phased-implementation-baseline.json");
const messageType = "pi-tmux-orchestrator-message-v1";
const planAssignment = "a".repeat(32);
const implementationAssignment = "b".repeat(32);

function delivery(details, content) {
  return {
    role: "custom",
    content,
    customType: messageType,
    details,
  };
}

function inspectionHistory() {
  const messages = [
    delivery({ kind: "context", delivery_kind: "baseline", round: 1 }, "bounded worker baseline"),
    delivery({ kind: "assignment", assignment_id: planAssignment, assignment_kind: "plan", round: 1 }, "inspect the complex task"),
    { role: "user", content: [{ type: "text", text: "direct operator steering" }] },
  ];
  for (let index = 0; index < 6; index += 1) {
    messages.push(
      { role: "assistant", content: [{ type: "text", text: `inspection analysis ${index}` }] },
      { role: "toolResult", toolName: "read", content: [{ type: "text", text: `path-${index}\n${"source evidence ".repeat(500)}` }] },
    );
  }
  return messages;
}

function serializedMetrics(messages) {
  const visible = workerHooks.filterWorkerContext(messages);
  const serialized = JSON.stringify(visible);
  return {
    visible_messages: visible.length,
    serialized_characters: serialized.length,
    serialized_utf8_bytes: Buffer.byteLength(serialized, "utf8"),
    direct_steering_retained: visible.some((message) => JSON.stringify(message).includes("direct operator steering")),
  };
}

export function buildPhasedImplementationBaseline() {
  const singleMessages = inspectionHistory();
  const phasedMessages = [
    ...inspectionHistory(),
    {
      role: "toolResult",
      toolName: "orchestrator_report",
      content: [{ type: "text", text: "bounded plan accepted" }],
    },
    delivery({ kind: "context", delivery_kind: "run_state", round: 1 }, "bounded accepted plan run state"),
    delivery({
      kind: "assignment",
      assignment_id: implementationAssignment,
      assignment_kind: "implementation",
      round: 1,
    }, "implement and verify from the accepted plan"),
  ];
  const single = serializedMetrics(singleMessages);
  const phased = serializedMetrics(phasedMessages);
  return {
    schema_version: 1,
    benchmark_kind: "synthetic-provider-context-proxy",
    fixed_case: "complex-six-read-inspection",
    comparison_point: "first implementation provider request",
    single,
    phased,
    reduction: {
      serialized_characters: single.serialized_characters - phased.serialized_characters,
      percent: Number(((1 - phased.serialized_characters / single.serialized_characters) * 100).toFixed(1)),
    },
    authoritative_evidence: {
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
          "required_checks", "failed_checks", "reviewer_findings", "missed_findings", "revision_rounds",
        ],
      },
    },
    claims: {
      provider_call_savings: false,
      provider_token_savings: false,
      billing_savings: false,
      quality_equivalence: false,
    },
  };
}

async function main() {
  const baseline = buildPhasedImplementationBaseline();
  await verifyBaselineFixture({
    baseline,
    fixturePath,
    mismatchMessage: "Phased-implementation baseline changed; inspect and recapture with --write",
    verifiedMessage: `Verified phased context proxy reduction: ${baseline.reduction.percent}%.`,
  });
}

runBaselineMain(import.meta.url, main);

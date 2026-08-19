const MAX_CONTEXT_CAPSULE_BYTES = 12 * 1024;

const CONTEXT_CAPSULE_FIELDS = new Set([
  "currentState", "decisions", "constraints", "acceptanceCriteria",
  "relevantPaths", "knownEvidence", "openQuestions", "outOfScope",
]);
const CONTEXT_CAPSULE_LISTS = [
  ["Decisions already made", "decisions", 12, 500],
  ["Constraints", "constraints", 12, 500],
  ["Acceptance criteria", "acceptanceCriteria", 16, 500],
  ["Relevant paths and symbols", "relevantPaths", 30, 300],
  ["Known evidence", "knownEvidence", 12, 500],
  ["Open questions", "openQuestions", 12, 500],
  ["Out of scope", "outOfScope", 12, 500],
];

export const contextCapsuleParameters = {
  type: "object",
  additionalProperties: false,
  description: "Bounded parent-authored recap for workers; summarize only task-relevant context and never copy the full parent transcript",
  properties: {
    currentState: { type: "string", maxLength: 3000 },
    decisions: { type: "array", maxItems: 12, items: { type: "string", maxLength: 500 } },
    constraints: { type: "array", maxItems: 12, items: { type: "string", maxLength: 500 } },
    acceptanceCriteria: { type: "array", maxItems: 16, items: { type: "string", maxLength: 500 } },
    relevantPaths: { type: "array", maxItems: 30, items: { type: "string", maxLength: 300 } },
    knownEvidence: { type: "array", maxItems: 12, items: { type: "string", maxLength: 500 } },
    openQuestions: { type: "array", maxItems: 12, items: { type: "string", maxLength: 500 } },
    outOfScope: { type: "array", maxItems: 12, items: { type: "string", maxLength: 500 } },
  },
};

function checkedCapsuleText(value, label, limit) {
  const normalized = value.trim();
  if (!normalized) throw new Error(`invalid_context_capsule_${label}`);
  if (value.includes("\0")) throw new Error(`invalid_context_capsule_${label}`);
  if (value.length > limit) throw new Error(`invalid_context_capsule_${label}`);
  return normalized;
}

function capsuleText(value, label, limit) {
  if (value === undefined) return undefined;
  if (typeof value !== "string") throw new Error(`invalid_context_capsule_${label}`);
  return checkedCapsuleText(value, label, limit);
}

function capsuleList(value, label, maxItems, itemLimit) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > maxItems) {
    throw new Error(`invalid_context_capsule_${label}`);
  }
  return value.map((item) => {
    const normalized = capsuleText(item, label, itemLimit);
    if (!normalized) throw new Error(`invalid_context_capsule_${label}`);
    return normalized;
  });
}

function validateContextCapsuleObject(value) {
  if (typeof value !== "object") throw new Error("invalid_context_capsule");
  if (Array.isArray(value)) throw new Error("invalid_context_capsule");
  const unknown = Object.keys(value).find((key) => !CONTEXT_CAPSULE_FIELDS.has(key));
  if (unknown) throw new Error("invalid_context_capsule_field");
}

function renderCapsuleList(value, [heading, key, maxItems, itemLimit]) {
  const items = capsuleList(value[key], key, maxItems, itemLimit);
  return items.length
    ? `### ${heading}\n${items.map((item) => `- ${item}`).join("\n")}`
    : undefined;
}

function contextCapsuleSections(value) {
  const sections = [];
  const currentState = capsuleText(value.currentState, "current_state", 3000);
  if (currentState) sections.push(`### Current state\n${currentState}`);
  for (const definition of CONTEXT_CAPSULE_LISTS) {
    const section = renderCapsuleList(value, definition);
    if (section) sections.push(section);
  }
  return sections;
}

export function renderContextCapsule(value) {
  if (value == null) return undefined;
  validateContextCapsuleObject(value);
  const sections = contextCapsuleSections(value);
  if (!sections.length) return undefined;
  const rendered = sections.join("\n\n");
  if (Buffer.byteLength(rendered, "utf8") > MAX_CONTEXT_CAPSULE_BYTES) {
    throw new Error("context_capsule_too_large");
  }
  return `${rendered}\n`;
}

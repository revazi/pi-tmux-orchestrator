export const ATTENTION_REASONS = Object.freeze(["clarification", "blocked", "tool_failure", "report_failure"]);
export const ATTENTION_ENTRY = "pi-tmux-orchestrator-attention-state-v1";
export const REPORT_REMINDER = "The active orchestration assignment has settled without an accepted final report or attention signal. You have one recovery provider turn: submit orchestrator_report as the final action, or orchestrator_attention if clarification, a blocker, tool failure, or report failure prevents a report. Acknowledgement is not completion. Do not wait or poll.";

export const attentionParameters = {
  type: "object", additionalProperties: false, required: ["reason"],
  properties: {
    reason: { type: "string", enum: ATTENTION_REASONS },
    summary: { type: "string", minLength: 1, maxLength: 500 },
    question: { type: "string", minLength: 1, maxLength: 500 },
  },
};

function validAttentionText(value) {
  return typeof value === "string" && Boolean(value.trim()) && [...value].length <= 500
    && !/[\u0000-\u001f\u007f-\u009f]/u.test(value)
    && !/[\ud800-\udfff]/u.test(value);
}

export function normalizeAttention(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || !Object.hasOwn(value, "reason") || !ATTENTION_REASONS.includes(value.reason)
    || Object.keys(value).some((key) => !["reason", "summary", "question"].includes(key))
    || ["summary", "question"].some((key) => Object.hasOwn(value, key) && !validAttentionText(value[key]))) {
    throw new Error("invalid_worker_attention");
  }
  return { ...value };
}

export function restoreAttentionState(entries, assignmentId) {
  const state = { recovery: false, reminderSeen: false, settlement: undefined, resumeId: null };
  if (!opaqueId(assignmentId)) return state;
  for (const entry of entries) {
    if (entry.type !== "custom" || entry.customType !== ATTENTION_ENTRY) continue;
    if (entry.data?.assignment_id !== assignmentId) continue;
    applyAttentionEntry(state, entry.data);
  }
  return state;
}

function opaqueId(value) {
  return typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
}

function applyAttentionEntry(state, data) {
  if (data.resume_id === null || opaqueId(data.resume_id)) state.resumeId = data.resume_id;
  if (typeof data.recovery === "boolean") state.recovery = data.recovery;
  if (data.reminder_seen === true) state.reminderSeen = true;
  if (opaqueId(data.settlement)) state.settlement = data.settlement;
  if (data.settlement === null) state.settlement = undefined;
}

// Pi invokes message_end before appending history or executing tool calls.
// Capture prose in memory and replace the entire accompanying assistant text /
// thinking so the attention payload never becomes a session journal entry.
export function redactAttentionMessage(message, capture) {
  if (message?.role !== "assistant" || !Array.isArray(message.content)) return undefined;
  if (!message.content.some((item) => item.type === "toolCall" && item.name === "orchestrator_attention")) return undefined;
  const content = message.content.filter((item) => item.type === "toolCall")
    .map((item) => capturedAttentionCall(item, capture));
  return { ...message, content };
}

function capturedAttentionCall(item, capture) {
  if (item.name !== "orchestrator_attention") return item;
  let attention;
  try { attention = normalizeAttention(item.arguments); } catch { /* fixed rejection at execute */ }
  capture(item.id, attention);
  return { type: "toolCall", id: item.id, name: item.name,
    arguments: { reason: attention?.reason ?? "report_failure" } };
}

export function validReminder(value, assignmentId, resumeId = null) {
  return value && ["assignment_id,id,type,version", "assignment_id,id,resume_id,type,version"].includes(Object.keys(value).sort().join(","))
    && value.version === 1 && value.type === "report_reminder"
    && (value.resume_id ?? null) === resumeId
    && typeof assignmentId === "string" && value.assignment_id === assignmentId && value.id === assignmentId;
}

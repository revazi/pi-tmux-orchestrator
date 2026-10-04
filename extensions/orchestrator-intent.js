// Stage 1 admission only: non-change work never enters the coding workflow.
export const TASK_INTENTS = ["change", "investigation", "review", "advisory"];

export function validateTaskIntent(value) {
  if (!TASK_INTENTS.includes(value)) throw new Error("invalid_task_intent");
  return value;
}

export function taskIntentMetadata(operator, recommendation = null) {
  if (operator !== undefined && operator !== null) validateTaskIntent(operator);
  if (recommendation !== null) validateTaskIntent(recommendation);
  return {
    version: 1,
    operator: operator ?? null,
    recommendation,
    effective: operator ?? recommendation ?? "change",
    source: operator != null ? "operator" : recommendation !== null ? "planner" : "default",
  };
}

export function taskIntentConfirmation(intent) {
  return `Task intent: ${intent.effective} (source=${intent.source}; operator=${intent.operator ?? "omitted"}; dynamic recommendation=${intent.recommendation ?? "none"}). Explicit operator intent wins. Only change launches the one-writer plus mandatory-review coding workflow.`;
}

export const NON_CHANGE_NOTICE = "Coding orchestration may be unnecessary for non-change work. The parent can answer directly; no implementation or change report will be created. No tmux session, broker, workers, or manifest will be started. To request repository changes, submit a fresh explicit change task.";

export async function redirectTaskIntent(ctx, intent, previewOnly = false) {
  const message = `${taskIntentConfirmation(intent)}\n\n${NON_CHANGE_NOTICE}`;
  ctx.ui.notify(message, "info");
  const direct = previewOnly || await ctx.ui.confirm("Answer directly in parent?", message);
  return {
    schema_version: "1",
    error: null,
    command: "start",
    success: true,
    data: {
      task_intent: intent,
      disposition: direct ? "direct-parent" : "cancelled",
      launched: false,
      dry_run: previewOnly,
      message: NON_CHANGE_NOTICE,
    },
  };
}

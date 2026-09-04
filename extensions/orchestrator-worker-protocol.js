import { randomBytes } from "node:crypto";

export const WORKER_PROTOCOL_VERSION = 1;
export const MAX_WORKER_FRAME_BYTES = 256 * 1024;
export const DELIVERY_ENTRY = "pi-tmux-orchestrator-delivery-v1";
export const BOUNDARY_ENTRY = "pi-tmux-orchestrator-context-boundary-v1";
export const GUARDRAIL_ENTRY = "pi-tmux-orchestrator-guardrail-v1";
export const RESULT_VOLUME_ENTRY = "pi-tmux-orchestrator-result-volume-v1";
export const WORKER_MESSAGE_TYPE = "pi-tmux-orchestrator-message-v1";

const ROLES = new Set(["implementer", "reviewer", "probe", "playwright", "django"]);

function id() {
  return randomBytes(16).toString("hex");
}

export function validWorkerEnvironment({
  role,
  token,
  socketPath,
  generation,
  guardrailPolicy,
}) {
  const checks = [
    ROLES.has(role),
    /^[a-f0-9]{32}$/.test(token || ""),
    Boolean(socketPath),
    Number.isInteger(generation) && generation > 0,
    guardrailPolicy !== undefined,
  ];
  return checks.every(Boolean);
}

export function workerFrame(value) {
  const payload = Buffer.from(JSON.stringify(value), "utf8");
  if (!payload.length || payload.length > MAX_WORKER_FRAME_BYTES) {
    throw new Error("broker_frame_too_large");
  }
  const prefix = Buffer.allocUnsafe(4);
  prefix.writeUInt32BE(payload.length);
  return Buffer.concat([prefix, payload]);
}

export function createWorkerMessage(role, token) {
  return (type, extra = {}) => ({
    version: WORKER_PROTOCOL_VERSION,
    type,
    role,
    token,
    id: id(),
    ...extra,
  });
}

export function deliveryOptions(trigger) {
  return { triggerTurn: trigger, deliverAs: "followUp" };
}

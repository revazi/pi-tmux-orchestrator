import { lstat, readFile } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import { join } from "node:path";
import net from "node:net";
import {
  BROKER_PROTOCOL_VERSION,
  brokerFrame,
  consumeObserverFrames,
  validateObserverFrame,
} from "./orchestrator-parent-protocol.js";
import {
  PARENT_MESSAGE_TYPE,
  parentProgressContent,
  parentUpdateContent,
} from "./orchestrator-parent-content.js";

export {
  brokerFrame,
  PARENT_MESSAGE_TYPE,
  parentProgressContent,
  parentUpdateContent,
  validateObserverFrame,
};

function brokerId() {
  return randomBytes(16).toString("hex");
}

function invalidUnless(condition, code) {
  if (!condition) throw new Error(code);
}

function nonemptyString(value) {
  return typeof value === "string" && Boolean(value);
}

function validObserverPaths(coordination, socketPath, session) {
  return [
    nonemptyString(coordination),
    nonemptyString(socketPath),
    nonemptyString(session),
    session?.length <= 128,
  ].every(Boolean);
}

function tokenOwnerMatches(metadata) {
  return typeof process.getuid !== "function" || metadata.uid === process.getuid();
}

function safeTokenMetadata(metadata) {
  return [
    metadata.isFile(),
    !metadata.isSymbolicLink(),
    metadata.size <= 128,
    (metadata.mode & 0o077) === 0,
    tokenOwnerMatches(metadata),
  ].every(Boolean);
}

async function readObserverIdentity(envelope) {
  const coordination = envelope.data?.paths?.coordination;
  const socketPath = envelope.data?.paths?.observer_socket;
  const session = envelope.data?.session;
  invalidUnless(validObserverPaths(coordination, socketPath, session), "observer_paths_unavailable");
  const tokenPath = join(coordination, "control.token");
  invalidUnless(safeTokenMetadata(await lstat(tokenPath)), "observer_token_unsafe");
  const token = (await readFile(tokenPath, "ascii")).trim();
  invalidUnless(/^[a-f0-9]{32}$/.test(token), "observer_token_invalid");
  return { session, socketPath, token };
}

export async function attachParentObserver(pi, envelope, observer, onStop, options = {}) {
  const identity = await readObserverIdentity(envelope);
  const triggerInitialActionable = options.triggerInitialActionable !== false;
  if (observer.closed) {
    onStop();
    throw new Error("observer_closed");
  }
  const reports = [];
  const reportIds = new Set();
  const roleStates = new Map();
  let workflowState = "starting";
  let round = 1;
  let retryCount = 0;
  let attentionNotified = false;
  let snapshotSeen = false;
  let attachmentNotified = false;
  let readySettled = false;
  let resolveReady;
  let rejectReady;
  const ready = new Promise((resolve, reject) => {
    resolveReady = resolve;
    rejectReady = reject;
  });

  function settleReady(error) {
    if (readySettled) return;
    readySettled = true;
    if (error) rejectReady(error);
    else resolveReady({ session: identity.session });
  }

  function clearObserverTimer() {
    if (observer.timer) clearTimeout(observer.timer);
  }

  function destroyObserverSocket() {
    if (observer.socket) observer.socket.destroy();
  }

  function stopError(error) {
    return error || new Error("observer_stopped_before_ready");
  }

  function stop(error) {
    if (observer.closed) return;
    observer.closed = true;
    clearObserverTimer();
    destroyObserverSocket();
    settleReady(stopError(error));
    onStop();
  }
  observer.stop = stop;

  function currentRoles() {
    return [...roleStates].map(([role, state]) => ({ role, state }));
  }

  function notifyProgress(update) {
    try {
      pi.sendMessage(
        {
          customType: PARENT_MESSAGE_TYPE,
          content: parentProgressContent(
            identity.session,
            workflowState,
            round,
            currentRoles(),
            update,
          ),
          display: true,
          details: {
            session: identity.session,
            state: workflowState,
            round,
            event: update.kind,
            role: update.role || null,
          },
        },
        { triggerTurn: false, deliverAs: "steer" },
      );
    } catch (error) {
      stop(error instanceof Error ? error : new Error("parent_progress_failed"));
    }
  }

  function notifyParent(state) {
    const update = parentUpdateContent(
      identity.session,
      state,
      round,
      reports,
      currentRoles(),
    );
    try {
      pi.sendMessage(
        {
          customType: PARENT_MESSAGE_TYPE,
          content: update.content,
          display: true,
          details: {
            session: identity.session,
            state,
            round,
            report_roles: update.reports.map((event) => event.role),
            omitted_reports: update.omitted,
          },
        },
        { triggerTurn: true, deliverAs: "steer" },
      );
    } catch (error) {
      stop(error instanceof Error ? error : new Error("parent_notification_failed"));
    }
  }

  function acceptActiveWorkflow(changed) {
    attentionNotified = false;
    if (snapshotSeen && changed) notifyProgress({ kind: "workflow" });
  }

  function acceptAttentionWorkflow(state, allowActionableTurn) {
    if (attentionNotified) return;
    attentionNotified = true;
    if (allowActionableTurn) notifyParent(state);
    else notifyProgress({ kind: "existing_actionable" });
  }

  function acceptTerminalWorkflow(state, allowActionableTurn) {
    if (allowActionableTurn) notifyParent(state);
    else notifyProgress({ kind: "existing_actionable" });
    stop();
  }

  const workflowHandlers = new Map([
    ["active", (state, allowActionableTurn, changed) => acceptActiveWorkflow(changed)],
    ["needs_attention", acceptAttentionWorkflow],
    ["ready", acceptTerminalWorkflow],
    ["uncertain", acceptTerminalWorkflow],
  ]);

  function acceptWorkflow(state, valueRound, allowActionableTurn = true) {
    const changed = workflowState !== state || round !== valueRound;
    workflowState = state;
    round = valueRound;
    workflowHandlers.get(state)?.(state, allowActionableTurn, changed);
  }

  function acceptReport(value) {
    if (reportIds.has(value.id)) return;
    invalidUnless(reports.length < 100, "too_many_observer_reports");
    reportIds.add(value.id);
    reports.push(value);
    if (snapshotSeen) {
      notifyProgress({ kind: "report", role: value.role, reportRound: value.round });
    }
  }

  function shouldNotifyAttachment(replayLost, state) {
    return [
      !attachmentNotified,
      !replayLost,
      !["ready", "uncertain", "needs_attention"].includes(state),
    ].every(Boolean);
  }

  function acceptFirstSnapshot(value, replayLost) {
    workflowState = value.state;
    round = value.round;
    if (!shouldNotifyAttachment(replayLost, value.state)) return;
    attachmentNotified = true;
    notifyProgress({ kind: "attached" });
  }

  function snapshotReplayLost(value) {
    return [
      !value.report_replay_complete,
      reportIds.size < value.report_count,
    ].every(Boolean);
  }

  function snapshotWorkflowState(value, replayLost) {
    return replayLost ? "uncertain" : value.state;
  }

  function snapshotAllowsActionableTurn(firstSnapshot) {
    return firstSnapshot ? triggerInitialActionable : true;
  }

  function acceptSnapshot(value) {
    retryCount = 0;
    const firstSnapshot = !snapshotSeen;
    for (const item of value.roles) roleStates.set(item.role, item.state);
    snapshotSeen = true;
    const replayLost = snapshotReplayLost(value);
    if (firstSnapshot) acceptFirstSnapshot(value, replayLost);
    acceptWorkflow(
      snapshotWorkflowState(value, replayLost),
      value.round,
      snapshotAllowsActionableTurn(firstSnapshot),
    );
  }

  function acceptLifecycle(value) {
    const changed = roleStates.get(value.role) !== value.state;
    roleStates.set(value.role, value.state);
    if (snapshotSeen && changed) {
      notifyProgress({ kind: "lifecycle", role: value.role, workerState: value.state });
    }
  }

  function acceptResponse(connection) {
    connection.acknowledged = true;
    settleReady();
  }

  const frameHandlers = new Map([
    ["report", acceptReport],
    ["snapshot", acceptSnapshot],
    ["lifecycle", acceptLifecycle],
    ["workflow", (value) => acceptWorkflow(value.state, value.round)],
  ]);

  function acceptFrame(rawValue, requestId, connection) {
    if (observer.closed) return;
    const value = validateObserverFrame(rawValue, identity.session, requestId);
    if (value.type === "response") return acceptResponse(connection);
    invalidUnless(connection.acknowledged, "observer_event_before_response");
    frameHandlers.get(value.type)(value);
  }

  function scheduleReconnect() {
    if (observer.closed) return;
    retryCount += 1;
    if (retryCount > 10) {
      workflowState = "uncertain";
      const error = new Error("observer_connection_uncertain");
      try {
        notifyParent("uncertain");
      } finally {
        stop(error);
      }
      return;
    }
    const delay = Math.min(100 * (2 ** (retryCount - 1)), 5000);
    observer.timer = setTimeout(connect, delay);
  }

  function connect() {
    if (observer.closed) return;
    const requestId = brokerId();
    const connection = { acknowledged: false, buffer: Buffer.alloc(0) };
    const socket = net.createConnection({ path: identity.socketPath });
    observer.socket = socket;
    socket.on("connect", () => {
      socket.write(brokerFrame({
        version: BROKER_PROTOCOL_VERSION,
        type: "observe",
        token: identity.token,
        id: requestId,
      }));
    });
    socket.on("data", (chunk) => {
      try {
        consumeObserverFrames(
          connection,
          chunk,
          (value) => acceptFrame(value, requestId, connection),
        );
      } catch {
        socket.destroy();
      }
    });
    socket.on("error", () => {});
    socket.on("close", () => {
      if (observer.socket === socket) observer.socket = undefined;
      if (!observer.closed) scheduleReconnect();
    });
  }

  connect();
  return { get state() { return workflowState; }, ready, stop };
}

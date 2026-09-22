const TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone";
const TYPESAFE_MODEL = "jev-latest";
const TYPESAFE_TIMEOUT_MS = 60_000;
const MAX_TYPESAFE_REQUEST_BYTES = 96 * 1024;
const MAX_TYPESAFE_RESPONSE_BYTES = 512 * 1024;

function utf8Bytes(value) {
  return Buffer.byteLength(value, "utf8");
}

export function normalizedTypeSafeApiKey(value) {
  if (value === undefined || value === null || value === "") return undefined;
  if (typeof value !== "string") throw new Error("invalid_typesafe_api_key");
  const key = value.trim();
  if (!key || key.length > 4096 || /[^\x21-\x7e]/.test(key)) {
    throw new Error("invalid_typesafe_api_key");
  }
  return key;
}

function validateContentLength(headers) {
  const raw = headers?.get?.("content-length");
  if (raw === null || raw === undefined) return;
  const length = Number(raw);
  if (!Number.isSafeInteger(length) || length < 0 || length > MAX_TYPESAFE_RESPONSE_BYTES) {
    throw new Error("typesafe_response_invalid_size");
  }
}

async function readResponseChunks(body) {
  if (!body || typeof body.getReader !== "function") {
    throw new Error("typesafe_response_unreadable");
  }
  const reader = body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const item = await reader.read();
      if (item.done) return { chunks, size };
      if (!(item.value instanceof Uint8Array)) {
        throw new Error("typesafe_response_unreadable");
      }
      size += item.value.byteLength;
      if (size > MAX_TYPESAFE_RESPONSE_BYTES) {
        void reader.cancel().catch(() => {});
        throw new Error("typesafe_response_invalid_size");
      }
      chunks.push(item.value);
    }
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("typesafe_")) throw error;
    throw new Error("typesafe_response_unreadable");
  }
}

function decodeResponse(chunks, size) {
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error("typesafe_response_invalid_utf8");
  }
}

async function boundedResponseText(response) {
  validateContentLength(response.headers);
  const result = await readResponseChunks(response.body);
  return decodeResponse(result.chunks, result.size);
}

function typesafeStatusError(status) {
  if (status === 401 || status === 403) return "typesafe_authentication_failed";
  if (status === 408 || status === 429) return "typesafe_rate_limited";
  return "typesafe_request_failed";
}

function serializedRequest(body) {
  const serialized = JSON.stringify(body);
  if (!serialized || utf8Bytes(serialized) > MAX_TYPESAFE_REQUEST_BYTES) {
    throw new Error("typesafe_request_invalid_size");
  }
  return serialized;
}

function requestAbort(options) {
  const timeoutMs = options.timeoutMs ?? TYPESAFE_TIMEOUT_MS;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > TYPESAFE_TIMEOUT_MS) {
    throw new Error("invalid_typesafe_timeout");
  }
  const controller = new AbortController();
  const state = { timedOut: false };
  const abort = () => controller.abort();
  const timeout = setTimeout(() => {
    state.timedOut = true;
    abort();
  }, timeoutMs);
  if (options.signal?.aborted) abort();
  else options.signal?.addEventListener?.("abort", abort, { once: true });
  return {
    signal: controller.signal,
    state,
    close() {
      clearTimeout(timeout);
      options.signal?.removeEventListener?.("abort", abort);
    },
  };
}

async function fetchTypeSafe(fetchImpl, apiKey, serialized, abort, callerSignal) {
  try {
    return await fetchImpl(TYPESAFE_ENDPOINT, {
      method: "POST",
      headers: {
        accept: "application/json",
        authorization: `Bearer ${apiKey}`,
        "content-type": "application/json",
      },
      body: serialized,
      redirect: "error",
      signal: abort.signal,
    });
  } catch {
    if (abort.state.timedOut) throw new Error("typesafe_request_timeout");
    if (callerSignal?.aborted) throw new Error("typesafe_request_aborted");
    throw new Error("typesafe_transport_failed");
  }
}

function validateHttpResponse(response) {
  if (!response || typeof response.status !== "number" || typeof response.ok !== "boolean") {
    throw new Error("typesafe_response_invalid");
  }
  if (!response.ok) throw new Error(typesafeStatusError(response.status));
}

async function parsedResponse(response) {
  const text = await boundedResponseText(response);
  if (!text) throw new Error("typesafe_response_invalid_size");
  try {
    return JSON.parse(text);
  } catch {
    throw new Error("typesafe_response_not_json");
  }
}

export async function requestTypeSafe(body, options = {}) {
  const apiKey = normalizedTypeSafeApiKey(options.apiKey);
  if (!apiKey) throw new Error("typesafe_api_key_unavailable");
  const serialized = serializedRequest(body);
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  if (typeof fetchImpl !== "function") throw new Error("typesafe_transport_unavailable");
  const abort = requestAbort(options);
  try {
    if (options.signal?.aborted) throw new Error("typesafe_request_aborted");
    const response = await fetchTypeSafe(
      fetchImpl,
      apiKey,
      serialized,
      abort,
      options.signal,
    );
    validateHttpResponse(response);
    return await parsedResponse(response);
  } catch (error) {
    if (abort.state.timedOut) throw new Error("typesafe_request_timeout");
    if (options.signal?.aborted) throw new Error("typesafe_request_aborted");
    throw error;
  } finally {
    abort.close();
  }
}

export const typesafeTestHooks = {
  MAX_TYPESAFE_REQUEST_BYTES,
  MAX_TYPESAFE_RESPONSE_BYTES,
  TYPESAFE_ENDPOINT,
  TYPESAFE_MODEL,
  TYPESAFE_TIMEOUT_MS,
};

export { TYPESAFE_MODEL };

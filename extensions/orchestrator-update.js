import { readFile } from "node:fs/promises";

const PACKAGE_NAME = "pi-tmux-orchestrator";
const PRODUCT_NAME = "Pi Tmux Orchestrator";
const NPM_URL = `https://www.npmjs.com/package/${PACKAGE_NAME}`;
const REPOSITORY_URL = "https://github.com/revazi/pi-tmux-orchestrator";
const RELEASES_URL = `${REPOSITORY_URL}/releases`;
const ISSUES_URL = `${REPOSITORY_URL}/issues`;
const UPDATE_COMMAND = `pi update npm:${PACKAGE_NAME}`;
const DISABLE_UPDATE_ENV = "PI_TMUX_ORCHESTRATOR_DISABLE_UPDATE_NOTICE";
const NPM_LATEST_URL = `https://registry.npmjs.org/${PACKAGE_NAME}/latest`;
const UPDATE_CHECK_TIMEOUT_MS = 1_500;
const LATEST_VERSION_CACHE_MS = 6 * 60 * 60 * 1_000;
const VERSION_PATTERN = /^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

let currentVersionPromise;
let latestVersionCache;
let latestVersionPromise;
let updateNoticeShown = false;

export function scheduleOrchestratorUpdateNotice(ctx) {
  if (!shouldCheckForUpdate(ctx)) return;
  updateNoticeShown = true;
  void getOrchestratorVersionInfo()
    .then((info) => {
      if (!info.updateAvailable || !info.latestVersion) return;
      ctx.ui?.notify(buildShortUpdateNotice(info), "warning");
    })
    .catch(() => {
      // Update checks are best-effort and must never affect Pi startup.
    });
}

export async function getOrchestratorAboutSummary() {
  const currentVersion = await getCurrentVersion();
  const latestVersion = latestVersionCache?.expiresAt > Date.now()
    ? latestVersionCache.value
    : undefined;
  return {
    currentVersion,
    latestVersion,
    updateAvailable: isUpdateAvailable(currentVersion, latestVersion),
    npmUrl: NPM_URL,
    repositoryUrl: REPOSITORY_URL,
    issuesUrl: ISSUES_URL,
    updateCommand: UPDATE_COMMAND,
  };
}

function shouldCheckForUpdate(ctx) {
  return Boolean(
    ctx?.hasUI
    && ctx.mode === "tui"
    && !updateNoticeShown
    && !isUpdateNoticeDisabled()
    && process.env.PI_TMUX_ORCHESTRATOR_ROLE === undefined
    && process.env.PI_TMUX_CONTROLLER !== "1"
  );
}

async function getOrchestratorVersionInfo(options = {}) {
  const currentVersion = await getCurrentVersion();
  const checkedAt = new Date().toISOString();
  const latestVersion = await getLatestVersion(options).catch(() => undefined);
  return {
    packageName: PACKAGE_NAME,
    currentVersion,
    latestVersion,
    updateAvailable: isUpdateAvailable(currentVersion, latestVersion),
    npmUrl: NPM_URL,
    repositoryUrl: REPOSITORY_URL,
    releasesUrl: RELEASES_URL,
    issuesUrl: ISSUES_URL,
    updateCommand: UPDATE_COMMAND,
    disableEnv: DISABLE_UPDATE_ENV,
    checkedAt,
    error: latestVersion ? undefined : "Latest npm version unavailable.",
  };
}

async function getCurrentVersion() {
  currentVersionPromise ??= readCurrentVersion();
  return currentVersionPromise;
}

async function readCurrentVersion() {
  try {
    const packageJsonPath = new URL("../package.json", import.meta.url);
    const packageJson = JSON.parse(await readFile(packageJsonPath, "utf8"));
    if (isVersion(packageJson?.version)) return packageJson.version;
  } catch {
    // Fall through to the bounded unknown marker.
  }
  return "unknown";
}

async function getLatestVersion(options) {
  const now = Date.now();
  if (!options.forceRefresh && latestVersionCache?.expiresAt > now) {
    return latestVersionCache.value;
  }
  const value = await resolveLatestVersion();
  if (value) latestVersionCache = { value, expiresAt: now + LATEST_VERSION_CACHE_MS };
  return value;
}

async function resolveLatestVersion() {
  latestVersionPromise ??= fetchLatestVersion();
  try {
    return await latestVersionPromise;
  } finally {
    latestVersionPromise = undefined;
  }
}

async function fetchLatestVersion() {
  if (typeof fetch !== "function") return undefined;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), UPDATE_CHECK_TIMEOUT_MS);
  timeout.unref?.();
  try {
    const response = await fetch(NPM_LATEST_URL, {
      signal: controller.signal,
      headers: { accept: "application/json" },
    });
    if (!response.ok) return undefined;
    const data = await response.json();
    return isVersion(data?.version) ? data.version : undefined;
  } finally {
    clearTimeout(timeout);
  }
}

function isVersion(value) {
  return typeof value === "string" && value.length <= 64 && VERSION_PATTERN.test(value);
}

function isUpdateNoticeDisabled() {
  const disabled = process.env[DISABLE_UPDATE_ENV];
  if (disabled !== undefined) return !isFalseLike(disabled);
  const legacyToggle = process.env.PI_TMUX_ORCHESTRATOR_UPDATE_CHECK;
  return legacyToggle !== undefined && isFalseLike(legacyToggle);
}

function isFalseLike(value) {
  return ["0", "false", "off", "no"].includes(String(value).toLowerCase());
}

function isUpdateAvailable(currentVersion, latestVersion) {
  if (!latestVersion || currentVersion === "unknown") return false;
  return compareVersions(latestVersion, currentVersion) > 0;
}

function compareVersions(left, right) {
  const leftParts = parseVersionParts(left);
  const rightParts = parseVersionParts(right);
  for (let index = 0; index < Math.max(leftParts.length, rightParts.length); index += 1) {
    const difference = (leftParts[index] ?? 0) - (rightParts[index] ?? 0);
    if (difference !== 0) return Math.sign(difference);
  }
  const leftPrerelease = left.includes("-");
  const rightPrerelease = right.includes("-");
  if (leftPrerelease === rightPrerelease) return 0;
  return leftPrerelease ? -1 : 1;
}

function parseVersionParts(version) {
  return version
    .split("-", 1)[0]
    .split(".")
    .map((part) => Number.parseInt(part, 10));
}

function buildShortUpdateNotice(info) {
  return `${PRODUCT_NAME} ${info.latestVersion} is available (you have ${info.currentVersion}). Update: ${info.updateCommand}. Details: /or-dashboard`;
}

export const updateTestHooks = {
  compareVersions,
  getOrchestratorVersionInfo,
  reset() {
    currentVersionPromise = undefined;
    latestVersionCache = undefined;
    latestVersionPromise = undefined;
    updateNoticeShown = false;
  },
};

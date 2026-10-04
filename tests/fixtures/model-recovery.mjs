// Provider-free parent tool adapter against the real bundled CLI and tmux broker.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import extension from "../../extensions/tmux-orchestrator.js";
const exec = promisify(execFile);
let tool;
const pi = {
  registerProvider() {}, registerCommand() {}, registerShortcut() {}, on() {},
  registerTool(value) { tool = value; },
  async exec(command, args, options) {
    try {
      const result = await exec(command, args, { ...options, maxBuffer: 512 * 1024 });
      return { code: 0, stdout: result.stdout };
    } catch (error) {
      return { code: typeof error.code === "number" ? error.code : 1, stdout: error.stdout || "" };
    }
  },
};
extension(pi);
const confirmations = [];
const ctx = {
  mode: process.argv[2], hasUI: true, cwd: process.cwd(),
  modelRegistry: { getAvailable: () => [] },
  ui: {
    notify() {},
    async confirm(title, message) {
      confirmations.push({ title, message });
      return process.argv[4] === "yes";
    },
  },
};
try {
  const result = await tool.execute("synthetic-call", JSON.parse(process.argv[3]), undefined, undefined, ctx);
  console.log(JSON.stringify({ result, confirmations }));
} catch (error) {
  console.log(JSON.stringify({ error: error.message, confirmations }));
}

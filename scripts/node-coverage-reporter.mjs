import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

function lineLocation(sourceLines, line) {
  return {
    start: { line, column: 0 },
    end: { line, column: sourceLines[line - 1]?.length ?? 0 },
  };
}

function mergedItems(files, property, identity) {
  const merged = new Map();
  for (const file of files) {
    const occurrences = new Map();
    for (const item of file[property]) {
      const base = identity(item);
      const occurrence = occurrences.get(base) ?? 0;
      occurrences.set(base, occurrence + 1);
      const key = `${base}\u0000${occurrence}`;
      const current = merged.get(key);
      if (current) current.count += item.count;
      else merged.set(key, { ...item });
    }
  }
  return [...merged.values()].sort((left, right) =>
    left.line - right.line || String(left.name ?? "").localeCompare(String(right.name ?? "")),
  );
}

function groupedFiles(summary) {
  const groups = new Map();
  for (const file of summary.files) {
    const path = resolve(file.path);
    const group = groups.get(path) ?? [];
    group.push(file);
    groups.set(path, group);
  }
  return groups;
}

function fileCoverage(path, files) {
  const sourceLines = readFileSync(path, "utf8").split(/\r?\n/);
  const statements = mergedItems(files, "lines", (item) => String(item.line));
  const functions = mergedItems(
    files,
    "functions",
    (item) => `${item.line}\u0000${item.name}`,
  );
  const branches = mergedItems(files, "branches", (item) => String(item.line));
  const statementMap = {};
  const statementCounts = {};
  const fnMap = {};
  const functionCounts = {};
  const branchMap = {};
  const branchCounts = {};

  statements.forEach((item, id) => {
    statementMap[id] = lineLocation(sourceLines, item.line);
    statementCounts[id] = item.count;
  });
  functions.forEach((item, id) => {
    const location = lineLocation(sourceLines, item.line);
    fnMap[id] = {
      name: item.name || `(anonymous_${id})`,
      decl: location,
      loc: location,
      line: item.line,
    };
    functionCounts[id] = item.count;
  });
  branches.forEach((item, id) => {
    const location = lineLocation(sourceLines, item.line);
    branchMap[id] = {
      loc: location,
      line: item.line,
      type: "branch",
      locations: [location],
    };
    branchCounts[id] = [item.count];
  });

  return {
    path,
    statementMap,
    s: statementCounts,
    fnMap,
    f: functionCounts,
    branchMap,
    b: branchCounts,
  };
}

export function istanbulCoverage(summary) {
  if (!summary || !Array.isArray(summary.files)) {
    throw new Error("Node test coverage summary is unavailable");
  }
  const coverage = {};
  for (const [path, files] of groupedFiles(summary)) {
    coverage[path] = fileCoverage(path, files);
  }
  return coverage;
}

function writeCoverage(path, summary) {
  const output = resolve(path);
  const temporary = `${output}.${process.pid}.tmp`;
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(temporary, `${JSON.stringify(istanbulCoverage(summary))}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  renameSync(temporary, output);
}

export default async function* coverageReporter(source) {
  const output = process.env.PI_TMUX_COVERAGE_FILE;
  if (!output) throw new Error("PI_TMUX_COVERAGE_FILE is required");
  for await (const event of source) {
    if (event.type === "test:coverage") writeCoverage(output, event.data.summary);
    if (event.type === "test:fail") {
      yield `not ok - ${event.data.name ?? "test failure"}\n`;
    }
  }
}

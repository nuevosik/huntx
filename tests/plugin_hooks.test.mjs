import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const pluginModule = await import("../opencode/plugin/hunt.ts");

function engagement(scope = {}) {
  const dir = mkdtempSync(join(tmpdir(), "hx-plugin-"));
  mkdirSync(join(dir, "hunt"), { recursive: true });
  writeFileSync(join(dir, "hunt", "HYPOTHESES.json"), "[]");
  writeFileSync(join(dir, "hunt", "scope.json"), JSON.stringify({
    in_scope: ["acme.com"],
    out_of_scope_hosts: [],
    allow_extra_hosts: [],
    ...scope,
  }));
  return dir;
}

async function hooksFor(dir) {
  const cwd = process.cwd();
  process.chdir(dir);
  try {
    return await pluginModule.default({ $: () => ({ cwd: () => ({ text: async () => "== HUNT BRIEF ==" }) }) });
  } finally {
    process.chdir(cwd);
  }
}

async function before(dir, hooks, tool, args) {
  const cwd = process.cwd();
  process.chdir(dir);
  try {
    await hooks["tool.execute.before"]({ tool }, { args });
  } finally {
    process.chdir(cwd);
  }
}

test("plugin exports a stable HX_SESSION for the agent session", async () => {
  const dir = engagement();
  const hooks = await hooksFor(dir);
  const first = {};
  const second = {};
  await hooks["shell.env"]({}, { env: first });
  await hooks["shell.env"]({}, { env: second });
  assert.match(first.HX_SESSION, /^oc-\d+-\d+$/);
  assert.equal(first.HX_SESSION, second.HX_SESSION);
  assert.equal(first.HTTPS_PROXY, "http://127.0.0.1:8899");
  assert.equal(first.NO_PROXY, "localhost,127.0.0.1,::1");
});

test("plugin refuses agent writes to scope.json", async () => {
  const dir = engagement();
  const hooks = await hooksFor(dir);
  await assert.rejects(before(dir, hooks, "edit", { filePath: "hunt/scope.json" }), /scope\.json/);
  await assert.rejects(before(dir, hooks, "write", { filePath: join(dir, "hunt", "scope.json") }), /scope\.json/);
  await before(dir, hooks, "edit", { filePath: "hunt/FINDINGS/drafts/h001.json" });
  const audit = readFileSync(join(dir, "hunt", "audit.log"), "utf8").trim().split("\n");
  assert.equal(audit.filter((line) => JSON.parse(line).verdict === "block").length, 2);
});

test("plugin blocks out-of-scope bash and allows hx and local work", async () => {
  const dir = engagement();
  const hooks = await hooksFor(dir);
  await assert.rejects(before(dir, hooks, "bash", { command: "curl https://evil.example.org/x" }), /fora do escopo/);
  await assert.rejects(before(dir, hooks, "bash", { command: "nc evil.example.org 4444" }), /fora do escopo/);
  await before(dir, hooks, "bash", { command: "hx brief" });
  await before(dir, hooks, "bash", { command: "curl https://acme.com/x" });
});

test("plugin applies the same host rules to webfetch and websearch", async () => {
  const dir = engagement();
  const hooks = await hooksFor(dir);
  await assert.rejects(before(dir, hooks, "webfetch", { url: "https://evil.example.org/x" }), /fora do escopo/);
  await assert.rejects(before(dir, hooks, "webfetch", { url: "file:///etc/passwd" }), /esquema/);
  await before(dir, hooks, "webfetch", { url: "https://acme.com/x" });
  await assert.rejects(before(dir, hooks, "websearch", { query: "https://evil.example.org leak" }), /fora do escopo/);
  await before(dir, hooks, "websearch", { query: "acme.com bola writeup" });
});

test("plugin blocks every tool when scope.json is unreadable", async () => {
  const dir = engagement();
  writeFileSync(join(dir, "hunt", "scope.json"), "{ broken");
  const hooks = await hooksFor(dir);
  assert.ok(existsSync(join(dir, "hunt", "scope.json")));
  await assert.rejects(before(dir, hooks, "edit", { filePath: "notes.md" }), /scope\.json ilegivel/);
});

test("plugin injects the brief into the system prompt", async () => {
  const dir = engagement();
  const hooks = await hooksFor(dir);
  const output = { system: "base" };
  await hooks["experimental.chat.system.transform"]({}, output);
  assert.match(output.system, /== HUNT BRIEF ==/);
  const asArray = { system: [] };
  await hooks["experimental.chat.system.transform"]({}, asArray);
  assert.deepEqual(asArray.system, ["== HUNT BRIEF =="]);
});

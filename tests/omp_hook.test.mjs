import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const hookModule = await import("../runner/omp/guard-hook.ts");

function engagement(scope = {}) {
  const dir = mkdtempSync(join(tmpdir(), "hx-omp-hook-"));
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

function hookFor() {
  let handler;
  hookModule.default({ on: (event, fn) => { if (event === "tool_call") handler = fn; } });
  assert.ok(handler, "handler tool_call registrado");
  return handler;
}

async function call(hook, dir, toolName, input) {
  return hook({ toolName, input }, { cwd: dir });
}

test("headless slice mode allows hx and blocks everything else", async () => {
  const dir = engagement();
  const hook = hookFor();
  assert.equal(await call(hook, dir, "bash", { command: "hx brief" }), undefined);
  const blocked = await call(hook, dir, "bash", { command: "curl https://acme.com/x" });
  assert.equal(blocked.block, true);
  assert.match(blocked.reason, /hx run/);
  const substituted = await call(hook, dir, "bash", { command: "hx run GET https://acme.com/$(id)" });
  assert.equal(substituted.block, true);
});

test("guard mode applies the shared tripwire to bash", async () => {
  const dir = engagement();
  const hook = hookFor();
  process.env.HX_OMP_BASH_MODE = "guard";
  try {
    assert.equal(await call(hook, dir, "bash", { command: "curl https://acme.com/x" }), undefined);
    assert.equal((await call(hook, dir, "bash", { command: "nc evil.example.org 4444" })).block, true);
    assert.equal((await call(hook, dir, "bash", { command: "git clone git@evil.example.org:r.git" })).block, true);
  } finally {
    delete process.env.HX_OMP_BASH_MODE;
  }
});

test("plan mode only enqueues hypotheses and never writes", async () => {
  const dir = engagement();
  const hook = hookFor();
  process.env.HX_OMP_BASH_MODE = "plan";
  try {
    assert.equal(await call(hook, dir, "bash", { command: "hx hypothesis add --claim x --endpoint y --class z --confirm c --refute r" }), undefined);
    assert.equal((await call(hook, dir, "bash", { command: "hx next" })).block, true);
    assert.equal((await call(hook, dir, "bash", { command: "hx run GET https://acme.com/x" })).block, true);
    assert.equal((await call(hook, dir, "edit", { filePath: "notes.md" })).block, true);
  } finally {
    delete process.env.HX_OMP_BASH_MODE;
  }
});

test("scope.json is immutable and audit is written", async () => {
  const dir = engagement();
  const hook = hookFor();
  assert.equal((await call(hook, dir, "write", { filePath: "hunt/scope.json" })).block, true);
  assert.equal(await call(hook, dir, "write", { filePath: "hunt/FINDINGS/drafts/h001.json" }), undefined);
  const lines = readFileSync(join(dir, "hunt", "audit.log"), "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(lines.length, 1);
  assert.equal(lines[0].actor, "omp-hook");
  assert.equal(lines[0].verdict, "block");
  assert.match(lines[0].reason, /scope\.json/);
});

test("unreadable scope fails closed and non-engagements are ignored", async () => {
  const broken = engagement();
  writeFileSync(join(broken, "hunt", "scope.json"), "{ broken");
  const hook = hookFor();
  assert.equal((await call(hook, broken, "bash", { command: "hx brief" })).block, true);
  const plain = mkdtempSync(join(tmpdir(), "hx-plain-"));
  assert.equal(await call(hook, plain, "bash", { command: "curl https://evil.example.org" }), undefined);
});

test("headless slice mode rejects chained non-hx segments", async () => {
  const dir = engagement();
  const hook = hookFor();
  assert.equal((await call(hook, dir, "bash", { command: "hx brief; cat hunt/scope.json" })).block, true);
  assert.equal((await call(hook, dir, "bash", { command: "hx brief && curl https://evil.example.org" })).block, true);
  assert.equal((await call(hook, dir, "bash", { command: "hx brief | tee /tmp/x" })).block, true);
  assert.equal(await call(hook, dir, "bash", { command: "hx result h001 --verdict blocked --note x" }), undefined);
});

test("separators inside quoted notes do not trip the slice rule", async () => {
  const dir = engagement();
  const hook = hookFor();
  assert.equal(
    await call(hook, dir, "bash", { command: "hx result h001 --verdict blocked --note \"victim 200; control 403 | sem marcador\"" }),
    undefined,
  );
  assert.equal(
    (await call(hook, dir, "bash", { command: "hx result h001 --note 'a; b' && curl https://evil.example.org" })).block,
    true,
  );
  assert.equal((await call(hook, dir, "bash", { command: "hx result h001 --note 'a; b' | tee /tmp/x" })).block, true);
});

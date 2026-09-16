import type { Plugin } from "@opencode-ai/plugin"
import { decideBash, decideUrl, extractHosts, hostAllowed } from "./lib/guard.js"
import { appendFileSync, existsSync, readFileSync } from "node:fs"
import { join, resolve } from "node:path"

function engagementDir(): string | null {
  let dir = resolve(process.cwd())
  for (;;) {
    if (existsSync(join(dir, "hunt", "HYPOTHESES.json"))) return dir
    const parent = resolve(dir, "..")
    if (parent === dir) return null
    dir = parent
  }
}

function scopeOf(dir: string): any | null {
  try {
    return JSON.parse(readFileSync(join(dir, "hunt", "scope.json"), "utf8"))
  } catch {
    return null
  }
}

function audit(dir: string, tool: string, argsDigest: string, verdict: string, reason: string) {
  try {
    const entry = { ts: Date.now() / 1000, actor: "plugin", tool, args_digest: argsDigest.slice(0, 200), verdict, reason }
    appendFileSync(join(dir, "hunt", "audit.log"), JSON.stringify(entry) + "\n")
  } catch {}
}

export default (async ({ $ }) => {
  return {
    "experimental.chat.system.transform": async (_input: any, output: any) => {
      const dir = engagementDir()
      if (!dir) return
      try {
        const brief = await $`hx brief --max-bytes 4000`.cwd(dir).text()
        if (typeof output?.system === "string") {
          output.system = output.system + "\n\n" + brief
        } else if (Array.isArray(output?.system)) {
          output.system.push(brief)
        }
      } catch (err) {
        audit(dir, "brief", "hx brief", "error", String(err))
      }
    },
    "shell.env": async (_input: any, output: any) => {
      const dir = engagementDir()
      if (!dir) return
      try {
        output.env.HTTP_PROXY = "http://127.0.0.1:8899"
        output.env.HTTPS_PROXY = "http://127.0.0.1:8899"
        output.env.ALL_PROXY = "http://127.0.0.1:8899"
        output.env.NO_PROXY = "localhost,127.0.0.1"
      } catch (err) {
        audit(dir, "shell.env", "proxy vars", "error", String(err))
      }
    },
    "tool.execute.before": async (input: any, output: any) => {
      const dir = engagementDir()
      if (!dir) return
      const scope = scopeOf(dir)
      if (!scope) {
        audit(dir, "tripwire", "scope.json", "block", "scope ilegivel — rede bloqueada")
        throw new Error("hunt/ presente mas scope.json ilegivel — rede bloqueada")
      }
      const tool = input?.tool || input?.name
      if (tool === "bash") {
        const command = String(output?.args?.command || "")
        const decision = decideBash(scope, command)
        audit(dir, "bash", command, decision.action, decision.reason)
        if (decision.action === "block") throw new Error(decision.reason)
      }
      if (tool === "webfetch") {
        const url = String(output?.args?.url || "")
        const decision = decideUrl(scope, url)
        audit(dir, "webfetch", url, decision.action, decision.reason)
        if (decision.action === "block") throw new Error(decision.reason)
      }
      if (tool === "websearch") {
        const query = String(output?.args?.query || "")
        const offenders = extractHosts(query).filter((host: string) => !hostAllowed(scope, host))
        if (offenders.length > 0) {
          audit(dir, "websearch", query, "block", `host fora do escopo: ${offenders.join(", ")}`)
          throw new Error(`host fora do escopo: ${offenders.join(", ")} — use hx run`)
        }
        audit(dir, "websearch", query, "allow", "")
      }
    },
  }
}) satisfies Plugin

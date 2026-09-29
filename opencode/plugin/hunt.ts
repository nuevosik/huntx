import type { Plugin } from "@opencode-ai/plugin"
import { decideBash, decideUrl, extractHosts, hostAllowed } from "./lib/guard.js"
import { appendFileSync, existsSync, readFileSync } from "node:fs"
import { join, resolve } from "node:path"

type Scope = Record<string, unknown> & {
  in_scope?: string[]
  out_of_scope_hosts?: string[]
  allow_extra_hosts?: string[]
}

const AGENT_SESSION = `oc-${process.pid}-${Math.floor(Date.now() / 1000)}`

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : null
}

function text(value: unknown, key: string): string {
  const holder = record(value)
  const raw = holder ? holder[key] : undefined
  return typeof raw === "string" ? raw : ""
}

function engagementDir(): string | null {
  let dir = resolve(process.cwd())
  for (;;) {
    if (existsSync(join(dir, "hunt", "HYPOTHESES.json"))) return dir
    const parent = resolve(dir, "..")
    if (parent === dir) return null
    dir = parent
  }
}

function scopeOf(dir: string): Scope | null {
  try {
    return JSON.parse(readFileSync(join(dir, "hunt", "scope.json"), "utf8")) as Scope
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

const SCOPE_MUTATORS = ["edit", "write", "patch", "multiedit"]

export default (async ({ $ }) => {
  return {
    "experimental.chat.system.transform": async (_input: unknown, output: unknown) => {
      const dir = engagementDir()
      if (!dir) return
      try {
        const brief = await $`hx brief --max-bytes 4000`.cwd(dir).text()
        const holder = record(output)
        if (!holder) return
        if (typeof holder.system === "string") {
          holder.system = holder.system + "\n\n" + brief
        } else if (Array.isArray(holder.system)) {
          holder.system.push(brief)
        }
      } catch (err) {
        audit(dir, "brief", "hx brief", "error", String(err))
      }
    },
    "shell.env": async (_input: unknown, output: unknown) => {
      const dir = engagementDir()
      if (!dir) return
      try {
        const env = record(record(output)?.env)
        if (!env) return
        env.HX_SESSION = AGENT_SESSION
        env.HTTP_PROXY = "http://127.0.0.1:8899"
        env.HTTPS_PROXY = "http://127.0.0.1:8899"
        env.ALL_PROXY = "http://127.0.0.1:8899"
        env.NO_PROXY = "localhost,127.0.0.1,::1"
      } catch (err) {
        audit(dir, "shell.env", "proxy vars", "error", String(err))
      }
    },
    "tool.execute.before": async (input: unknown, output: unknown) => {
      const dir = engagementDir()
      if (!dir) return
      const scope = scopeOf(dir)
      if (!scope) {
        audit(dir, "tripwire", "scope.json", "block", "scope ilegivel — rede bloqueada")
        throw new Error("hunt/ presente mas scope.json ilegivel — rede bloqueada")
      }
      const tool = text(input, "tool") || text(input, "name")
      if (SCOPE_MUTATORS.includes(tool)) {
        const target = text(record(output)?.args, "filePath") || text(record(output)?.args, "path") || text(record(output)?.args, "file_path")
        if (/(^|\/)scope\.json$/.test(target)) {
          audit(dir, tool, target, "block", "scope.json e imutavel pelo agente")
          throw new Error("scope.json so o dj edita — peça a mudança de escopo em vez de reescrever o arquivo")
        }
      }
      if (tool === "bash") {
        const command = text(record(output)?.args, "command")
        const decision = decideBash(scope, command)
        audit(dir, "bash", command, decision.action, decision.reason)
        if (decision.action === "block") throw new Error(decision.reason)
      }
      if (tool === "webfetch") {
        const url = text(record(output)?.args, "url")
        const decision = decideUrl(scope, url)
        audit(dir, "webfetch", url, decision.action, decision.reason)
        if (decision.action === "block") throw new Error(decision.reason)
      }
      if (tool === "websearch") {
        const query = text(record(output)?.args, "query")
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

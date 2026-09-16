import type { Plugin } from "@opencode-ai/plugin"
import { decideBash, decideUrl } from "./lib/guard.js"
import { existsSync, readFileSync } from "node:fs"
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

export default (async ({ $ }) => {
  return {
    "experimental.chat.system.transform": async (_input: any, output: any) => {
      try {
        const dir = engagementDir()
        if (!dir) return
        const brief = await $`hx brief --max-bytes 4000`.cwd(dir).text()
        if (typeof output?.system === "string") {
          output.system = output.system + "\n\n" + brief
        } else if (Array.isArray(output?.system)) {
          output.system.push(brief)
        }
      } catch {}
    },
    "shell.env": async (_input: any, output: any) => {
      try {
        if (!engagementDir()) return
        output.env.HTTP_PROXY = "http://127.0.0.1:8899"
        output.env.HTTPS_PROXY = "http://127.0.0.1:8899"
        output.env.ALL_PROXY = "http://127.0.0.1:8899"
        output.env.NO_PROXY = "localhost,127.0.0.1"
      } catch {}
    },
    "tool.execute.before": async (input: any, output: any) => {
      const dir = engagementDir()
      if (!dir) return
      const scope = scopeOf(dir)
      if (!scope) throw new Error("hunt/ presente mas scope.json ilegivel — rede bloqueada")
      const tool = input?.tool || input?.name
      if (tool === "bash") {
        const decision = decideBash(scope, output?.args?.command)
        if (decision.action === "block") throw new Error(decision.reason)
      }
      if (tool === "webfetch" || tool === "websearch") {
        const decision = decideUrl(scope, output?.args?.url)
        if (decision.action === "block") throw new Error(decision.reason)
      }
    },
  }
}) satisfies Plugin

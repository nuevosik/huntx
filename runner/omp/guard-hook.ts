import { appendFileSync, existsSync, readFileSync } from "node:fs"
import { join } from "node:path"
import { decideBash } from "../../opencode/plugin/lib/guard.js"

type ToolCallEvent = {
  toolName: string
  input: Record<string, unknown>
}

type HookContext = {
  cwd?: string
}

type HookResult = { block?: boolean; reason?: string } | void

type HookApi = {
  on(event: string, handler: (event: ToolCallEvent, ctx: HookContext) => Promise<HookResult>): void
}

type Scope = Record<string, unknown> & {
  in_scope?: string[]
  out_of_scope_hosts?: string[]
  allow_extra_hosts?: string[]
}

const SCOPE_MUTATORS = ["edit", "write", "patch", "multiedit", "ast_edit"]
const SUBSTITUTION_RE = /\$\(|`|<\(|>\(/
const HX_ONLY_RE = /^\s*hx(\s|$)/

function splitSegments(command: string): string[] {
  const segments: string[] = []
  let current = ""
  let quote: string | null = null
  for (let i = 0; i < command.length; i++) {
    const ch = command[i]
    if (quote) {
      if (ch === "\\" && i + 1 < command.length) {
        current += ch + command[i + 1]
        i++
        continue
      }
      if (ch === quote) quote = null
      current += ch
      continue
    }
    if (ch === '"' || ch === "'") {
      quote = ch
      current += ch
      continue
    }
    if (ch === ";" || ch === "\n" || ch === "&" || ch === "|") {
      segments.push(current)
      current = ""
      continue
    }
    current += ch
  }
  segments.push(current)
  return segments.map((segment) => segment.trim()).filter(Boolean)
}

function nonHxSegment(command: string): boolean {
  const segments = splitSegments(command)
  if (segments.length === 0) return true
  return segments.some((segment) => !HX_ONLY_RE.test(segment))
}

function text(input: Record<string, unknown>, key: string): string {
  const value = input[key]
  return typeof value === "string" ? value : ""
}

function audit(dir: string, tool: string, argsDigest: string, verdict: string, reason: string) {
  try {
    const entry = { ts: Date.now() / 1000, actor: "omp-hook", tool, args_digest: argsDigest.slice(0, 200), verdict, reason }
    appendFileSync(join(dir, "hunt", "audit.log"), JSON.stringify(entry) + "\n")
  } catch {}
}

function scopeOf(cwd: string): Scope | null {
  const path = join(cwd, "hunt", "scope.json")
  if (!existsSync(path)) return null
  try {
    return JSON.parse(readFileSync(path, "utf8")) as Scope
  } catch {
    return null
  }
}

export default function huntGuard(pi: HookApi): void {
  pi.on("tool_call", async (event, ctx) => {
    const cwd = ctx.cwd || process.cwd()
    if (!existsSync(join(cwd, "hunt", "HYPOTHESES.json"))) return
    const scope = scopeOf(cwd)
    if (!scope) {
      audit(cwd, event.toolName, "scope.json", "block", "scope ilegivel — rede bloqueada")
      return { block: true, reason: "hunt/ presente mas scope.json ilegivel — rede bloqueada" }
    }
    const mode = process.env.HX_OMP_BASH_MODE || "hx-only"
    if (SCOPE_MUTATORS.includes(event.toolName)) {
      if (mode === "plan") {
        audit(cwd, event.toolName, "planner read-only", "block", "planner nao escreve")
        return { block: true, reason: "planner é read-only: proponha hipóteses com `hx hypothesis add`" }
      }
      const target = text(event.input, "filePath") || text(event.input, "path") || text(event.input, "file_path")
      if (/(^|\/)scope\.json$/.test(target)) {
        audit(cwd, event.toolName, target, "block", "scope.json e imutavel pelo agente")
        return { block: true, reason: "scope.json so o dj edita — peça a mudança de escopo em vez de reescrever o arquivo" }
      }
      return
    }
    if (event.toolName !== "bash") return
    const command = text(event.input, "command")
    if (mode === "plan") {
      if (!/^\s*hx hypothesis add(\s|$)/.test(command) || SUBSTITUTION_RE.test(command)) {
        audit(cwd, "bash", command, "block", "planner: so hx hypothesis add")
        return { block: true, reason: "planner só enfileira: use `hx hypothesis add`" }
      }
      audit(cwd, "bash", command, "allow", "plan")
      return
    }
    if (mode === "guard") {
      const decision = decideBash(scope, command)
      audit(cwd, "bash", command, decision.action, decision.reason)
      if (decision.action === "block") return { block: true, reason: decision.reason }
      return
    }
    if (!HX_ONLY_RE.test(command) || SUBSTITUTION_RE.test(command) || nonHxSegment(command)) {
      audit(cwd, "bash", command, "block", "fatia headless: so hx * sem substituicao")
      return { block: true, reason: "fatia headless: só `hx *` (sem substituição de comando) — use hx run" }
    }
    audit(cwd, "bash", command, "allow", "")
  })
}

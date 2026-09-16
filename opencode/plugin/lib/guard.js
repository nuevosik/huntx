export const SCAN_BINARIES = ["curl", "wget", "nuclei", "ffuf", "naabu", "dnsx", "httpx", "katana", "subfinder", "s3scanner", "nmap", "masscan"];

const WRAPPERS = new Set(["sudo", "env", "command", "time", "nice", "nohup"]);
const IPV4_RE = /^(?:\d{1,3}\.){3}\d{1,3}$/;

function stripBrackets(host) {
  const value = (host || "").toLowerCase();
  if (value.startsWith("[") && value.endsWith("]")) return value.slice(1, -1);
  return value;
}

function baseBinary(command) {
  const words = command.trim().split(/\s+/).filter((token) => !token.startsWith("-") && !/^[A-Za-z_][A-Za-z0-9_]*=/.test(token));
  for (const word of words) {
    if (WRAPPERS.has(word)) continue;
    return word.split("/").pop();
  }
  return "";
}

function hostToken(token) {
  let candidate = token.replace(/:\d+$/, "").replace(/\/\d{1,2}$/, "");
  candidate = stripBrackets(candidate);
  if (IPV4_RE.test(candidate)) return candidate;
  if (/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(candidate)) return candidate;
  return null;
}

export function hostMatches(pattern, host) {
  const normalized = pattern.toLowerCase().trim();
  const target = stripBrackets(host);
  if (normalized.startsWith("*.")) {
    return target.endsWith(normalized.slice(1)) || target === normalized.slice(2);
  }
  return target === normalized || target.endsWith("." + normalized);
}

export function hostAllowed(scope, host) {
  const target = stripBrackets(host);
  if (!target || target === "127.0.0.1" || target === "localhost" || target === "::1") return true;
  for (const pattern of scope.out_of_scope_hosts || []) {
    if (hostMatches(pattern, target)) return false;
  }
  for (const pattern of scope.in_scope || []) {
    if (hostMatches(pattern, target)) return true;
  }
  for (const pattern of scope.allow_extra_hosts || []) {
    if (hostMatches(pattern, target)) return true;
  }
  return false;
}

export function extractHosts(command) {
  const hosts = new Set();
  const urlRe = /https?:\/\/[^\s"']+/gi;
  let match;
  while ((match = urlRe.exec(command)) !== null) {
    try {
      hosts.add(stripBrackets(new URL(match[0]).hostname));
    } catch {
      continue;
    }
  }
  return [...hosts];
}

export function decideBash(scope, command) {
  if (!command) return { action: "allow", reason: "" };
  const trimmed = command.trim();
  const segments = trimmed.split(/[;\n]|&&|\|\|?/).map((segment) => segment.trim()).filter(Boolean);
  if (segments.length > 0 && segments.every((segment) => segment === "hx" || segment.startsWith("hx "))) {
    return { action: "allow", reason: "hx path" };
  }
  const offenders = new Set();
  for (const host of extractHosts(trimmed)) {
    if (!hostAllowed(scope, host)) offenders.add(host);
  }
  if (SCAN_BINARIES.includes(baseBinary(trimmed))) {
    for (const token of trimmed.split(/\s+/)) {
      if (token.startsWith("-")) continue;
      const host = hostToken(token);
      if (host && !hostAllowed(scope, host)) offenders.add(host);
    }
  }
  if (offenders.size > 0) {
    return { action: "block", reason: `host fora do escopo: ${[...offenders].join(", ")} — use hx run` };
  }
  return { action: "allow", reason: "" };
}

export function decideUrl(scope, url) {
  if (!url) return { action: "allow", reason: "" };
  let host = "";
  try {
    host = stripBrackets(new URL(url).hostname);
  } catch {
    return { action: "allow", reason: "" };
  }
  if (hostAllowed(scope, host)) return { action: "allow", reason: "" };
  return { action: "block", reason: `host fora do escopo: ${host}` };
}

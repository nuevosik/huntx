export const SCAN_BINARIES = ["curl", "wget", "nuclei", "ffuf", "naabu", "dnsx", "httpx", "katana", "subfinder", "s3scanner", "nmap", "masscan"];

export function hostMatches(pattern, host) {
  const normalized = pattern.toLowerCase().trim();
  const target = host.toLowerCase();
  if (normalized.startsWith("*.")) {
    return target.endsWith(normalized.slice(1)) || target === normalized.slice(2);
  }
  return target === normalized || target.endsWith("." + normalized);
}

export function hostAllowed(scope, host) {
  const target = (host || "").toLowerCase();
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
  const urlRe = /https?:\/\/([^/\s"':]+)/gi;
  let match;
  while ((match = urlRe.exec(command)) !== null) hosts.add(match[1].toLowerCase());
  return [...hosts];
}

export function decideBash(scope, command) {
  if (!command) return { action: "allow", reason: "" };
  const trimmed = command.trim();
  if (trimmed === "hx" || trimmed.startsWith("hx ")) return { action: "allow", reason: "hx path" };
  const offenders = new Set();
  for (const host of extractHosts(trimmed)) {
    if (!hostAllowed(scope, host)) offenders.add(host);
  }
  const firstWord = trimmed.split(/\s+/)[0].split("/").pop();
  if (SCAN_BINARIES.includes(firstWord)) {
    for (const token of trimmed.split(/\s+/)) {
      if (token.startsWith("-")) continue;
      if (/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(token) && !hostAllowed(scope, token)) offenders.add(token);
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
    host = new URL(url).hostname;
  } catch {
    return { action: "allow", reason: "" };
  }
  if (hostAllowed(scope, host)) return { action: "allow", reason: "" };
  return { action: "block", reason: `host fora do escopo: ${host}` };
}

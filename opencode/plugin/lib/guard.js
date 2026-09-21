export const SCAN_BINARIES = ["curl", "wget", "nuclei", "ffuf", "naabu", "dnsx", "httpx", "katana", "subfinder", "s3scanner", "nmap", "masscan", "nc", "ncat", "socat", "openssl", "ssh", "scp", "sftp", "telnet", "dig", "nslookup", "host", "ftp", "smbclient"];

const WRAPPERS = new Set(["sudo", "env", "command", "time", "nice", "nohup", "timeout", "xargs", "watch", "flock", "strace", "ltrace", "doas"]);
const IPV4_RE = /^(?:\d{1,3}\.){3}\d{1,3}$/;
const SUBSTITUTION_RE = /\$\(|`|<\(|>\(/;
const DEV_TCP_RE = /\/dev\/(?:tcp|udp)\b/;
const INTERP_RE = /(^|[;\n&|]\s*)(python3?|node|perl|ruby|php|sh|bash|dash|zsh|ksh)\s+-[ce]\b/;
const INTERP_NET_RE = /urllib|requests|http\.client|httpx|aiohttp|\bsocket\b|\bnet\b|\bdgram\b|\btls\b|WebSocket|getaddrinfo|fetch\s*\(|XMLHttpRequest|os\.system|subprocess|popen|child_process|\bcurl\b|\bwget\b|\bnc\b|netcat|socat|openssl|\bssh\b|\bscp\b/;

function stripBrackets(host) {
  const value = (host || "").toLowerCase();
  if (value.startsWith("[") && value.endsWith("]")) return value.slice(1, -1);
  return value;
}

function baseBinary(command) {
  const words = command
    .trim()
    .split(/\s+/)
    .filter((token) => !token.startsWith("-") && !/^[A-Za-z_][A-Za-z0-9_]*=/.test(token) && !/^\d+$/.test(token));
  for (const word of words) {
    if (WRAPPERS.has(word)) continue;
    return word.split("/").pop();
  }
  return "";
}

function hostToken(token) {
  let candidate = token.replace(/:\d+$/, "").replace(/\/\d{1,2}$/, "");
  candidate = candidate.split("@").pop();
  candidate = candidate.replace(/^(?:tcp|udp)\d?:/i, "");
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
  const urlRe = /\b(?:https?|ftp|sftp|ssh|git|smb):\/\/[^\s"']+/gi;
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

function allowedLiteralPresent(scope, command) {
  if (extractHosts(command).some((host) => hostAllowed(scope, host))) return true;
  return command.split(/\s+/).some((token) => {
    if (token.startsWith("-")) return false;
    const host = hostToken(token);
    return host !== null && hostAllowed(scope, host);
  });
}

function infoOnly(segment) {
  const rest = segment.trim().split(/\s+/).slice(1);
  return rest.every((token) => !token || token.startsWith("-") || WRAPPERS.has(token) || /^[A-Za-z_][A-Za-z0-9_]*=/.test(token));
}

function netCapable(segment) {
  const words = segment.split(/\s+/).filter(Boolean);
  const binary = baseBinary(segment);
  if (SCAN_BINARIES.includes(binary)) return true;
  if (words.length && WRAPPERS.has(words[0]) && words.some((word) => SCAN_BINARIES.includes(word.split("/").pop()))) return true;
  const execMatch = segment.match(/\s-(?:exec|execdir|ok)\s+(.+)$/);
  return execMatch ? SCAN_BINARIES.includes(baseBinary(execMatch[1])) : false;
}

function splitSegments(command) {
  const segments = [];
  let current = "";
  let quote = null;
  for (let i = 0; i < command.length; i++) {
    const ch = command[i];
    if (quote) {
      if (ch === "\\" && i + 1 < command.length) {
        current += ch + command[i + 1];
        i++;
        continue;
      }
      if (ch === quote) quote = null;
      current += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      current += ch;
    } else if (ch === ";" || ch === "\n" || ch === "&" || ch === "|") {
      segments.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  segments.push(current);
  return segments.map((segment) => segment.trim()).filter(Boolean);
}

export function decideBash(scope, command) {
  if (!command) return { action: "allow", reason: "" };
  const trimmed = command.trim();
  const segments = splitSegments(trimmed);
  const hxSegments = segments.filter((segment) => segment === "hx" || segment.startsWith("hx "));
  if (hxSegments.length > 0 && SUBSTITUTION_RE.test(trimmed)) {
    return { action: "block", reason: "substituicao de comando nos argumentos do hx — proibido" };
  }
  if (segments.length > 0 && hxSegments.length === segments.length) {
    return { action: "allow", reason: "hx path" };
  }
  if (DEV_TCP_RE.test(trimmed)) {
    return { action: "block", reason: "/dev/tcp ou /dev/udp — use hx run" };
  }
  const offenders = new Set();
  for (const host of extractHosts(trimmed)) {
    if (!hostAllowed(scope, host)) offenders.add(host);
  }
  for (const segment of segments) {
    if (!netCapable(segment)) continue;
    for (const token of segment.split(/\s+/)) {
      if (token.startsWith("-")) continue;
      const host = hostToken(token);
      if (host && !hostAllowed(scope, host)) offenders.add(host);
    }
  }
  if (offenders.size > 0) {
    return { action: "block", reason: `host fora do escopo: ${[...offenders].join(", ")} — use hx run` };
  }
  for (const segment of segments) {
    if (netCapable(segment) && !allowedLiteralPresent(scope, segment) && !infoOnly(segment)) {
      const label = baseBinary(segment) || segment.split(/\s+/)[0];
      return { action: "block", reason: `${label} sem host in-scope literal — use hx run` };
    }
    if (INTERP_RE.test(segment) && INTERP_NET_RE.test(segment) && !allowedLiteralPresent(scope, segment)) {
      return { action: "block", reason: "rede via interpretador sem host in-scope literal — use hx run" };
    }
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

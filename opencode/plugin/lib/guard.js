export const SCAN_BINARIES = ["curl", "wget", "nuclei", "ffuf", "naabu", "dnsx", "httpx", "katana", "subfinder", "s3scanner", "nmap", "masscan", "nc", "ncat", "socat", "openssl", "ssh", "scp", "sftp", "telnet", "dig", "nslookup", "host", "ftp", "smbclient", "git", "rsync", "svn", "hg", "docker", "podman", "ping", "ping6", "traceroute", "tracepath", "netcat", "busybox", "go", "npm", "npx", "yarn", "pnpm", "pip", "pip3", "gem", "cargo", "kubectl", "aws", "gcloud", "az", "psql", "mysql", "mongosh", "redis-cli", "wrk", "ab", "siege", "tftp", "iperf", "iperf3", "sshpass", "dropbear", "rclone", "restic", "borg", "ansible", "terraform", "vagrant", "ldapsearch", "snmpwalk"];

export const INTERP_BINARIES = ["python", "python2", "python3", "node", "deno", "bun", "perl", "ruby", "php", "sh", "bash", "dash", "zsh", "ksh", "lua", "tclsh", "pwsh", "powershell", "osascript", "busybox"];

const LOCAL_SUBCOMMANDS = {
  openssl: ["rand", "version", "dgst", "genrsa", "genpkey", "req", "x509", "enc", "passwd", "prime", "rsa", "ec", "pkcs12", "verify", "asn1parse", "base64", "list"],
  git: ["status", "log", "diff", "show", "branch", "add", "commit", "checkout", "stash", "rev-parse", "describe", "tag", "init"],
  npm: ["install", "test", "run", "ci", "ls", "list"],
  docker: ["ps", "images", "logs", "inspect"],
};

const INTERP_LOCAL_RE = /(?:^|\s)-m\s+(?:http\.server|json\.tool|venv|compileall|pytest|unittest)(?:\s|$)/;
const INLINE_FLAG_RE = /(?:^|\s)(?:-c|-e|--eval|--exec|-ce|-ec)(?:\s|$)/;
const SCRIPT_ARG_RE = /(?:^|\s)[^\s-][^\s]*\.(?:py|js|mjs|cjs|ts|rb|pl|php|sh|bash|lua)(?:\s|$)|(?:^|\s)(?:\/|\.\/|\.\.\/)[^\s]+/;
const LISTEN_FLAGS = ["-l", "--listen"];
const LISTEN_BINARIES = ["nc", "ncat", "netcat", "socat", "busybox"];

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
  let candidate = token.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
  candidate = candidate.replace(/^[A-Za-z_][A-Za-z0-9_]*=/, "");
  const at = candidate.indexOf("@");
  const slash = candidate.indexOf("/");
  if (at > -1 && (slash === -1 || at < slash)) candidate = candidate.slice(at + 1);
  else if (slash > -1 && at > slash) candidate = candidate.slice(0, at);
  candidate = candidate.split("/")[0];
  candidate = candidate.replace(/:\d+$/, "");
  candidate = candidate.replace(/^(?:tcp|udp)\d?:/i, "");
  const colon = candidate.indexOf(":");
  if (colon > 0 && !/^\d+$/.test(candidate.slice(colon + 1))) candidate = candidate.slice(0, colon);
  candidate = stripBrackets(candidate);
  if (IPV4_RE.test(candidate)) return candidate;
  if (/^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}$/i.test(candidate)) return candidate;
  return null;
}

const SUBSTITUTION_BODY_RE = /\$\(([^()]*)\)|`([^`]*)`/g;
const QUOTED_HOST_RE = /['"`]((?:[a-z][a-z0-9+.-]*:\/\/)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:[/:?#][^'"`\s]*)?)['"`]/gi;

function substitutionBodies(command) {
  const bodies = [];
  let match;
  SUBSTITUTION_BODY_RE.lastIndex = 0;
  while ((match = SUBSTITUTION_BODY_RE.exec(command)) !== null) {
    bodies.push(match[1] ?? match[2] ?? "");
  }
  return bodies;
}

function quotedHosts(segment) {
  const hosts = new Set();
  let match;
  QUOTED_HOST_RE.lastIndex = 0;
  while ((match = QUOTED_HOST_RE.exec(segment)) !== null) {
    const host = hostToken(match[1]);
    if (host) hosts.add(host);
  }
  return [...hosts];
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

function localOnly(segment) {
  const binary = baseBinary(segment);
  const words = segment.trim().split(/\s+/).filter(Boolean);
  if (isInterpreter(binary) && INTERP_LOCAL_RE.test(segment)) return true;
  const args = words.slice(1).filter((word) => !/^[A-Za-z_][A-Za-z0-9_]*=/.test(word));
  if (LISTEN_BINARIES.includes(binary) && args.some((word) => LISTEN_FLAGS.includes(word))) return true;
  const allowed = LOCAL_SUBCOMMANDS[binary];
  if (!allowed) return false;
  const first = args.find((word) => !word.startsWith("-"));
  return Boolean(first && allowed.includes(first));
}

function isInterpreter(binary) {
  if (INTERP_BINARIES.includes(binary)) return true;
  return INTERP_BINARIES.includes(binary.replace(/\d+(?:\.\d+)*$/, ""));
}

function netCapable(segment) {
  const words = segment.split(/\s+/).filter(Boolean);
  const binary = baseBinary(segment);
  if (SCAN_BINARIES.includes(binary) || INTERP_BINARIES.includes(binary)) return true;
  if (words.length && WRAPPERS.has(words[0]) && words.some((word) => SCAN_BINARIES.includes(word.split("/").pop()) || INTERP_BINARIES.includes(word.split("/").pop()))) return true;
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
  if (DEV_TCP_RE.test(trimmed)) {
    return { action: "block", reason: "/dev/tcp ou /dev/udp — use hx run" };
  }
  const hxSegments = segments.filter((segment) => segment === "hx" || segment.startsWith("hx "));
  if (hxSegments.length > 0 && SUBSTITUTION_RE.test(trimmed)) {
    return { action: "block", reason: "substituicao de comando nos argumentos do hx — proibido" };
  }
  if (segments.length > 0 && hxSegments.length === segments.length) {
    return { action: "allow", reason: "hx path" };
  }
  const offenders = new Set();
  for (const host of extractHosts(trimmed)) {
    if (!hostAllowed(scope, host)) offenders.add(host);
  }
  for (const segment of segments) {
    for (const body of substitutionBodies(segment)) {
      for (const token of body.split(/\s+/)) {
        const host = hostToken(token);
        if (host && !hostAllowed(scope, host)) offenders.add(host);
      }
    }
  }
  for (const segment of segments) {
    if (!netCapable(segment)) continue;
    const words = segment.split(/\s+/);
    const moduleIndex = words.indexOf("-m");
    const ignored = moduleIndex > -1 ? new Set([words[moduleIndex + 1]]) : new Set();
    for (const token of words) {
      if (token.startsWith("-") || ignored.has(token)) continue;
      const host = hostToken(token);
      if (host && !hostAllowed(scope, host)) offenders.add(host);
    }
  }
  if (offenders.size > 0) {
    return { action: "block", reason: `host fora do escopo: ${[...offenders].join(", ")} — use hx run` };
  }
  const localSkipped = [];
  for (const segment of segments) {
    if (localOnly(segment)) {
      localSkipped.push(baseBinary(segment) || segment.split(/\s+/)[0]);
      continue;
    }
    const binary = baseBinary(segment);
    const interpreter = isInterpreter(binary);
    if (interpreter && !INTERP_LOCAL_RE.test(segment)) {
      const args = segment.trim().split(/\s+/).slice(1).filter((word) => !/^[A-Za-z_][A-Za-z0-9_]*=/.test(word));
      const bare = args.length === 0;
      const inline = INLINE_FLAG_RE.test(segment);
      const netish = INTERP_NET_RE.test(segment);
      const script = !inline && (SCRIPT_ARG_RE.test(segment) || bare) && !infoOnly(segment);
      if (netish || script || (bare && /[|<]/.test(trimmed))) {
        for (const host of quotedHosts(segment)) {
          if (!hostAllowed(scope, host)) {
            return { action: "block", reason: `host fora do escopo em interpretador: ${host} — use hx run` };
          }
        }
        if (!allowedLiteralPresent(scope, segment)) {
          return { action: "block", reason: `${binary} sem host in-scope literal — use hx run` };
        }
      }
    }
    if (!interpreter && netCapable(segment) && !allowedLiteralPresent(scope, segment) && !infoOnly(segment)) {
      const label = binary || segment.split(/\s+/)[0];
      return { action: "block", reason: `${label} sem host in-scope literal — use hx run` };
    }
    if (INTERP_RE.test(segment) && INTERP_NET_RE.test(segment) && !allowedLiteralPresent(scope, segment)) {
      return { action: "block", reason: "rede via interpretador sem host in-scope literal — use hx run" };
    }
  }
  if (localSkipped.length > 0) {
    return { action: "allow", reason: `local-only: ${localSkipped.join(", ")}` };
  }
  return { action: "allow", reason: "" };
}

export function decideUrl(scope, url) {
  if (!url) return { action: "allow", reason: "" };
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return { action: "block", reason: "url invalida ou relativa — use hx run" };
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { action: "block", reason: `esquema ${parsed.protocol} nao suportado — use hx run` };
  }
  const host = stripBrackets(parsed.hostname);
  if (hostAllowed(scope, host)) return { action: "allow", reason: "" };
  return { action: "block", reason: `host fora do escopo: ${host}` };
}

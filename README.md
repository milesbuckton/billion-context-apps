# billion-context-apps

[![Lint](https://github.com/milesbuckton/billion-context-apps/actions/workflows/lint.yml/badge.svg)](https://github.com/milesbuckton/billion-context-apps/actions/workflows/lint.yml)

Portable installers for routing desktop apps that upstream
[billion-context](https://www.npmjs.com/package/billion-context) has no lane
for through its proxy, so they get the same context compression as the other
agentic IDEs on the machine.

| App | Installer | Route |
| --- | --- | --- |
| VS Code's bundled GitHub Copilot agent host | `./install-copilot.sh` | cert-MITM via `http.proxy` |
| GitHub Copilot desktop app | `./install-copilot.sh` | cert-MITM via `proxyUrl` |
| Antigravity desktop app | `./install-antigravity.sh` | upstream `bili antigravity` lane, [below](#antigravity-desktop-app) |

Each installer is independent and takes `--uninstall`.

---

## VS Code and the GitHub Copilot app

### Why this exists

Upstream `billion-context` has **no `vscode` lane**. `LAUNCH_CLIENTS` in
`src/launcher.ts:165` lists `copilot`, but that is the **Copilot CLI** — a
closed Go binary with no base-URL override, which upstream handles with
cert-MITM (`src/launcher.ts:828-833`, [#1049](https://github.com/ranxianglei/billion-context/issues/1049)).

VS Code's Copilot agent host spawns a **native** runtime
(`copilot-runtime` from `@github/copilot-sdk-darwin-arm64`) with the same
constraint: no base-URL override. So the only route is the same cert-MITM
contract:

```text
VS Code ──> copilot-runtime ──(HTTPS_PROXY)──> bili proxy (18788)
                                                    │
                                          api.githubcopilot.com
```

### How it is wired (and why it is update-proof)

#### VS Code

The routing uses **VS Code's stable `http.proxy` setting**. The agent host
reads it and forwards it to the runtime:

```js
// .../app/out/vs/platform/agentHost/node/agentHostMain.js
_readConfiguredProxy()          -> getRootValue(..., "http.proxy")
_createCopilotCliEnvironment()  -> sets HTTP_PROXY / HTTPS_PROXY / NO_PROXY
```

So `settings.json` gets:

```jsonc
"http.proxy": "http://127.0.0.1:18788",
"http.noProxy": ["localhost", "127.0.0.1", "::1", ...],
"http.proxySupport": "off"
```

**Nothing here depends on an experimental setting id.** An earlier version of
this kit used `chat.agentHost.copilot.runtimePath` (an experimental,
agent-host-scoped id that Microsoft can rename at any time) with a wrapper
script. That was replaced:

| | old (experimental) | new (stable) |
| --- | --- | --- |
| setting | `chat.agentHost.copilot.runtimePath` | `http.proxy` |
| risk | upstream renames the id → compression silently stops | none — `http.proxy` is a long-standing documented setting |
| trust | `SSL_CERT_FILE` / `NODE_EXTRA_CA_CERTS` in the wrapper's env | the root CA in the **login keychain** (macOS trust store) |
| fail-open | the wrapper could exec the runtime unproxied | a helper removes `http.proxy` while the proxy is down and puts it back |
| upgrade path | needed a wrapper path that survives VS Code upgrades | nothing to re-point |

#### The GitHub Copilot app

The app (`com.github.githubapp`, a Tauri app) spawns the **pinned bundled
Copilot CLI** as its session runtime:

```text
~/Library/Caches/github-copilot-sdk/cli/<version>/copilot --server --stdio --no-auto-update
```

The app's own Rust stack has no proxy-env support, but the CLI it spawns
reads its own settings and applies them to its own environment:

```text
~/.copilot/logs/process-*.log
  Proxy configuration: HTTP_PROXY=(not set), HTTPS_PROXY=(not set), NO_PROXY=localhost,127.0.0.1
  Applied proxy URL from user setting to HTTP_PROXY/HTTPS_PROXY: http://127.0.0.1:18788
```

`proxyUrl` is a documented, stable CLI setting (`copilot help config`):

> `proxyUrl`: proxy URL for HTTP(S) requests (e.g. `http://proxy.corp.example:3128`).
>
> - Can be overridden by the `HTTP_PROXY` / `HTTPS_PROXY` environment variables
>   (any casing, including lowercase `http_proxy` / `https_proxy`); if either
>   is set in the environment, it takes precedence and this setting is ignored
> - `NO_PROXY` from the environment is still honored
> - Changing this requires a restart to take effect

So the setting is the least invasive way in: no environment variable in the
GUI session (which would leak into every other app), and the same mechanism
the standalone `copilot` CLI uses. It goes into `~/.copilot/settings.json`:

```jsonc
"proxyUrl": "http://127.0.0.1:18788"
```

#### The trust side

The trust side matters: the Copilot runtime's TLS stack is
`native-tls` / `rustls-platform-verifier`, which on macOS resolves to
**SecTrust** — the system and login keychain. It does *not* use
`SSL_CERT_FILE` for the trust decision the way `curl` does. So the bili root
CA is installed in the login keychain:

```bash
security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db \
  ~/.local/share/billion-context/ca/root-ca.pem
```

`scripts/bili-vscode-trust-ca.sh` does this, but it **compares SHA-1
fingerprints first** — `security add-trusted-cert` blocks (and can prompt)
when the certificate is already present, which would hang an unattended
installer.

One keychain CA covers all three: VS Code's runtime, the app's pinned CLI and
the standalone `copilot` CLI.

---

### Install (on a new Mac)

Requirements:

- macOS with VS Code installed in `/Applications` (or `~/Applications`)
- `node` (18+) on `PATH`
- the `billion-context` package installed globally:
  `npm install -g billion-context`

Then:

```bash
cd billion-context-apps
./install-copilot.sh
```

Restart VS Code and start a Copilot chat.

To remove it again:

```bash
./install-copilot.sh --uninstall
```

---

### What gets installed

| Path | What it is |
| --- | --- |
| `~/.local/lib/billion-context/vscode/copilot-proxy.sh` | idempotent starter for the dedicated Copilot proxy on `127.0.0.1:18788` |
| `~/.local/lib/billion-context/vscode/copilot-proxy-supervisor.sh` | launchd-driven loop: keeps the proxy alive, repairs the proxy-origin pointer, fails open on outage |
| `~/.local/lib/billion-context/vscode/repair-proxy-pointer.py` | restores the global `proxy-origin` pointer when a short-lived proxy deletes it (that breaks `bili mcp` discovery for every host) |
| `~/.local/lib/billion-context/vscode/patch-vscode-proxy-settings.py` | writes/removes the three `http.*` settings, line-based, validate-before-write, CRLF-preserving, idempotent |
| `~/.local/lib/billion-context/vscode/bili-vscode-failopen.py` | removes `http.proxy` while the proxy is down (so Copilot stays usable, uncompressed) and restores it when the proxy is back |
| `~/.local/lib/billion-context/vscode/bili-copilot-app-settings.py` | writes/removes `proxyUrl` in `~/.copilot/settings.json` (the GitHub Copilot app route), same safety rules |
| `~/.local/lib/billion-context/vscode/bili-copilot-app-failopen.py` | removes `proxyUrl` while the proxy is down, restores it when the proxy is back |
| `~/.local/lib/billion-context/vscode/bili-vscode-trust-ca.sh` | installs the bili root CA in the login keychain, fingerprint-checked so it never blocks |
| `~/Library/LaunchAgents/com.billion-context.vscode-copilot-proxy.plist` | launchd job: `RunAtLoad` + `KeepAlive`, so the proxy comes back after a crash or a reboot |
| `~/Library/Application Support/Code/User/settings.json` | the three `http.*` settings (and any leftover `*.runtimePath` key is removed) |
| `~/.copilot/settings.json` | `proxyUrl` (the GitHub Copilot app route) — only if the app is installed |

State and logs live in `~/.local/state/billion-context/`:

| File | Contents |
| --- | --- |
| `bili-copilot.log` | the proxy's own log (what it MITM'd) |
| `copilot-proxy-supervisor.log` | the supervisor cycle |
| `vscode-proxy-failopen.log` | proxy up/down transitions and the VS Code fail-open actions |
| `copilot-app-failopen.log` | proxy up/down transitions and the app fail-open actions |
| `copilot-app-settings.log` | every `proxyUrl` change |
| `vscode-trust-ca.log` | CA trust actions |
| `vscode-copilot-proxy.pid` | the proxy's pid |
| `backups/settings.json.<timestamp>.bak` | a backup before every VS Code settings write |
| `backups/copilot-settings.json.<timestamp>.bak` | a backup before every `~/.copilot/settings.json` write |

---

### How the proxy is configured

The dedicated proxy is a plain `bili start` on port **18788** with the
Copilot host whitelist passed in the child environment only:

```bash
BILI_MITM_DOMAINS="api.githubcopilot.com,api.individual.githubcopilot.com,\
api.business.githubcopilot.com,api.enterprise.githubcopilot.com" \
  bili start --port 18788 --host 127.0.0.1
```

The whitelist stays out of `~/.config/billion-context/billion-context.json`
on purpose: putting it there would change the **opencode** lane's recorded
`mitmDomains`, and upstream compares that list element-wise
(`src/launcher.ts:3621`, `mitm-domains-mismatch`), so the opencode proxy
would have to be rotated.

The proxy is started with `BILI_PARENT_PID` pointing at the supervisor. That
arms the server-side parent-gone watchdog, which makes it a deliberately
**managed shared proxy** — upstream lets other lanes attach to an armed
lane-less daemon instead of refusing it:

```text
native-attach: skip http://127.0.0.1:18788: not own lane and not an armed
shared daemon (attachPolicy=own-lane)
```

With the watchdog armed, `bili copilot` attaches to this proxy instead of
spawning a throw-away one (which used to steal the global pointer and then
delete it on exit).

---

### Non-obvious upstream contracts

These are the things that are not obvious from the docs and that this kit
depends on:

- **The Responses API over WebSocket.** The Copilot runtime tries a WS
  upgrade for `/responses` before falling back to HTTP POST. bili's WS bridge
  only claims `/bili/...` paths (`src/responses-ws.ts:766`), so a MITM'd
  upgrade is refused with `426 Upgrade Required` and the client re-sends over
  plain POST. The work succeeds, but it costs a failed round trip and a 426
  in the log. The runtime honours `COPILOT_CLI_DISABLE_WEBSOCKET_RESPONSES`
  — and the value **must be the string `"true"`**; `"1"` is ignored by the
  runtime's flag parser, and `COPILOT_CLI_ENABLED_FEATURE_FLAGS="websocket_responses=0"`
  does not work. The supervisor sets it with `launchctl setenv` so the GUI
  session inherits it.
- **The agent host's `NO_PROXY` default.** The agent host's own default
  no-proxy list is only used when `http.noProxy` is *not* set. As soon as
  you set it, you must list loopback yourself, or VS Code's own local servers
  (MCP gateway, extension host) get routed through the proxy.
- **`http.proxySupport: "off"`.** Without it VS Code also applies the proxy
  to its own extension traffic, which is not model traffic.
- **Blind tunnels are normal.** The proxy logs `BLIND TUNNEL` warnings for
  `api.github.com`, `cafe.github.com`, `telemetry.*.githubcopilot.com` and
  `exp.*.githubcopilot.com`. Those are not model hosts, so they are passed
  through un-compressed on purpose.
- **Copilot sessions are anonymous.** VS Code's Copilot runtime sends no
  session id, so bili assigns a prefix-affinity id (`pfa-*`). That is fine —
  the same history re-attaches to the same session — but it is not a named
  session.
- **The app's `proxyUrl` is read at startup.** "Changing this requires a
  restart to take effect" — so the app needs a restart to pick the setting up
  (or to pick up a fail-open removal). The VS Code lane does not have this
  problem: it re-reads its settings file live.
- **The app ships its own `NO_PROXY`.** Its CLI children get
  `NO_PROXY=localhost,127.0.0.1` from the app, so the app's own loopback
  WebSocket server is never routed through the proxy.

---

### Verify it is working

```bash
# the proxy is alive and sees Copilot traffic
curl -fsS http://127.0.0.1:18788/__bili/health | head -c 400; echo
tail -f ~/.local/state/billion-context/bili-copilot.log | grep -E "forward POST|new anonymous"

# VS Code actually picked the proxy up
grep -E "Resolved CAPI proxy|Restarting CopilotClient" \
  ~/Library/Application\ Support/Code/logs/*/agenthost.log | tail -3

# the GitHub Copilot app actually picked the proxy up
grep -h "Applied proxy URL" ~/.copilot/logs/process-*.log | tail -2

# neither lane is left pointing at a dead endpoint
tail -3 ~/.local/state/billion-context/vscode-proxy-failopen.log
tail -3 ~/.local/state/billion-context/copilot-app-failopen.log
```

A healthy run looks like this in `bili-copilot.log`:

```text
mitm api.individual.githubcopilot.com:443 tunnel established (TLS terminated locally)
[prefix-affinity] new anonymous session pfa-… (depth=1, tail=…)
forward POST → https://api.individual.githubcopilot.com/responses
```

---

### Update resilience

| What changes | What happens |
| --- | --- |
| VS Code updates | nothing — the routing is a stable setting id, and the runtime lives in the app bundle |
| the Copilot extension updates | nothing — the runtime ships in the app bundle, not the extension |
| the GitHub Copilot app updates | nothing — it resolves its pinned CLI at runtime and reads `proxyUrl` from `~/.copilot/settings.json` |
| `billion-context` updates | the proxy restarts into the new code; the global npm copy is what `18788` runs from |
| the machine reboots | launchd (`RunAtLoad` + `KeepAlive`) brings the proxy back; `launchctl setenv` re-applies the WS-disable var |
| the proxy dies | the supervisor restarts it within ~30 s; `http.proxy` **and** `proxyUrl` are removed while it is down, so both clients keep working (uncompressed) instead of failing every request |

---

### Uninstall

```bash
./install-copilot.sh --uninstall
```

That removes the launchd job, the scripts, the three VS Code settings and the
`proxyUrl` setting. The root CA stays in the login keychain on purpose (it is
harmless, and other tools may want it). To remove it as well:

```bash
security find-certificate -a -c "billion-context" -p \
  ~/Library/Keychains/login.keychain-db | openssl x509 -noout -fingerprint -sha1
security delete-certificate -Z <SHA1 without colons> \
  ~/Library/Keychains/login.keychain-db
```

---

## Antigravity desktop app

Upstream `bili antigravity` launches the **`agy` CLI** only (`resolveClientCommand`
looks for `agy` on `PATH` / `~/.local/bin/agy`). The desktop app runs the same
language server, which honours `CLOUD_CODE_URL`, so the upstream lane works for
it unchanged once `BILI_CLIENT_BIN` points at the app's binary. bili then
rewrites `CLOUD_CODE_URL` to `<proxy>/bili/https://daily-cloudcode-pa.googleapis.com`
(the real default upstream since [#3129](https://github.com/ranxianglei/billion-context/issues/3129)) — no MITM, no CA.

```bash
./install-antigravity.sh             # install / repair
./install-antigravity.sh --uninstall
```

| Path | What it is |
| --- | --- |
| `~/.local/bin/antigravity-bili` | wrapper: resolves node + `bili` at launch (survives node upgrades), sets `BILI_CLIENT_BIN`, runs `bili antigravity` |
| `/Applications/Antigravity (Billion Context).app` | AppleScript launcher with Antigravity's icon; backgrounds the wrapper |
| `~/.local/state/billion-context/antigravity-bili.log` | the wrapper's output (proxy start + app log) |

Launch **Antigravity (Billion Context)** instead of Antigravity. A healthy start
logs `bili: started proxy at http://127.0.0.1:… (HTTP /bili/ rewrites: 1)`.

---

## Linting

CI runs [ShellCheck](https://www.shellcheck.net/) on the installers and
`scripts/*.sh`, and [Pylint](https://pylint.readthedocs.io/) on `scripts/*.py`
with the rules in `.pylintrc`. To run the same checks locally:

```bash
shellcheck *.sh scripts/*.sh
pylint scripts/*.py
```

## Contributing

This is a personal repository and does not accept issues or pull requests. See
[docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).

## License

[MIT](LICENSE)

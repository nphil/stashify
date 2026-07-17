#!/bin/bash
set -uo pipefail

# Tailscale session hook: joins the tailnet so Claude can SSH to devices on
# the local network. Runs only in Claude Code on the web, and only when
# TS_AUTHKEY is set as an environment variable in the Claude environment
# settings (use a reusable + ephemeral auth key so dead containers
# auto-remove themselves from the tailnet).

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

log() { echo "[tailscale-hook] $*" >&2; }

if [ -z "${TS_AUTHKEY:-}" ]; then
  log "TS_AUTHKEY not set - skipping Tailscale setup."
  exit 0
fi

if ! command -v tailscale >/dev/null 2>&1; then
  log "installing tailscale..."
  if ! curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1; then
    log "tailscale install failed - continuing without it."
    exit 0
  fi
fi

# Userspace networking: no TUN device needed, works in unprivileged
# containers. --state=mem: keeps no state on disk, pairing with the
# ephemeral auth key so every session joins as a fresh throwaway node.
if ! pgrep -x tailscaled >/dev/null 2>&1; then
  nohup tailscaled --state=mem: --tun=userspace-networking \
    --socks5-server=localhost:1055 \
    >/tmp/tailscaled.log 2>&1 &
fi

for _ in $(seq 1 30); do
  [ -S /var/run/tailscale/tailscaled.sock ] && break
  sleep 0.5
done

if tailscale up --auth-key="${TS_AUTHKEY}" --hostname="claude-session" \
     --accept-dns=false --timeout=90s >/dev/null 2>&1; then
  log "connected to tailnet:"
  tailscale status >&2 || true
else
  log "tailscale up failed - session continues without tailnet access:"
  tail -5 /tmp/tailscaled.log >&2 || true
  exit 0
fi

# In userspace mode there is no TUN interface, so plain `ssh 100.x.y.z`
# cannot dial directly. Route ssh to tailnet hosts through tailscaled's
# SOCKS5 proxy instead; MagicDNS names (*.ts.net) resolve inside the proxy.
mkdir -p /root/.ssh && chmod 700 /root/.ssh
if ! grep -q "tailscale-socks5" /root/.ssh/config 2>/dev/null; then
  cat >> /root/.ssh/config <<'SSHEOF'
# tailscale-socks5
Host 100.* *.ts.net
  ProxyCommand nc -X 5 -x localhost:1055 %h %p
  StrictHostKeyChecking accept-new
SSHEOF
fi
chmod 600 /root/.ssh/config
log "ssh configured for tailnet hosts (100.* and *.ts.net)."

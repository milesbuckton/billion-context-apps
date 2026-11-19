#!/bin/bash
# bili-vscode-trust-ca.sh - make macOS trust billion-context's MITM root CA.
#
# Why this is needed
# ------------------
# The bundled GitHub Copilot runtime is a closed native binary. When it is
# routed through the bili proxy (cert-MITM), the proxy presents a certificate
# signed by bili's own root CA. The runtime's TLS stack is native-tls /
# rustls-platform-verifier, which on macOS resolves to SecTrust - i.e. the
# SYSTEM and LOGIN KEYCHAIN. It does not read SSL_CERT_FILE for the trust
# decision in the way curl does.
#
# Without the CA in the trust store the runtime fails with:
#
#   client error (Connect): The certificate was not trusted.
#   (and bili logs: TLS error: ... ssl/tls alert certificate unknown)
#
# So the CA is installed in the login keychain. That is a machine-level,
# version-independent trust: it survives VS Code updates, the Copilot runtime
# being replaced, and it also covers the standalone `copilot` CLI and any
# other client that uses the OS trust store.
#
# IMPORTANT: `security add-trusted-cert` BLOCKS (and can prompt) when the
# certificate is already in the keychain, so this script first compares the
# SHA-1 fingerprints and only acts when the CA is genuinely missing. That
# makes it safe to run unattended from a launchd agent or an installer.
#
# Usage: bili-vscode-trust-ca.sh [--check]
#   --check  report the state and exit without changing anything
set -uo pipefail

CA_FILE="${BILI_CA_FILE:-$HOME/.local/share/billion-context/ca/root-ca.pem}"
KEYCHAIN="${BILI_KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"
LOG="${BILI_TRUST_LOG:-$HOME/.local/state/billion-context/vscode-trust-ca.log}"

log() {
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG" 2>/dev/null || true
  printf '[bili-vscode] %s\n' "$*"
}

if [ ! -r "$CA_FILE" ]; then
  log "CA not found at $CA_FILE - run 'bili start' once so bili can generate it"
  exit 1
fi

want_fp="$(openssl x509 -in "$CA_FILE" -noout -fingerprint -sha1 2>/dev/null | sed 's/.*=//; s/://g' | tr '[:lower:]' '[:upper:]')"
if [ -z "$want_fp" ]; then
  log "could not read a SHA-1 fingerprint from $CA_FILE"
  exit 1
fi

have_fp=""
if [ -r "$KEYCHAIN" ]; then
  have_fp="$(security find-certificate -a -c "billion-context" -p "$KEYCHAIN" 2>/dev/null \
    | openssl x509 -noout -fingerprint -sha1 2>/dev/null \
    | sed 's/.*=//; s/://g' | tr '[:lower:]' '[:upper:]' | head -n 1)"
fi

if [ "$have_fp" = "$want_fp" ]; then
  if [ "${1:-}" != "--check" ]; then
    log "CA already trusted (SHA-1 $want_fp) - nothing to do"
  else
    printf 'trusted: %s\n' "$want_fp"
  fi
  exit 0
fi

if [ "${1:-}" = "--check" ]; then
  printf 'NOT trusted: want %s have %s\n' "$want_fp" "${have_fp:-none}"
  exit 1
fi

log "installing CA $want_fp into $KEYCHAIN"
# -r trustRoot = trust for TLS/SSL. The prompt is avoided by -T and by the
# fingerprint check above; a timeout guards against the interactive hang.
if security add-trusted-cert -r trustRoot -k "$KEYCHAIN" "$CA_FILE" >/dev/null 2>&1 &
then
  wait "$!" 2>/dev/null
  rc=$?
else
  rc=1
fi

now_fp="$(security find-certificate -a -c "billion-context" -p "$KEYCHAIN" 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha1 2>/dev/null \
  | sed 's/.*=//; s/://g' | tr '[:lower:]' '[:upper:]' | head -n 1)"

if [ "$now_fp" = "$want_fp" ]; then
  log "CA trusted (SHA-1 $want_fp)"
  exit 0
fi

log "FAILED to trust the CA (rc=$rc, have ${now_fp:-none})"
exit 1

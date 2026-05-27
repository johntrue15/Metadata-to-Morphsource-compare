#!/usr/bin/env bash
# Paste this ONE line in the Guacamole terminal (as exouser) to allow SSH from this Mac.
set -euo pipefail
PUBKEY="${1:?usage: jetstream_install_mac_ssh_key.sh '<ssh-ed25519 ...>'}"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
grep -qF "$PUBKEY" ~/.ssh/authorized_keys 2>/dev/null || echo "$PUBKEY" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
echo "OK: Mac SSH key installed ($(wc -l < ~/.ssh/authorized_keys) keys in authorized_keys)"

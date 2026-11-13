#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE_NAME="71-steelseries-nimbus-fix.rules"
LEGACY_RULE_DST="/etc/udev/rules.d/99-steelseries-nimbus-fix.rules"
RULE_SRC="$SCRIPT_DIR/$RULE_NAME"
RULE_DST="/etc/udev/rules.d/$RULE_NAME"

run_cmd="rm -f '$LEGACY_RULE_DST' && install -m 644 '$RULE_SRC' '$RULE_DST' && udevadm control --reload-rules && udevadm trigger --subsystem-match=input --subsystem-match=hidraw"

if [[ $EUID -eq 0 ]]; then
  bash -c "$run_cmd"
elif [[ -t 0 && ${1:-} != "--pkexec" ]]; then
  sudo bash -c "$run_cmd"
else
  pkexec bash -c "$run_cmd"
fi

echo "Installed $RULE_DST (removed legacy 99- rule if present) and reloaded udev rules."

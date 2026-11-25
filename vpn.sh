#!/bin/bash
# vpn.sh up|probe|down — lab OpenVPN on the runner from $VPN_CONFIG/$VPN_AUTH; output goes to logs only.
set -uo pipefail
D=${RUNNER_TEMP:-/tmp}/vpn; LOGS=${LOGS:-$PWD/logs}
case $1 in
  up)
    : "${VPN_CONFIG:?}" "${VPN_AUTH:?}"
    command -v openvpn >/dev/null || brew install -q openvpn || exit 1
    OVPN=$(command -v openvpn || echo "$(brew --prefix)/sbin/openvpn")
    mkdir -p "$D" && chmod 700 "$D"
    printf '%s\n' "$VPN_CONFIG" >"$D/c.ovpn"; printf '%s\n' "$VPN_AUTH" >"$D/a"; chmod 600 "$D"/*
    sudo "$OVPN" --config "$D/c.ovpn" --auth-user-pass "$D/a" --daemon --log "$LOGS/vpn.log" --writepid "$D/pid" || exit 1
    for _ in $(seq 60); do
      sudo grep -q 'Initialization Sequence Completed' "$LOGS/vpn.log" 2>/dev/null && { sudo rm -f "$D/a"; exit 0; }
      sleep 1
    done
    sudo rm -f "$D/a"; exit 1;;
  probe)
    ok=0
    for t in 10.11.1.83:22 10.11.1.83:8389 10.11.1.83:8388; do
      nc -z -G 5 "${t%:*}" "${t#*:}" && echo "ok $t" || { echo "fail $t"; ok=1; }
    done
    curl -sS -m 10 -o /dev/null -w 'auth %{http_code}\n' https://auth.deh.deeppavlov.ai/ || ok=1
    curl -sS -m 10 -w '\nm2 %{http_code}\n' -H 'Host: 127.0.0.1' http://10.11.1.83:8389/health/ready || ok=1
    exit $ok;;
  down)
    [ -f "$D/pid" ] && sudo kill "$(cat "$D/pid")"; sudo chmod 644 "$LOGS/vpn.log" 2>/dev/null; true;;
esac

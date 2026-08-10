#!/bin/zsh
# =============================================================================
# tb4-up.sh — persist the Thunderbolt ring addressing + MTU across link flaps.
#
# Problem: the ring IPs and jumbo-frame MTU are applied with `ifconfig` and are
# NOT persistent. Every time the Thunderbolt link re-negotiates (peer Mac
# sleeps/wakes, cable event, OS network reset) macOS reverts the interface to
# link-local addressing and MTU 1500. Traffic to the peer then leaks out the
# LAN default gateway (a 12-25 ms detour to the router) and the ring dies.
#
# Fix: a LaunchDaemon (com.hydra.tb4.plist) runs this script as root on load
# and every StartInterval seconds. It is idempotent — it only acts when the
# desired state is missing, so a 20s poll is harmless.
#
# Per-node addressing comes from env (set in the LaunchDaemon's
# EnvironmentVariables or exported in a shell): RING_IFACE, RING_IP, RING_MASK,
# RING_MTU. Command-line args override env; the last three default to sensible
# values. For a 2-node ring e.g. node A sets RING_IP=192.168.2.1 and node B
# RING_IP=192.168.2.2 on the same iface.
#
# Usage:
#   /usr/local/bin/tb4-up.sh [iface] [ip] [netmask] [mtu]
# =============================================================================

IFACE="${1:-${RING_IFACE:-en2}}"
IP="${2:-$RING_IP}"
MASK="${3:-${RING_MASK:-255.255.0.0}}"
MTU="${4:-${RING_MTU:-8000}}"
LOG=/var/log/tb4-up.log
now() { /bin/date '+%Y-%m-%dT%H:%M:%S%z'; }

if [ -z "$IP" ]; then
  echo "$(now) ERROR: no ring IP given (set RING_IP or pass one as arg 2)" >> "$LOG"
  exit 2
fi

# --- IP alias: add only if not already present -------------------------------
if ! /sbin/ifconfig "$IFACE" 2>/dev/null | /usr/bin/grep -q "inet $IP "; then
  if /sbin/ifconfig "$IFACE" inet "$IP" netmask "$MASK" alias 2>/dev/null; then
    echo "$(now) added $IP/$MASK on $IFACE" >> "$LOG"
  else
    echo "$(now) FAILED to add $IP on $IFACE (link down?)" >> "$LOG"
  fi
fi

# --- MTU: set only if it differs ---------------------------------------------
cur=$(/sbin/ifconfig "$IFACE" 2>/dev/null | /usr/bin/awk '/^[a-zA-Z0-9]+: /{print $4}')
if [ -n "$cur" ] && [ "$cur" != "$MTU" ]; then
  if /sbin/ifconfig "$IFACE" mtu "$MTU" 2>/dev/null; then
    echo "$(now) set mtu $MTU on $IFACE (was $cur)" >> "$LOG"
  else
    echo "$(now) FAILED to set mtu $MTU on $IFACE" >> "$LOG"
  fi
fi

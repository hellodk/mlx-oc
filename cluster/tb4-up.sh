#!/bin/zsh
# =============================================================================
# tb4-up.sh — persist the Thunderbolt ring addressing + MTU across link flaps.
#
# Problem: the ring IPs (192.168.2.1/.2) and jumbo-frame MTU are applied with
# `ifconfig` and are NOT persistent. Every time the Thunderbolt link
# re-negotiates (peer Mac sleeps/wakes, cable event, OS network reset) macOS
# reverts the interface to link-local addressing and MTU 1500. Traffic to the
# peer then leaks out the LAN default gateway (a 12-25 ms detour to the router,
# and in our case an OpenWrt dropbear SSH on the far side), and the ring dies.
#
# Fix: a LaunchDaemon (com.hydra.tb4.plist) runs this script as root on load
# and every StartInterval seconds. It is idempotent — it only acts when the
# desired state is missing, so a 20s poll is harmless.
#
# Usage:
#   /usr/local/bin/tb4-up.sh [iface] [ip] [netmask] [mtu]
#     rank0:  en2 192.168.2.1 255.255.0.0 8000
#     rank1:  en2 192.168.2.2 255.255.0.0 8000
# =============================================================================

IFACE="${1:-en2}"
IP="${2:-192.168.2.1}"
MASK="${3:-255.255.0.0}"
MTU="${4:-8000}"
LOG=/var/log/tb4-up.log
now() { /bin/date '+%Y-%m-%dT%H:%M:%S%z'; }

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

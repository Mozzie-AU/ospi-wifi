#!/bin/bash
# Install ospi-wifi on Raspberry Pi OS Bookworm or later (NetworkManager).
# Usage: sudo ./install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo: sudo ./install.sh"
    exit 1
fi
cd "$(dirname "$0")"

if ! systemctl is-active --quiet NetworkManager; then
    echo "NetworkManager isn't running. ospi-wifi needs Raspberry Pi OS Bookworm or later."
    exit 1
fi

if systemctl is-active --quiet dnsmasq; then
    echo "WARNING: the dnsmasq service is running. It can stop the hotspot from handing out"
    echo "addresses. If you don't need it: sudo systemctl disable --now dnsmasq"
fi

need=()
command -v python3 >/dev/null || need+=(python3)
command -v iw >/dev/null || need+=(iw)
dpkg -s dnsmasq-base >/dev/null 2>&1 || need+=(dnsmasq-base)
if [ ${#need[@]} -gt 0 ]; then
    echo "Installing: ${need[*]}"
    apt-get update
    apt-get install -y "${need[@]}"
fi

install -d /opt/ospi-wifi /var/lib/ospi-wifi
install -m 755 ospi-wifi.py /opt/ospi-wifi/ospi-wifi.py
if [ -f /etc/ospi-wifi.conf ]; then
    echo "Keeping existing /etc/ospi-wifi.conf"
else
    install -m 600 ospi-wifi.conf /etc/ospi-wifi.conf
fi
install -m 644 ospi-wifi.service /etc/systemd/system/ospi-wifi.service

# Raspberry Pi OS keeps WiFi blocked until a country is set.
country=$(sed -n 's/^WIFI_COUNTRY=//p' /etc/ospi-wifi.conf | tr -d "\"' " | head -n1)
if [ -n "$country" ] && command -v raspi-config >/dev/null; then
    raspi-config nonint do_wifi_country "$country" || true
fi
rfkill unblock wifi 2>/dev/null || true

# Check the adapter can be a hotspot.
iface=$(sed -n 's/^IFACE=//p' /etc/ospi-wifi.conf | tr -d "\"' " | head -n1)
iface=${iface:-wlan0}
phy=$(iw dev "$iface" info 2>/dev/null | awk '/wiphy/ {print "phy"$2}')
if [ -n "$phy" ] && ! iw phy "$phy" info | sed -n '/Supported interface modes/,/^\s*[A-Z]/p' | grep -q '\* AP$'; then
    echo "WARNING: $iface doesn't list AP mode, so the fallback hotspot probably won't start."
fi

systemctl daemon-reload
systemctl enable ospi-wifi.service
systemctl restart ospi-wifi.service

port=$(sed -n 's/^CONSOLE_PORT=//p' /etc/ospi-wifi.conf | tr -d "\"' " | head -n1)
echo
echo "ospi-wifi installed and running."
echo "Setup console: http://$(hostname).local:${port:-8090}/"
echo "Logs:          journalctl -u ospi-wifi -f"

#!/bin/sh
# install.sh - install the hp_smm_fan kernel module via DKMS (survives kernel
# upgrades, auto-signs with your MOK if Secure Boot is on) plus the fan-curve
# daemon and a systemd service. Run: sudo ./install.sh
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root: sudo ./install.sh"; exit 1; }
cd "$(dirname "$0")"
NAME=hp_smm_fan; VER=1.0; SRC="/usr/src/${NAME}-${VER}"

if ! command -v dkms >/dev/null 2>&1; then
	echo "== installing dkms"
	apt-get update -q && apt-get install -y dkms
fi

echo "== copying source to $SRC"
dkms remove -m "$NAME" -v "$VER" --all 2>/dev/null || true
rm -rf "$SRC"; mkdir -p "$SRC"
cp hp_smm_fan.c Makefile dkms.conf "$SRC/"

echo "== dkms add/build/install"
dkms add -m "$NAME" -v "$VER"
dkms build -m "$NAME" -v "$VER"
dkms install -m "$NAME" -v "$VER"
echo "$NAME" > /etc/modules-load.d/${NAME}.conf

echo "== daemon + service"
install -m 0755 fan-daemon.py /usr/local/sbin/fan-daemon.py
install -m 0644 hp-fan.service /etc/systemd/system/hp-fan.service
[ -f /etc/hp-fan.conf ] || install -m 0644 hp-fan.conf /etc/hp-fan.conf
modprobe "$NAME" || true
systemctl daemon-reload
systemctl enable hp-fan.service
systemctl restart hp-fan.service

echo "== done (DKMS will rebuild the module on every kernel upgrade)"
dkms status "$NAME"
systemctl --no-pager status hp-fan.service | head -4

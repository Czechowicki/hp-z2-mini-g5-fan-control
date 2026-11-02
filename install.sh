#!/bin/sh
# Stage the DKMS module and daemon for the next boot.
set -eu
fail() { echo "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "Run as root: sudo ./install.sh"
cd "$(dirname "$0")"
kernel=$(uname -r)
name=hp_smm_fan
version=$(sed -n 's/^PACKAGE_VERSION="\([0-9.]*\)"$/\1/p' dkms.conf)
[ -n "$version" ] || fail "Missing DKMS package version."
src="/usr/src/$name-$version"

# The install bundle keeps the original daemon and DKMS module for rollback.
# Overwriting either file would break both its selector and rollback checks.
for path in /etc/depmod.d/81-hp-z2-fan-abi2.conf \
            /etc/systemd/system/hp-fan.service.d/81-hp-z2-fan-abi2.conf \
            /usr/local/lib/hp-z2-fan-abi2-147/start.py; do
    [ ! -e "$path" ] || fail "Bundle-managed installation. Use its instaluj.py status or rollback."
done
[ "$(cat /sys/class/dmi/id/sys_vendor)" = HP ] &&
[ "$(cat /sys/class/dmi/id/product_name)" = 'HP Z2 Mini G5 Workstation' ] &&
[ "$(cat /sys/class/dmi/id/board_name)" = 8755 ] &&
[ "$(cat /sys/class/dmi/id/bios_version)" = 'S50 Ver. 01.04.20' ] ||
    fail "Unsupported platform for this installer. See README.md."
printf '%s  %s\n' 88067a83dc0a13b1b07d4e16d17ddfb0f97e2b3002454e8ca9985daf4d040e97 \
    /sys/firmware/acpi/tables/DSDT | sha256sum -c --status || fail "Unknown DSDT."
[ ! -e /run/hp-smm-policy-trial-unresolved ] || fail "Unresolved earlier session."
for path in /etc/systemd/system/hp-fan.service.d/*.conf /run/systemd/system/hp-fan.service.d/*.conf; do
    [ ! -e "$path" ] || fail "Review existing service override: $path"
done
command -v dkms >/dev/null || fail "Install dkms and kernel headers first."
[ -d "/lib/modules/$kernel/build" ] || fail "Missing headers for $kernel."
if [ -f /etc/hp-fan.conf ]; then
    python3 -IB -c 'import runpy,sys; sys.exit(0 if runpy.run_path("fan-daemon.py")["load_curve"]("/etc/hp-fan.conf") else "Invalid /etc/hp-fan.conf")'
fi

# A rebuild must use exactly these sources. Keep all earlier DKMS versions.
install -d "$src"
for file in hp_smm_fan.c Makefile dkms.conf; do
    if [ -e "$src/$file" ]; then
        cmp -s "$file" "$src/$file" || fail "Conflicting source at $src/$file"
    else
        install -m 0644 "$file" "$src/$file"
    fi
done
dkms install -m "$name" -v "$version" -k "$kernel"
set -- "/var/lib/dkms/$name/$version/$kernel/$(uname -m)/module/$name.ko"*
[ "$#" -eq 1 ] && [ -f "$1" ] || fail "Cannot verify the DKMS build."
expected=$(modinfo -F srcversion "$1")
[ -n "$expected" ] && [ "$(modinfo -k "$kernel" -F srcversion "$name")" = "$expected" ] ||
    fail "Another module is selected for $kernel. Review before rebooting."
if [ -f "/boot/initrd.img-$kernel" ]; then
    update-initramfs -u -k "$kernel"
fi

# Keep the existing curve and backups of any replaced service/client.
install -d /usr/local/sbin /etc/modules-load.d
if ! cmp -s fan-daemon.py /usr/local/sbin/fan-daemon.py; then
    install --backup=numbered -m 0755 fan-daemon.py /usr/local/sbin/fan-daemon.py
fi
if ! cmp -s hp-fan.service /etc/systemd/system/hp-fan.service; then
    install --backup=numbered -m 0644 hp-fan.service /etc/systemd/system/hp-fan.service
fi
[ -e /etc/hp-fan.conf ] || install -m 0644 hp-fan.conf /etc/hp-fan.conf
[ -e /etc/modules-load.d/hp_smm_fan.conf ] || printf '%s\n' "$name" > /etc/modules-load.d/hp_smm_fan.conf
systemctl daemon-reload
systemctl enable hp-fan.service
echo "Staged for $kernel. Reboot when convenient, then check control_abi and hp-fan.service."

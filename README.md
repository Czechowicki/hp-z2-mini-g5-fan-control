# hp-z2-mini-g5-fan-control

Live, dynamic CPU-fan control on the **HP Z2 Mini G5** from Linux — without
capping the CPU, without a reboot, and without touching BIOS/NVRAM.

The Z2 Mini G5's firmware runs the CPU fan on a conservative curve: under
sustained load the chassis gets hot while the fan stays lazy, and there is no
documented OS-level knob to make it spin faster. The hardware-monitor write-lock
blocks the OS from writing fan registers directly. This project drives the fan
through the **same SMM service the HP UEFI Diagnostics "Fan Speed Test" uses** —
reproduced from the running OS — so you can set a *minimum* fan speed on the fly
and let a temperature→speed curve manage it continuously.

- Full CPU performance — this raises the fan floor, it does **not** throttle, cap,
  or RAPL-limit the CPU.
- Live — changes apply immediately, no reboot.
- Non-persistent at the hardware level — it writes to the hardware-monitor RAM
  via SMM, not to flash/NVRAM, so there is no flash wear from frequent updates.
- Safe default — the module restores full firmware **AUTO** control on unload,
  and the daemon restores AUTO on exit.

> ⚠️ **Hardware-specific.** It was reverse-engineered and tested on one exact
> machine (see *Requirements*). The module refuses to load on anything else via a
> DMI board/product guard. Do not run it on other HP models without verifying the
> register map yourself — poking SMM/Super-I/O on the wrong board can hang it.
> Provided as-is, no warranty. See [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md).

## Requirements

- HP Z2 Mini G5 Workstation, board **8755** (enforced by a DMI guard in the module).
- BIOS S50 family (tested on 01.04.20 and 01.05.00 — identical Super-I/O layout).
- Linux with kernel headers, `make`, `dkms`. Tested on Ubuntu 24.04, kernel 6.8.
- Root. Secure Boot is fine — DKMS signs the module with your enrolled MOK.

## Install

```bash
git clone https://github.com/<your-user>/hp-z2-mini-g5-fan-control.git
cd hp-z2-mini-g5-fan-control
sudo ./install.sh
```

This builds and installs the module via DKMS (so it rebuilds automatically on
kernel upgrades), installs the daemon to `/usr/local/sbin/fan-daemon.py`, the
config to `/etc/hp-fan.conf`, and enables the `hp-fan` systemd service.

Check it:

```bash
systemctl status hp-fan
sudo cat /sys/kernel/debug/hp_smm_fan/rpm   # current CPU-fan RPM
sudo cat /sys/kernel/debug/hp_smm_fan/min   # current minimum % (-1 = AUTO)
```

## The fan curve

Edit **`/etc/hp-fan.conf`** — one `temperature percent` pair per line. The daemon
reloads it automatically within a few seconds; **no restart needed**. If you save
an invalid file it keeps the previous curve and logs a warning, so you can't lock
yourself out with a typo.

```
# CPU package temp (°C)   fan minimum (%)
50   20     # <=50 C: quiet at idle
60   35
70   52
80   72
87   90
93  100     # >=93 C: maximum
```

Between points the daemon interpolates linearly. Values below the first
temperature use the first percentage; above the last, the last. Add as many
points as you like (minimum two valid points, `0<=temp<=120`, `0<=percent<=100`).

Uninstall / go back to stock behavior: `sudo systemctl disable --now hp-fan`
then `sudo modprobe -r hp_smm_fan` (both restore firmware AUTO).

## Manual control (without the daemon)

```bash
sudo modprobe hp_smm_fan
echo 60   | sudo tee /sys/kernel/debug/hp_smm_fan/set   # floor = 60%
echo auto | sudo tee /sys/kernel/debug/hp_smm_fan/set   # back to firmware AUTO
```

## Layout

| Path | What |
|------|------|
| `hp_smm_fan.c` | The kernel module (the mechanism). GPL-2.0-or-later. |
| `fan-daemon.py` | Userspace daemon (the policy: temp→speed curve). |
| `hp-fan.conf` | Editable curve, installed to `/etc/hp-fan.conf`. |
| `hp-fan.service` | systemd unit. |
| `Makefile`, `dkms.conf`, `install.sh` | Build & DKMS install. |
| `tools/` | Read-only probe + diagnostic modules used to verify the path. |
| `docs/HOW-IT-WORKS.md` | The reverse-engineering writeup. |

## How it works (short version)

The module acquires the firmware's ACPI mutex (to serialize with
`hp-wmi-sensors`), writes a small command buffer into the HP SMM command region,
rings the hardware-monitor doorbell register, and pulses the Super-I/O event bit
that raises the SMI. The SMM handler — which, unlike the OS, is allowed to lift
the hardware-monitor write-lock — applies the requested fan minimum. It's the
exact transaction the HP UEFI diagnostic performs, issued at runtime instead.
Full details, register map and safety reasoning in
[docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md).

## License

GPL-2.0-or-later. See [LICENSE](LICENSE). This is independent, interoperability
work for hardware I own; it ships no HP firmware, no BIOS images, and no HP code.

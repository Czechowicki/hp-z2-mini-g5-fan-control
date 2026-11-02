# hp-z2-mini-g5-fan-control

A Linux driver and a temperature-based CPU fan curve for the HP Z2 Mini G5.
The daemon reads the CPU package temperature and asks HP firmware to change the
minimum fan speed. Firmware can raise the fan above that minimum. Your curve
lives in `/etc/hp-fan.conf`.

## ABI 2 update

- Rising temperatures can raise the minimum on the next sample, even by one
  percentage point. The five-point default deadband applies to decreases.
- If firmware times out, the driver leaves the pending request in place and
  blocks additional control writes for that module instance. The daemon exits
  on a control error. The service suppresses automatic restarts for exit 79.
- On orderly shutdown after a successful minimum request the daemon requests
  AUTO once. An acknowledgement means firmware cleared the request. The full
  internal firmware state after restore is unknown.

The matched ABI 2 driver and daemon were observed running on board **8755**,
BIOS **S50 Ver. 01.04.20**, Ubuntu 24.04, kernel **6.8.0-147-generic**.
The host reported an acknowledged minimum of **24%**. Measured RPM comes from
the tachometer. Peak temperatures with this revision under load remain unknown.

## Install from source on Ubuntu

If you already installed the kernel-specific ABI 2 bundle and its status is
`ABI2_ACTIVE`, you already have this driver and daemon. The extracted folder
can be deleted. Keep its original ZIP somewhere accessible and extract it again
when you need `instaluj.py status` or `instaluj.py rollback`.

For a source install or a source upgrade from DKMS 1.0, use the reviewed BIOS,
board and DSDT. Install DKMS, build tools and headers for the kernel that will
boot. If Secure Boot is enabled, your kernel must trust the DKMS signing key.

```bash
sudo apt install dkms build-essential linux-headers-$(uname -r)
git clone https://github.com/Czechowicki/hp-z2-mini-g5-fan-control.git
cd hp-z2-mini-g5-fan-control
sudo ./install.sh
```

The installer stages DKMS **1.1** for the running kernel, checks which module is
selected on disk, refreshes its initramfs when present, and enables `hp-fan.service`.
It keeps the existing curve and earlier DKMS sources. Schedule a reboot into the
staged kernel after the installer reports success. From the new boot, check:

```bash
sudo cat /sys/kernel/debug/hp_smm_fan/control_abi  # expected: 2
systemctl is-active hp-fan.service                # expected: active
sudo cat /sys/kernel/debug/hp_smm_fan/min         # last acknowledged request
```

A value of `-1` is the initial/AUTO cache. A value of `-2` means that firmware
completion is unknown. In that case preserve the kernel and service logs before
considering recovery. Unloading the module discards its in-memory fault latch.

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
points as you like (minimum two, `0<=temp<=120`, `0<=percent<=100`).
Temperatures must be distinct. The minimum must stay level or increase with
temperature. A malformed line invalidates the whole curve. Higher requests
apply at the next sample. The `--step` deadband applies to decreases.

To stop curve control on an ordinary source installation: `sudo systemctl disable --now hp-fan`
then `sudo modprobe -r hp_smm_fan` on a normal shutdown. An ambiguous command
requires review. Reloading the module discards its in-memory fault latch.

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
the hardware-monitor write-lock — applies the requested fan minimum. The driver uses this transaction to request a fan minimum while Linux is running.
Full details, register map and safety reasoning in
[docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md).

## Contributors

- [Czechowicki](https://github.com/Czechowicki) maintains the project and tested fan control on the HP Z2 Mini G5.
- [Claude](https://github.com/claude) helped develop the original SMM driver and fan curve daemon.
- [OpenAI Codex](https://github.com/codex) contributed the ABI 2 timeout handling, daemon shutdown fixes, and offline regression tests.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE). This is independent, interoperability
work for hardware I own; it ships no HP firmware, no BIOS images, and no HP code.

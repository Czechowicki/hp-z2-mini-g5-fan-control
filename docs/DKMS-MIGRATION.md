# Migrating the kernel-specific ABI 2 bundle to DKMS

This migration carries the existing 6.8.0-147-generic ABI 2 installation forward
to 6.8.0-149-generic. It is intended for the reviewed HP Z2 Mini G5, board 8755,
BIOS S50 Ver. 01.04.20 and the exact installation from the original bundle.
The ordinary top-level `install.sh` remains the source installer for other
reviewed installations and deliberately refuses the bundle's files.

## Confirmed deployment

On 2026-11-06 the operator supplied this status after booting the physical HP:

```json
{
  "kernel": "6.8.0-149-generic",
  "pid": "5870",
  "client": "/usr/local/lib/hp-z2-fan-dkms-1.1.1/fan-daemon.py",
  "control_abi": 2,
  "minimum_command_cache": 22,
  "status": "DKMS_ABI2_ACTIVE",
  "firmware_or_rpm_verification": "NOT_PERFORMED",
  "migration_phase": "STAGED_FOR_149_REBOOT"
}
```

DKMS reported version 1.1.1 installed for 149, version 1.0 built for 149, and
version 1.0 installed for 147. The separate ABI 2 module remains selected on 147
by the preserved depmod override. The retained 1.0 build for 149 is expected.

`migration_phase` is the stored staging receipt. The read-only status operation
does not update it after boot. `DKMS_ABI2_ACTIVE` is the current qualification of
the loaded module and running client. The cached 22% is an acknowledged request,
not a tachometer measurement. This evidence does not establish cooling under
load or complete internal firmware restoration.

If this is already your current status, the migration is complete. Do not run
stage again or reinstall from the top-level installer.

## Package and installation

The repository contains the exact contents of the delivered ZIP under
[`migrations/abi2-dkms-1.1.1`](../migrations/abi2-dkms-1.1.1). The original ZIP's
SHA-256 is `8216a003e70f0fa3ce6773273674f350c693a4e97eaaf51bc483ce0af48a6cab`.
The package's manifest and pinned executable sources are unchanged. Its original
validation report records offline preparation; the host result above is subsequent
evidence. The driver and daemon are byte-identical to the root-level ABI 2 files.

Read the complete [Polish instructions](../migrations/abi2-dkms-1.1.1/README-PL.txt)
first. From a downloaded or cloned repository:

```bash
cd migrations/abi2-dkms-1.1.1
sha256sum -c SHA256SUMS
python3 -IB migrate.py                 # plan only, no hardware access
```

Only for the matching existing bundle while still running the verified 147 kernel,
and after all manifest entries report OK:

```bash
sudo /usr/bin/python3 -IB migrate.py stage
```

Do not reboot after a STOP. A successful stage ends with all three markers:

```text
STAGED_FOR_149_REBOOT
RUNNING_147_UNCHANGED
CURVE_UNCHANGED
```

After a successful stage, schedule a normal reboot into 149. Then, from the same
package directory:

```bash
uname -r
sudo /usr/bin/python3 -IB migrate.py status
dkms status
```

No benchmark, fan experiment or additional live control command is part of this
migration. Building uses at most two compiler jobs. Staging writes the new module,
DKMS source, runtime selector, service drop-in and 149 initramfs, and reloads the
systemd unit definitions. It does not stop/restart the running service or issue
module load/unload or firmware commands.

## Later updates and recovery

- DKMS 1.1.1 enables automatic builds for x86-64 `6.8.0-N-generic`, N >=149.
  Review package-update errors and check `migrate.py status` after the reboot.
  Automatic building does not guarantee compatibility with every future update.
- Kernel 147 is excluded from the new package and preserves the known bundle.
  Keep it available during deployment. Another kernel family, changed BIOS/DSDT,
  Secure Boot/lockdown policy, or custom DKMS settings requires separate review.
- Keep `/var/lib/hp-z2-fan-dkms-migration-1.1.1/` and the migration package.
  The persistent receipt and command log support recovery after a partial failure.
- For the original 149 migration, rollback runs only after booting the known 147
  bundle. Follow `README-PL.txt` and use this package's `migrate.py rollback`.
  It restores legacy DKMS 1.0 for 149 and leaves the ABI 2 bundle on 147. It refuses
  modified files or an expanded installation on additional kernels; review that
  state before attempting broader removal. Do not use the older `instaluj.py`
  rollback as a substitute for migration rollback.
- On an unknown firmware result (`min=-2` or SMM timeout), preserve logs before
  deciding recovery. Reloading the module discards its in-memory fault latch.

## Offline checks

From this package directory, without sudo:

```bash
python3 -B tests/check_migration.py
python3 -B source/tests/check_offline.py
```

Preparation included an actual make and isolated DKMS build against official 149
headers, an expected DKMS exclusion for 147, and 34 migration/selector tests.
The [preparation report](../migrations/abi2-dkms-1.1.1/VALIDATION.md) records the
scope and limitations. These tests use temporary files and mocked host commands.
They do not load a kernel module or access the HP firmware.

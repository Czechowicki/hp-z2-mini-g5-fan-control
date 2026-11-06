# ABI 2 → DKMS migration, 2026-11-06

## Outcome

OFFLINE_VALIDATED_FOR_HOST_STAGING. No physical HP access, firmware operations,
module load/unload or service restart was performed. The user must stage the
package on the still-running 147 kernel and verify it after a normal boot of 149.

## Implementation

- The ABI 2 driver and daemon are byte-identical to the already deployed pair.
  Driver SHA-256 `110317f5d17ae8eaa92dbe67e262716b61c947403b70ec2fe2f8cddb540b876c`,
  daemon `fa57684564312bb11484327aa445fa8a740bc774b64b03f6383e6df2ef5fbd88`.
- DKMS packaging version 1.1.1 identifies this bounded migration. It is not a new
  fan protocol. AUTOINSTALL selects the newer DKMS package. BUILD_EXCLUSIVE limits
  it to x86-64 Ubuntu generic 6.8.0 ABI numbers >=149 and excludes the fallback 147.
- New source and runtime directories are separate. The existing 147 selector,
  override, module, initramfs, legacy client and curve are preserved. Migration
  adds a second service drop-in selecting a qualified client at the next start.
- The new selector requires the exact DMI/BIOS/DSDT, ABI 2, unchanged DKMS source
  and client, identical built/selected module contents, matching kernel vermagic,
  and matching GNU build-ID note of the loaded module. It rejects an unknown
  command result. On 147 it delegates to the preserved original selector.
- Persistent recovery receipt is saved before mutations. Failure leaves evidence
  and does not emit a successful staging result. Existing partial state blocks a
  repeat stage. Rollback runs only on the known 147 installation and refuses
  modified files or additional kernels beyond the originally reviewed 149.

## Executed checks

1. Actual make/W=1 build against extracted official Ubuntu 6.8.0-149.149 headers.
   No compile warnings/errors; optional BTF generation skipped because vmlinux is
   absent. This driver does not require BTF for the implemented control path.
2. Actual unprivileged DKMS 3.0.11 build in isolated source/build/module trees.
   Final build succeeded. An initial lab attempt used the legacy absolute header
   path and failed because 149 headers were only extracted locally. The final
   dkms.conf uses DKMS's kernel_source_dir and passed the real build.
   No host install was performed. VM signing was unavailable; host staging checks
   the already-established lockdown/signature policy and does not change it.
3. Actual DKMS attempt for 147 returned expected code 77 due to BUILD_EXCLUSIVE,
   before compilation. This protects the working kernel-specific installation.
4. 34 migration/boot-selector tests passed with temporary files and mocked system
   commands. They cover staging, command failures, persistent recovery state,
   rollback, changed files/PID, missing headers, collision, loaded-note/ABI mismatch,
   daemon tampering, platform refusal, and preservation of the 147 selection.
5. Existing regression script passed, including compiled C transactions using RAM
   mocks, daemon error handling, signal/restore paths, ABI and curve validation.
6. ELF-note parser was additionally exercised on the actual DKMS-built module.
7. Six input files from the previous source worktree retained their SHA-256 values.
   No previous project/laboratory artifact was edited. Input archive/header/package
   hashes and test/build outputs are under validation/.

## Limits

- Module insertion, firmware behavior, complete installation and reboot on HP
  remain untested in this preparation. Static build/tests do not prove host success.
- Initial staging supports the reported 147→149 installation. Additional runtime
  environments, a different kernel family, changed firmware, custom DKMS settings,
  signature enforcement, or conflicting files cause refusal and need review.
- The driver itself remains the existing implementation. Its fixed firmware
  addresses and limited in-kernel platform gating remain a separate concern. The
  selector's firmware check occurs before the daemon starts, not before kernel
  module initialization through modules-load. This migration does not claim to
  solve that independent architectural issue.
- Rollback after later kernels have received 1.1.1 requires reviewing those kernels
  too. The provided automated rollback deliberately covers only the initial 149.
- Dynamic regulation remains the same minimum-speed policy. Cache acknowledgements
  are not RPM measurements or evidence of complete firmware restoration.

## Sources

- [Ubuntu DKMS manual](https://manpages.ubuntu.com/manpages/noble/man8/dkms.8.html)
  for add/build/install, overrides and BUILD_EXCLUSIVE.
- Ubuntu package dkms 3.0.11-1ubuntu13, extracted but not installed in the VM.
  `usr/sbin/dkms:2230` autoinstall selects the latest module version;
  `:2267` reads AUTOINSTALL; build/install obey exclusion. `:1382` can load modules
  if modprobe_on_install is configured, so this installer refuses active custom
  framework settings before changes. Older text at the end of the manual describes
  different multi-version behavior; conclusions here use the actual package source.
- [Official Ubuntu archive](https://archive.ubuntu.com/ubuntu/pool/main/l/linux/)
  headers `linux-headers-6.8.0-149_6.8.0-149.149_all.deb` and
  `linux-headers-6.8.0-149-generic_6.8.0-149.149_amd64.deb`.
- Previous verified HP_FAN_ABI2_INSTALL.zip and ABI 2 source worktree.

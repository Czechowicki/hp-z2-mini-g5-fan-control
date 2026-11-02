# How it works

This is a writeup of how the CPU fan on an HP Z2 Mini G5 (board 8755) can be
driven from a running Linux system. It documents the interface of hardware I
own, for interoperability. It contains **no HP firmware, no disassembly, and no
HP code** — only the externally observable interface and the register addresses
needed to use it.

## The problem

The embedded controller / Super-I/O hardware monitor (a Nuvoton device in the
NPCD318 family) runs the CPU fan on a firmware curve. Three things make it hard
to override from the OS:

1. **Write-lock.** The hardware-monitor register window exposes a lock bit; with
   it set, writes from the OS to the fan registers are ignored. Only code running
   in SMM (System Management Mode) clears it.
2. **No OS knob.** There is no ACPI method or sysfs attribute that raises the fan
   floor. `hp-wmi-sensors` is read-only for fan RPM.
3. **BIOS setting applies only at boot.** The BIOS *"Increase Idle Fan Speed"* /
   fan-floor option exists, but it is written to NVRAM and takes effect at the
   next reboot — useless for dynamic control, and it wears flash.

## Firmware route

The driver implements the thermal SMM request found during analysis of HP
firmware and diagnostic software. Diagnostic menu availability varies. On the
reviewed board the Linux transaction can be observed through the CPU fan
tachometer and the acknowledged minimum command.

## The interface

Three pieces of hardware cooperate for one fan command:

### 1. The SMM command region (`HPSF`)

An ACPI `SystemMemory` operation region (named `HPSF` in the DSDT) lives at a
fixed physical address and holds the command buffer the SMM handler reads:

| Offset | Size | Meaning |
|--------|------|---------|
| `+0x26` | word | payload byte count |
| `+0x28` | 126 bytes | payload (per-channel values) |

On this machine the region is at physical `0xC3EF9000`, length `0xB0`. It is
ACPI NVS / reserved memory, so the kernel maps it with `acpi_os_map_memory()`
(plain `ioremap()` refuses reserved RAM with `-ENOMEM`).

### 2. The doorbell (hardware-monitor register `0xfc0`)

The hardware monitor is reached through a banked I/O window at ports
`0x900`–`0x9FF`, with the bank selector at `0x9FF` (register address = bank in
the high byte, offset in the low byte). Register `0xfc0` is the **doorbell**: you
write the operation code there, and the SMM handler clears it back to `0x00` as
an acknowledgement.

Operation codes observed:

| Op | Meaning | Payload |
|----|---------|---------|
| `0x09` | set per-channel minimum | `payload[channel] = percent` (`0xff` = leave channel alone) |
| `0x0a` | restore snapshot / AUTO | `payload = 1` |

CPU fan is channel `0`. Tachometer for channel 0 is read at register `0x504`.

### 3. The SMI trigger (Super-I/O LDN8, register `0xf7` bit 7)

Writing the doorbell **alone does not run the handler** — nothing happens until
an SMI is raised. The trigger is an edge on bit 7 of register `0xf7` in Super-I/O
logical device 8 (the hardware-monitor LDN), reached through the classic
`0x2e`/`0x2f` index/data pair. Setting the bit, a short delay, then clearing it
raises the eSPI SMI that invokes the handler. (This was the key missing step: the
first attempts wrote only the doorbell and got no acknowledgement.)

### Serialization

`hp-wmi-sensors` and the firmware both touch the hardware monitor, so every
transaction is wrapped in the firmware's own ACPI mutex `\_SB.WMIS.HHHM`
(`acpi_acquire_mutex`), exactly as the WMI driver does, plus a module-level mutex
and `local_irq_save` around the banked-I/O sequences.

## The transaction

With the mutex held:

1. Verify the doorbell reads `0x00` (handler idle).
2. Write the byte count at `HPSF+0x26` and the payload at `HPSF+0x28`.
3. Write the op code to doorbell `0xfc0`.
4. Pulse Super-I/O `LDN8/0xf7` bit 7: set, `udelay(50)`, clear.
5. Poll the doorbell for up to ~30 ms; when it returns to `0x00` the command is
   acknowledged.

To raise the floor to 60%: op `0x09`, payload `[60]`. To hand control back to the
firmware: op `0x0a`, payload `[1]`.

## Acknowledgement and timeout

The runtime request targets the hardware monitor's RAM. On normal unload
after an acknowledged minimum, the module requests restore. If firmware leaves
the doorbell pending past the polling limit, the driver returns `ETIMEDOUT`,
publishes `min=-2` and blocks further commands. The tachometer provides a
separate observation of the fan response.

## Verifying on your own machine

The `tools/` modules reproduce the path in isolation so you can confirm the
register map before trusting the main module:

- `hp_smm_probe` — read-only: reads RPM and the doorbell, no SMI.
- `hp_smm_fire` — fires the SMI with an empty command to confirm the trigger.
- `hp_smm_apply` — sets a minimum, observes RPM for a few seconds, restores.
  `insmod hp_smm_apply.ko apply=1 percent=60 hold_sec=8`

All of them carry the same DMI guard and refuse to load on other boards.

## Scope and caveats

- Addresses here (`0xC3EF9000`, `0xfc0`, `0x504`, the `0xf7` bit, the mutex path)
  are specific to board 8755 / BIOS S50. On another board or a very different BIOS
  the DSDT names and the SMM region address can differ. The module checks board and product at load.
  The source installer also checks the specific BIOS version and DSDT digest.
- This is unofficial and unsupported by HP. Use at your own risk.

// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * hp_smm_apply - ustaw minimum obrotow CPU Fan NA ZYWO przez uchwyt SMM HP.
 *
 * Wykorzystuje potwierdzona sciezke: HPSF (command buffer) + dzwonek HWM 0xfc0
 * + zbocze Super I/O LDN8/F7.bit7 -> SMI -> uchwyt SMM 0410. Uchwyt ma prawo
 * chwilowo zdjac write-lock i zapisac minimum (0x3d8+kanal), wiec zmiana dziala
 * bez restartu. To ta sama operacja co BIOS "Increase Idle Fan Speed", ale
 * stosowana w runtime.
 *
 * Komendy (op do dzwonka 0xfc0):
 *   0x09 = min   (payload[ch] = procent 0..100, 0xff = pomin kanal)
 *   0x0a = restore (payload = 1; przywraca snapshot/AUTO)
 *
 * Parametry:
 *   apply=1   wykonaj: min(percent) na kanale 0, obserwuj RPM hold_sec s, restore.
 *   percent=  docelowe minimum w % (domyslnie 60 ~ 3000 RPM). Zakres 30..100.
 *   hold_sec= ile sekund trzymac i obserwowac przed przywroceniem (domyslnie 8).
 *   keep=1    NIE przywracaj na koniec (zostaw min ustawione). Domyslnie przywraca.
 *
 * Domyslnie apply=0: tylko odczyt RPM, nic nie wysyla.
 * Caly dostep pod mutexem ACPI \_SB.WMIS.HHHM.
 */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/acpi.h>
#include <linux/delay.h>
#include <linux/dmi.h>
#include <linux/io.h>
#include <linux/ioport.h>
#include <linux/irqflags.h>
#include <linux/module.h>
#include <linux/mutex.h>

#define HWM_BASE	0x900
#define HWM_LEN		0x100
#define HWM_BANK	(HWM_BASE + 0xff)
#define HWM_BANK_MASK	0x1f
#define SIO_IDX		0x2e
#define SIO_DATA	0x2f
#define SIO_LDN_REG	0x07
#define SIO_HWM_LDN	0x08
#define SIO_F7		0xf7

#define REG_DOORBELL	0xfc0
#define REG_TACH0	0x504
#define OP_MIN		0x09
#define OP_RESTORE	0x0a
#define CPU_CHANNEL	0

#define HPSF_PHYS	0xC3EF9000UL
#define HPSF_LEN	0xB0
#define HPSF_COUNT	0x26
#define HPSF_PAYLOAD	0x28
#define HPSF_PAYLOAD_MAX 126

#define AML_MUTEX	"\\_SB.WMIS.HHHM"
#define AML_TIMEOUT	1000

static bool apply;
static int percent = 60;
static int hold_sec = 8;
static bool keep;
module_param(apply, bool, 0444);
module_param(percent, int, 0444);
module_param(hold_sec, int, 0444);
module_param(keep, bool, 0444);
MODULE_PARM_DESC(apply, "1 = ustaw min i obserwuj (domyslnie 0 = tylko odczyt RPM)");
MODULE_PARM_DESC(percent, "docelowe minimum w % (30..100, domyslnie 60)");
MODULE_PARM_DESC(hold_sec, "sekundy obserwacji przed przywroceniem (domyslnie 8)");
MODULE_PARM_DESC(keep, "1 = nie przywracaj (zostaw min). Domyslnie przywraca AUTO");

static struct {
	struct mutex lock;
	acpi_handle aml;
	struct resource *region;
	void __iomem *hpsf;
} a;

static int lock_hw(void)
{
	acpi_status st;

	mutex_lock(&a.lock);
	st = acpi_acquire_mutex(a.aml, NULL, AML_TIMEOUT);
	if (ACPI_FAILURE(st)) {
		mutex_unlock(&a.lock);
		return -EBUSY;
	}
	return 0;
}

static void unlock_hw(void)
{
	acpi_release_mutex(a.aml, NULL);
	mutex_unlock(&a.lock);
}

static u8 hwm_r(u16 reg)
{
	u8 bank = (reg >> 8) & HWM_BANK_MASK, prev, v;
	unsigned long f;

	local_irq_save(f);
	prev = inb(HWM_BANK);
	if (prev != bank)
		outb(bank, HWM_BANK);
	v = inb(HWM_BASE + (reg & 0xff));
	if (prev != bank)
		outb(prev, HWM_BANK);
	local_irq_restore(f);
	return v;
}

static u16 hwm_rw(u16 reg)
{
	return hwm_r(reg) | (hwm_r(reg + 1) << 8);
}

static void doorbell_w(u8 val)
{
	u8 bank = (REG_DOORBELL >> 8) & HWM_BANK_MASK, prev;
	unsigned long f;

	local_irq_save(f);
	prev = inb(HWM_BANK);
	if (prev != bank)
		outb(bank, HWM_BANK);
	outb(val, HWM_BASE + (REG_DOORBELL & 0xff));
	if (prev != bank)
		outb(prev, HWM_BANK);
	local_irq_restore(f);
}

static u8 sio_f7_read(void)
{
	unsigned long f;
	u8 idx, ldn, v;

	local_irq_save(f);
	idx = inb(SIO_IDX);
	outb(SIO_LDN_REG, SIO_IDX);
	ldn = inb(SIO_DATA);
	if (ldn != SIO_HWM_LDN)
		outb(SIO_HWM_LDN, SIO_DATA);
	outb(SIO_F7, SIO_IDX);
	v = inb(SIO_DATA);
	if (ldn != SIO_HWM_LDN) {
		outb(SIO_LDN_REG, SIO_IDX);
		outb(ldn, SIO_DATA);
	}
	outb(idx, SIO_IDX);
	local_irq_restore(f);
	return v;
}

static void sio_f7_write(u8 val)
{
	unsigned long f;
	u8 idx, ldn;

	local_irq_save(f);
	idx = inb(SIO_IDX);
	outb(SIO_LDN_REG, SIO_IDX);
	ldn = inb(SIO_DATA);
	if (ldn != SIO_HWM_LDN)
		outb(SIO_HWM_LDN, SIO_DATA);
	outb(SIO_F7, SIO_IDX);
	outb(val, SIO_DATA);
	if (ldn != SIO_HWM_LDN) {
		outb(SIO_LDN_REG, SIO_IDX);
		outb(ldn, SIO_DATA);
	}
	outb(idx, SIO_IDX);
	local_irq_restore(f);
}

/*
 * Jedna transakcja do uchwytu SMM. Buduje bufor w HPSF (+0x26 count, +0x28
 * payload; pola +0..+0x25 i +0xa6.. nietkniete), bije w dzwonek, generuje
 * zbocze F7.bit7, czeka na ACK (dzwonek -> 0). Zwraca true gdy ACK.
 * Wolac z trzymanym mutexem HHHM.
 */
static bool smm_cmd_locked(u8 op, u16 count, const u8 *payload, unsigned int plen)
{
	u8 f7, door = hwm_r(REG_DOORBELL);
	unsigned int i;

	if (door != 0x00) {
		pr_warn("dzwonek zajety (0x%02x) - pomijam komende 0x%02x\n", door, op);
		return false;
	}
	writew(count, a.hpsf + HPSF_COUNT);
	for (i = 0; i < HPSF_PAYLOAD_MAX; i++)
		writeb(i < plen ? payload[i] : 0x00, a.hpsf + HPSF_PAYLOAD + i);
	wmb();

	doorbell_w(op);
	f7 = sio_f7_read();
	sio_f7_write(f7 | 0x80);
	udelay(50);
	sio_f7_write(f7 & ~0x80);

	for (i = 0; i < 60; i++) {
		if (hwm_r(REG_DOORBELL) == 0x00)
			return true;
		udelay(500);
	}
	doorbell_w(0x00);	/* sprzatamy jesli brak ACK */
	return false;
}

static u16 read_rpm(void)
{
	u16 v;

	if (lock_hw())
		return 0;
	v = hwm_rw(REG_TACH0);
	unlock_hw();
	return v;
}

static int __init hp_smm_apply_init(void)
{
	const char *prod = dmi_get_system_info(DMI_PRODUCT_NAME);
	acpi_object_type t;
	u8 pl[1];
	bool ok;
	int i, ret;

	if (!dmi_match(DMI_BOARD_NAME, "8755") || !prod || !strstr(prod, "Z2 Mini G5"))
		return -ENODEV;
	if (ACPI_FAILURE(acpi_get_handle(NULL, AML_MUTEX, &a.aml)) ||
	    ACPI_FAILURE(acpi_get_type(a.aml, &t)) || t != ACPI_TYPE_MUTEX)
		return -ENODEV;
	if (percent < 30)
		percent = 30;
	if (percent > 100)
		percent = 100;

	mutex_init(&a.lock);
	a.region = request_region(HWM_BASE, HWM_LEN, KBUILD_MODNAME);

	pr_info("baseline RPM=%u\n", read_rpm());
	if (!apply) {
		pr_info("tryb tylko-odczyt. Ustawienie: insmod ... apply=1 percent=60\n");
		ret = 0;
		goto out;
	}

	a.hpsf = acpi_os_map_memory(HPSF_PHYS, HPSF_LEN);
	if (!a.hpsf) {
		pr_err("nie zmapowano HPSF\n");
		ret = -ENOMEM;
		goto out;
	}

	/* min(percent) na kanale CPU=0 */
	pl[0] = (u8)percent;
	ret = lock_hw();
	if (ret)
		goto out_unmap;
	ok = smm_cmd_locked(OP_MIN, 1, pl, 1);
	unlock_hw();
	pr_info("min %d%% na kanale %d: ACK=%s\n", percent, CPU_CHANNEL, ok ? "TAK" : "nie");

	for (i = 1; i <= hold_sec; i++) {
		msleep(1000);
		pr_info("  t=%ds RPM=%u\n", i, read_rpm());
	}

	if (keep) {
		pr_info("keep=1: zostawiam min %d%%. Powrot: insmod ... apply=1 percent=... albo osobny restore.\n",
			percent);
	} else {
		pl[0] = 1;
		ret = lock_hw();
		if (!ret) {
			ok = smm_cmd_locked(OP_RESTORE, 1, pl, 1);
			unlock_hw();
			pr_info("restore (AUTO): ACK=%s\n", ok ? "TAK" : "nie");
		}
		msleep(1500);
		pr_info("po restore RPM=%u\n", read_rpm());
	}
	ret = 0;

out_unmap:
	if (a.hpsf)
		acpi_os_unmap_memory(a.hpsf, HPSF_LEN);
out:
	if (a.region)
		release_region(HWM_BASE, HWM_LEN);
	a.region = NULL;
	return ret;
}
module_init(hp_smm_apply_init);

static void __exit hp_smm_apply_exit(void) { }
module_exit(hp_smm_apply_exit);

MODULE_DESCRIPTION("HP Z2 Mini G5 - ustawianie min obrotow przez uchwyt SMM (runtime)");
MODULE_LICENSE("GPL");

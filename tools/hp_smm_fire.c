// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * hp_smm_fire - TEST wyzwalacza uchwytu SMM HP Thermal (count=0, bez zmiany obrotow).
 *
 * Odtwarza transakcje, ktora diagnostyk HP (HP PC Hardware Diagnostics UEFI)
 * wysyla do uchwytu SMM 0410, ale z komenda MAX o count=0 - wg analizy firmware
 * jest to ACK bez wymuszenia jakiegokolwiek kanalu, wiec zaden wentylator nie
 * zmienia obrotow. Cel: sprawdzic, czy uchwyt SMM w ogole odpowiada z poziomu
 * dzialajacego Ubuntu (czy wyczysci dzwonek 0xfc0 / ruszy flagi skrzynki).
 *
 * Sekwencja (jak DXE040F 0x1984->0x1fd4):
 *   HPSF+0x26 = count(WORD) = 0;  HPSF+0x28..+0xa5 = payload = 0  (pola +0..+0x25
 *     i +0xa6.. nalezace do WMI/BIOS NIE sa dotykane)
 *   dzwonek HWM 0xfc0 = 0x11 (op MAX)      <- to prawdopodobnie generuje SMI
 *   0xc50.bit7 (wake) utrzymany (u nas juz=1)
 *   po tym: odczyt 0xfc0 (ACK gdy wroci 0) oraz flag 0xc40/0xcd8.
 *
 * Domyslnie fire=0: tylko odczyt baseline, NIC nie wyzwala. fire=1: wykonuje test.
 * Caly dostep pod mutexem ACPI \_SB.WMIS.HHHM (serializacja ze sciezka czujnikow).
 *
 * RYZYKO: to realne wywolanie SMM. Przy fire=1 moze nastapic chwilowy stall
 * wszystkich rdzeni. Petle poll w firmware sa ograniczone, ale na produkcji
 * uruchamiaj swiadomie.
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
#define REG_WAKE	0xc50
#define REG_FLAG1	0xc40
#define REG_FLAG2	0xcd8
#define REG_TACH0	0x504	/* CPU Fan tach, WORD */

#define OP_MAX		0x11

#define HPSF_PHYS	0xC3EF9000UL
#define HPSF_LEN	0xB0
#define HPSF_COUNT	0x26	/* WORD */
#define HPSF_PAYLOAD	0x28	/* 126 B */
#define HPSF_PAYLOAD_LEN 126

#define AML_MUTEX	"\\_SB.WMIS.HHHM"
#define AML_TIMEOUT	1000

static bool fire;
module_param(fire, bool, 0444);
MODULE_PARM_DESC(fire, "1 = wykonaj test wyzwalacza SMM (count=0). Domyslnie 0 = tylko odczyt.");

static struct {
	struct mutex lock;
	acpi_handle aml;
	struct resource *region;
} s;

static int lock_hw(void)
{
	acpi_status st;

	mutex_lock(&s.lock);
	st = acpi_acquire_mutex(s.aml, NULL, AML_TIMEOUT);
	if (ACPI_FAILURE(st)) {
		mutex_unlock(&s.lock);
		return -EBUSY;
	}
	return 0;
}

static void unlock_hw(void)
{
	acpi_release_mutex(s.aml, NULL);
	mutex_unlock(&s.lock);
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

/* Super I/O: odczyt/zapis LDN8/0xF7 (zachowuje inne bity; tylko do wyzwolenia). */
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

/* Zapis TYLKO do dzwonka 0xfc0. */
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

static int __init hp_smm_fire_init(void)
{
	const char *prod = dmi_get_system_info(DMI_PRODUCT_NAME);
	acpi_object_type t;
	void __iomem *hpsf;
	u8 door0, wake, f1a, f2a, f1b, f2b, door_ack = 0xff, f7_orig = 0;
	u16 rpm_before, rpm_after;
	bool acked = false;
	int i, ret;

	if (!dmi_match(DMI_BOARD_NAME, "8755") || !prod || !strstr(prod, "Z2 Mini G5"))
		return -ENODEV;
	if (ACPI_FAILURE(acpi_get_handle(NULL, AML_MUTEX, &s.aml)) ||
	    ACPI_FAILURE(acpi_get_type(s.aml, &t)) || t != ACPI_TYPE_MUTEX) {
		pr_err("brak mutexa %s\n", AML_MUTEX);
		return -ENODEV;
	}
	mutex_init(&s.lock);
	s.region = request_region(HWM_BASE, HWM_LEN, KBUILD_MODNAME);

	ret = lock_hw();
	if (ret) {
		pr_err("timeout mutexa AML\n");
		goto out_region;
	}
	door0 = hwm_r(REG_DOORBELL);
	wake = hwm_r(REG_WAKE);
	f1a = hwm_r(REG_FLAG1);
	f2a = hwm_r(REG_FLAG2);
	rpm_before = hwm_rw(REG_TACH0);
	unlock_hw();

	pr_info("baseline: door(0xfc0)=0x%02x wake(0xc50)=0x%02x flag(0xc40)=0x%02x flag(0xcd8)=0x%02x tach0=%u RPM\n",
		door0, wake, f1a, f2a, rpm_before);

	if (!fire) {
		pr_info("tryb tylko-odczyt. Test wyzwalacza: insmod ... fire=1\n");
		ret = 0;
		goto out_region;
	}
	if (door0 != 0x00) {
		pr_warn("dzwonek nie jest wolny (0x%02x) - przerywam, nie wyzwalam\n", door0);
		ret = -EBUSY;
		goto out_region;
	}

	/* HPSF to ACPI SystemMemory (NVS/reserved) - mapujemy jak ACPICA, nie ioremap. */
	hpsf = acpi_os_map_memory(HPSF_PHYS, HPSF_LEN);
	if (!hpsf) {
		pr_err("nie zmapowano HPSF @0x%lx\n", HPSF_PHYS);
		ret = -ENOMEM;
		goto out_region;
	}

	ret = lock_hw();
	if (ret)
		goto out_unmap;

	/* komenda MAX, count=0: tylko pola +0x26 i +0x28.. (reszta HPSF nietknieta) */
	writew(0x0000, hpsf + HPSF_COUNT);
	for (i = 0; i < HPSF_PAYLOAD_LEN; i++)
		writeb(0x00, hpsf + HPSF_PAYLOAD + i);
	wmb();	/* HPSF widoczne zanim uderzymy w dzwonek */

	f7_orig = sio_f7_read();
	pr_info("wyzwalam: HPSF count=0, dzwonek 0xfc0 <- 0x%02x, zbocze SIO F7.bit7 (F7=0x%02x)\n",
		OP_MAX, f7_orig);
	doorbell_w(OP_MAX);

	/* Zbocze na LDN8/F7.bit7 = kandydat na zrodlo SMI (eSPI). Robimy narastajace
	 * i opadajace, zeby pokryc oba kierunki; inne bity F7 zachowane. */
	sio_f7_write(f7_orig | 0x80);
	udelay(50);
	sio_f7_write(f7_orig & ~0x80);

	/* uchwyt SMM, jesli zadziala, wyczysci dzwonek (ACK). Polling ~20 ms. */
	for (i = 0; i < 40; i++) {
		door_ack = hwm_r(REG_DOORBELL);
		if (door_ack == 0x00) {
			acked = true;
			break;
		}
		udelay(500);
	}
	sio_f7_write(f7_orig);	/* przywroc F7 do stanu wyjsciowego */
	f1b = hwm_r(REG_FLAG1);
	f2b = hwm_r(REG_FLAG2);
	rpm_after = hwm_rw(REG_TACH0);

	if (!acked)
		doorbell_w(0x00);	/* sprzatamy nasza stala komende */

	unlock_hw();

	pr_info("WYNIK: ACK=%s (door po=0x%02x)  flagi 0xc40 %02x->%02x  0xcd8 %02x->%02x  tach0 %u->%u RPM\n",
		acked ? "TAK" : "nie", door_ack, f1a, f1b, f2a, f2b, rpm_before, rpm_after);
	if (acked)
		pr_info("   -> uchwyt SMM ODPOWIADA z OS. Sciezka zywa. Nastepny krok: komenda zmieniajaca obroty.\n");
	else
		pr_info("   -> brak ACK: uchwyt nie odpowiedzial po starcie systemu (lub SMI nie powstaje z tego zapisu).\n");

	ret = 0;
out_unmap:
	acpi_os_unmap_memory(hpsf, HPSF_LEN);
out_region:
	if (s.region)
		release_region(HWM_BASE, HWM_LEN);
	s.region = NULL;
	return ret;
}
module_init(hp_smm_fire_init);

static void __exit hp_smm_fire_exit(void) { }
module_exit(hp_smm_fire_exit);

MODULE_DESCRIPTION("HP Z2 Mini G5 - test wyzwalacza uchwytu SMM (count=0)");
MODULE_LICENSE("GPL");

// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * hp_smm_probe - READ-ONLY sonda interfejsu dzwonka HP Thermal (HPSF/HWM).
 *
 * Cel: ustalić, czy z działającego Ubuntu da się odtworzyć wyzwalacz, którym
 * diagnostyk HP (HP PC Hardware Diagnostics UEFI) prosi uchwyt SMM o zmianę
 * obrotów. Diagnostyk pisze numer komendy do rejestru HWM 0xfc0, ustawia bit
 * w 0xc50 i szarpie Super I/O LDN8/0xF7.bit7, co generuje SMI do uchwytu SMM.
 *
 * Ten moduł NICZEGO nie wyzwala i domyślnie NICZEGO nie zapisuje. Czyta stan
 * rejestrów dzwonka/skrzynki i konfiguracji Super I/O, pod mutexem ACPI
 * \_SB.WMIS.HHHM (tym samym, którego używa odczyt czujników), żeby nie wejść
 * w kolizję ze ścieżką RPM. Bankowany dostep HWM jak w hp-wmi-sensors:
 * selektor banku 0x9ff, dane 0x900+offset.
 *
 * allow_doorbell_write=1 pozwala na JEDEN ostrozny test zapisu do 0xfc0
 * (zapis wartosci testowej, odczyt, przywrocenie oryginalu) BEZ szarpania
 * bitem SMI - czyli bez budzenia uchwytu SMM. To odpowiada na pytanie:
 * czy blokada zapisu obejmuje tez rejestr-dzwonek. Nic nie generuje SMI.
 *
 * Mapa rejestrow z analizy statycznej DXE040F/SMM0410 (S50) + DSDT.
 */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/acpi.h>
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

#define SIO_IDX		0x2e	/* Super I/O config index */
#define SIO_DATA	0x2f
#define SIO_LDN_REG	0x07
#define SIO_HWM_LDN	0x08
#define SIO_F7		0xf7

#define AML_MUTEX	"\\_SB.WMIS.HHHM"
#define AML_TIMEOUT	1000

/* Rejestry dzwonka/skrzynki (HWM, logiczne 0xPRR = bank P, offset RR). */
#define REG_CTRL	0x004	/* bit0 = write-lock/active */
#define REG_DOORBELL	0xfc0	/* numer komendy dla uchwytu SMM */
#define REG_WAKE	0xc50	/* bit7 = wake interfejsu */
#define REG_MBX_FLAG1	0xc40	/* bit4 w handshake */
#define REG_MBX_FLAG2	0xcd8	/* bit0 w handshake */

static bool allow_doorbell_write;
module_param(allow_doorbell_write, bool, 0444);
MODULE_PARM_DESC(allow_doorbell_write,
	"Jeden test zapisu do 0xfc0 z przywroceniem, BEZ generowania SMI (domyslnie nie)");

static struct {
	struct mutex lock;
	acpi_handle aml;
	struct resource *region;
} p;

static int lock_hw(void)
{
	acpi_status s;

	mutex_lock(&p.lock);
	s = acpi_acquire_mutex(p.aml, NULL, AML_TIMEOUT);
	if (ACPI_FAILURE(s)) {
		mutex_unlock(&p.lock);
		return -EBUSY;
	}
	return 0;
}

static void unlock_hw(void)
{
	acpi_release_mutex(p.aml, NULL);
	mutex_unlock(&p.lock);
}

static u8 hwm_read(u16 reg)
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

/* Zapis TYLKO do 0xfc0 (dzwonek). Zadnej innej wartosci/rejestru. */
static void doorbell_write(u8 val)
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

/* Super I/O: odczyt LDN8/0xF7 (tylko odczyt; zapisu konfiguracji nie robimy). */
static int sio_read_f7(u8 *out)
{
	unsigned long f;
	u8 saved_idx, saved_ldn, v;

	if (!request_region(SIO_IDX, 2, KBUILD_MODNAME)) {
		/* ktos trzyma 0x2e/0x2f; czytamy ostroznie bez rezerwacji */
	}
	local_irq_save(f);
	saved_idx = inb(SIO_IDX);
	outb(SIO_LDN_REG, SIO_IDX);
	saved_ldn = inb(SIO_DATA);
	if (saved_ldn != SIO_HWM_LDN)
		outb(SIO_HWM_LDN, SIO_DATA);
	outb(SIO_F7, SIO_IDX);
	v = inb(SIO_DATA);
	if (saved_ldn != SIO_HWM_LDN) {
		outb(SIO_LDN_REG, SIO_IDX);
		outb(saved_ldn, SIO_DATA);
	}
	outb(saved_idx, SIO_IDX);
	local_irq_restore(f);
	release_region(SIO_IDX, 2);
	*out = v;
	return 0;
}

static int __init hp_smm_probe_init(void)
{
	const char *prod = dmi_get_system_info(DMI_PRODUCT_NAME);
	acpi_object_type t;
	u8 ctrl, door, wake, f1, f2, f7, bank, door2 = 0;
	int ret;

	if (!dmi_match(DMI_BOARD_NAME, "8755") || !prod || !strstr(prod, "Z2 Mini G5"))
		return -ENODEV;
	if (ACPI_FAILURE(acpi_get_handle(NULL, AML_MUTEX, &p.aml)) ||
	    ACPI_FAILURE(acpi_get_type(p.aml, &t)) || t != ACPI_TYPE_MUTEX) {
		pr_err("brak mutexa %s\n", AML_MUTEX);
		return -ENODEV;
	}
	mutex_init(&p.lock);
	p.region = request_region(HWM_BASE, HWM_LEN, KBUILD_MODNAME);

	ret = lock_hw();
	if (ret) {
		pr_err("timeout mutexa AML\n");
		goto out_region;
	}
	bank = inb(HWM_BANK);
	if (bank > HWM_BANK_MASK) {
		unlock_hw();
		pr_err("nieprawidlowy selektor banku 0x%02x, nie dotykam interfejsu\n", bank);
		ret = -ENODEV;
		goto out_region;
	}
	ctrl = hwm_read(REG_CTRL);
	door = hwm_read(REG_DOORBELL);
	wake = hwm_read(REG_WAKE);
	f1 = hwm_read(REG_MBX_FLAG1);
	f2 = hwm_read(REG_MBX_FLAG2);
	unlock_hw();

	sio_read_f7(&f7);

	pr_info("ODCZYT: ctrl(0x004)=0x%02x door(0xfc0)=0x%02x wake(0xc50)=0x%02x flag(0xc40)=0x%02x flag(0xcd8)=0x%02x  SIO LDN8/F7=0x%02x\n",
		ctrl, door, wake, f1, f2, f7);
	pr_info("        write-lock bit0(0x004)=%u  wake bit7(0xc50)=%u  F7 bit7=%u\n",
		ctrl & 1, wake >> 7, f7 >> 7);

	if (allow_doorbell_write) {
		u8 testv = door ^ 0x01;	/* minimalna zmiana, latwa do cofniecia */

		ret = lock_hw();
		if (ret)
			goto out_region;
		doorbell_write(testv);
		door2 = hwm_read(REG_DOORBELL);
		doorbell_write(door);	/* przywroc oryginal */
		unlock_hw();

		if (door2 == testv)
			pr_info("ZAPIS 0xfc0: PRZESZEDL (0x%02x->0x%02x, przywrocono 0x%02x). Dzwonek jest zapisywalny z OS.\n",
				door, testv, door);
		else
			pr_info("ZAPIS 0xfc0: ODRZUCONY (zapisano 0x%02x, odczyt 0x%02x). Dzwonek jest zablokowany jak reszta okna.\n",
				testv, door2);
	} else {
		pr_info("tryb tylko-odczyt. Zapis testowy dzwonka: insmod ... allow_doorbell_write=1\n");
	}

	ret = 0;	/* sukces; wynik w dmesg, potem: sudo rmmod hp_smm_probe */
out_region:
	if (p.region)
		release_region(HWM_BASE, HWM_LEN);
	p.region = NULL;
	return ret;
}
module_init(hp_smm_probe_init);

static void __exit hp_smm_probe_exit(void) { }
module_exit(hp_smm_probe_exit);

MODULE_DESCRIPTION("HP Z2 Mini G5 - read-only sonda dzwonka HPSF/HWM");
MODULE_LICENSE("GPL");

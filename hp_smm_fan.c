// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * hp_smm_fan - mechanizm zywego sterowania minimum obrotow CPU Fan na HP Z2
 * Mini G5, przez uchwyt SMM HP Thermal (potwierdzona sciezka HPSF + dzwonek
 * 0xfc0 + zbocze Super I/O LDN8/F7.bit7).
 *
 * To jest TYLKO mechanizm. Polityke (krzywa temperatura->obroty) realizuje
 * demon w userspace, piszac procent do debugfs. Zapis idzie do HWM RAM przez
 * SMM (nie NVRAM), wiec mozna wolac czesto bez zuzycia flash.
 *
 * debugfs /sys/kernel/debug/hp_smm_fan/ (root):
 *   set   zapis "0".."100" -> ustaw minimum % na kanale CPU (0) na zywo
 *         zapis "auto"      -> wyslij zadanie przywrocenia
 *   rpm   odczyt            -> biezace obroty CPU Fan
 *   min   odczyt            -> cache polecenia (-1 = start/auto, -2 = wynik nieznany)
 *
 * Przy wyladowaniu modul zleca AUTO, gdy znany jest ostatni zapis minimum.
 */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/acpi.h>
#include <linux/debugfs.h>
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
#define MIN_AUTO	(-1)
#define MIN_UNKNOWN	(-2)

#define HPSF_PHYS	0xC3EF9000UL
#define HPSF_LEN	0xB0
#define HPSF_COUNT	0x26
#define HPSF_PAYLOAD	0x28
#define HPSF_PAYLOAD_MAX 126

#define AML_MUTEX	"\\_SB.WMIS.HHHM"
#define AML_TIMEOUT	1000

/* Indicates timeout latching with the original one-write command interface. */
static u32 control_abi = 2;

static struct {
	struct mutex lock;
	acpi_handle aml;
	struct resource *region;
	void __iomem *hpsf;
	struct dentry *dir;
	int cur_min;	/* chronione przez g.lock */
} g;

static int lock_hw(void)
{
	acpi_status st;

	mutex_lock(&g.lock);
	st = acpi_acquire_mutex(g.aml, NULL, AML_TIMEOUT);
	if (ACPI_FAILURE(st)) {
		mutex_unlock(&g.lock);
		return -EBUSY;
	}
	return 0;
}

static void unlock_hw(void)
{
	acpi_release_mutex(g.aml, NULL);
	mutex_unlock(&g.lock);
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

/* Jedna transakcja SMM. Wolac z trzymanym HHHM. */
static int smm_cmd_locked(u8 op, u16 count, const u8 *payload, unsigned int plen)
{
	u8 f7;
	unsigned int i;

	if (hwm_r(REG_DOORBELL) != 0x00)
		return -EBUSY;
	writew(count, g.hpsf + HPSF_COUNT);
	for (i = 0; i < HPSF_PAYLOAD_MAX; i++)
		writeb(i < plen ? payload[i] : 0x00, g.hpsf + HPSF_PAYLOAD + i);
	wmb();

	doorbell_w(op);
	f7 = sio_f7_read();
	sio_f7_write(f7 | 0x80);
	udelay(50);
	sio_f7_write(f7 & ~0x80);

	for (i = 0; i < 60; i++) {
		if (hwm_r(REG_DOORBELL) == 0x00)
			return 0;
		udelay(500);
	}
	/* Firmware may still own this request and the shared payload. */
	return -ETIMEDOUT;
}

static int apply_setting(int minimum)
{
	bool restore = minimum == MIN_AUTO;
	u8 payload = restore ? 1 : minimum;
	int ret;

	ret = lock_hw();
	if (ret)
		return ret;

	if (g.cur_min == MIN_UNKNOWN) {
		ret = -EUCLEAN;
	} else {
		ret = smm_cmd_locked(restore ? OP_RESTORE : OP_MIN, 1, &payload, 1);
		if (!ret)
			g.cur_min = minimum;
		else if (ret == -ETIMEDOUT) {
			g.cur_min = MIN_UNKNOWN;
			pr_err("SMM completion unknown; control writes disabled for this module instance\n");
		}
	}
	unlock_hw();
	return ret;
}

static ssize_t set_write(struct file *file, const char __user *ubuf,
			 size_t count, loff_t *ppos)
{
	char buf[16];
	char *value;
	int ret, v;

	if (!count || count >= sizeof(buf))
		return -EINVAL;
	if (copy_from_user(buf, ubuf, count))
		return -EFAULT;
	if (memchr(buf, '\0', count))
		return -EINVAL;
	buf[count] = '\0';
	value = strim(buf);
	if (!strcmp(value, "auto"))
		v = MIN_AUTO;
	else if (kstrtoint(value, 10, &v) || v < 0 || v > 100)
		return -EINVAL;
	ret = apply_setting(v);
	return ret ? ret : count;
}

static const struct file_operations set_fops = {
	.owner = THIS_MODULE,
	.open = simple_open,
	.write = set_write,
	.llseek = noop_llseek,
};

static int rpm_get(void *data, u64 *val)
{
	int ret = lock_hw();

	if (ret)
		return ret;
	*val = hwm_rw(REG_TACH0);
	unlock_hw();
	return 0;
}
DEFINE_DEBUGFS_ATTRIBUTE(rpm_fops, rpm_get, NULL, "%llu\n");

static int min_get(void *data, u64 *val)
{
	mutex_lock(&g.lock);
	*val = (u64)(s64)g.cur_min;
	mutex_unlock(&g.lock);
	return 0;
}
DEFINE_DEBUGFS_ATTRIBUTE(min_fops, min_get, NULL, "%lld\n");

static int __init hp_smm_fan_init(void)
{
	const char *prod = dmi_get_system_info(DMI_PRODUCT_NAME);
	acpi_object_type t;

	if (!dmi_match(DMI_BOARD_NAME, "8755") || !prod || !strstr(prod, "Z2 Mini G5"))
		return -ENODEV;
	if (ACPI_FAILURE(acpi_get_handle(NULL, AML_MUTEX, &g.aml)) ||
	    ACPI_FAILURE(acpi_get_type(g.aml, &t)) || t != ACPI_TYPE_MUTEX)
		return -ENODEV;

	mutex_init(&g.lock);
	g.cur_min = MIN_AUTO;
	g.region = request_region(HWM_BASE, HWM_LEN, KBUILD_MODNAME);
	if (!g.region)
		return -EBUSY;
	g.hpsf = acpi_os_map_memory(HPSF_PHYS, HPSF_LEN);
	if (!g.hpsf) {
		pr_err("nie zmapowano HPSF\n");
		if (g.region)
			release_region(HWM_BASE, HWM_LEN);
		return -ENOMEM;
	}

	g.dir = debugfs_create_dir(KBUILD_MODNAME, NULL);
	if (IS_ERR_OR_NULL(g.dir)) {
		int err = g.dir ? PTR_ERR(g.dir) : -ENOMEM;

		acpi_os_unmap_memory(g.hpsf, HPSF_LEN);
		release_region(HWM_BASE, HWM_LEN);
		return err;
	}
	debugfs_create_u32("control_abi", 0400, g.dir, &control_abi);
	debugfs_create_file("set", 0200, g.dir, NULL, &set_fops);
	debugfs_create_file("rpm", 0444, g.dir, NULL, &rpm_fops);
	debugfs_create_file("min", 0444, g.dir, NULL, &min_fops);

	pr_info("gotowe. echo 0..100 > /sys/kernel/debug/%s/set  (albo 'auto')\n",
		KBUILD_MODNAME);
	return 0;
}
module_init(hp_smm_fan_init);

static void __exit hp_smm_fan_exit(void)
{
	debugfs_remove_recursive(g.dir);
	if (g.cur_min >= 0 && apply_setting(MIN_AUTO))
		pr_err("restore request failed during unload\n");
	if (g.cur_min == MIN_UNKNOWN)
		pr_err("unloading with unknown firmware state\n");
	if (g.hpsf)
		acpi_os_unmap_memory(g.hpsf, HPSF_LEN);
	if (g.region)
		release_region(HWM_BASE, HWM_LEN);
}
module_exit(hp_smm_fan_exit);

MODULE_DESCRIPTION("HP Z2 Mini G5 - zywe sterowanie min obrotow CPU przez uchwyt SMM");
MODULE_LICENSE("GPL");

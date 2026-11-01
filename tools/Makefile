obj-m := hp_smm_probe.o hp_smm_fire.o hp_smm_apply.o
KDIR ?= /lib/modules/$(shell uname -r)/build
all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
.PHONY: all clean

# candidate-5b-safe: same module name as candidate-4 (edgefhe_v8b_dma_proxy.ko), two objects. Only ef_v8b_vec.o is
# built without -mgeneral-regs-only (as the kernel's lib/raid6 NEON objects are); it is called only inside
# kernel_neon_begin/end.
obj-m += edgefhe_v8b_dma_proxy.o
edgefhe_v8b_dma_proxy-y := ef_v8b_main.o ef_v8b_vec.o
CFLAGS_ef_v8b_vec.o += -ffreestanding
CFLAGS_REMOVE_ef_v8b_vec.o += -mgeneral-regs-only
KDIR ?= /lib/modules/$(shell uname -r)/build
all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) W=1 modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean

# 野火 F103VET6 / M1 测试固件
PREFIX  = arm-none-eabi-
CC      = $(PREFIX)gcc
OBJCOPY = $(PREFIX)objcopy
SIZE    = $(PREFIX)size

CPUFLAGS = -mcpu=cortex-m3 -mthumb
CFLAGS   = $(CPUFLAGS) -O2 -Wall -Wextra -nostdlib -ffreestanding
LDFLAGS  = $(CPUFLAGS) -T linker.ld -nostdlib -Wl,--gc-sections

TARGET = m1_test
SRCS   = startup.c main.c
OBJS   = $(SRCS:.c=.o)

all: $(TARGET).bin
	@echo ""
	@$(SIZE) $(TARGET).elf

$(TARGET).elf: $(OBJS) linker.ld
	$(CC) $(LDFLAGS) -o $@ $(OBJS)

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

$(TARGET).bin: $(TARGET).elf
	$(OBJCOPY) -O binary $< $@

clean:
	rm -f *.o *.elf *.bin

.PHONY: all clean

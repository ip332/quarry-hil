#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

QUARRY_ROOT=/home/igor/work/quarry

# Same architecture/ABI flags as the VisionCB M4 build -- hard-float,
# fully freestanding, no libc.
CFLAGS="-mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
        -ffreestanding -fno-builtin -nostdlib -nostartfiles \
        -Wall -Wextra -Os -g -std=c11 \
        -I. -Iquarry_generated -I${QUARRY_ROOT}/include -I${QUARRY_ROOT}/build/debug/generated"

arm-none-eabi-gcc $CFLAGS -c startup.c -o startup.o
arm-none-eabi-gcc $CFLAGS -c libc_shim.c -o libc_shim.o
arm-none-eabi-gcc $CFLAGS -c main.c -o main.o
arm-none-eabi-gcc $CFLAGS -c quarry_generated/hil/telemetry.generated.c -o telemetry.generated.o
arm-none-eabi-gcc $CFLAGS -T link.ld -Wl,--gc-sections -Wl,-Map=nucleo_bench.map \
    -nostdlib -nostartfiles -o nucleo_bench.elf \
    startup.o libc_shim.o main.o telemetry.generated.o
arm-none-eabi-objcopy -O binary nucleo_bench.elf nucleo_bench.bin

echo "=== size ==="
arm-none-eabi-size nucleo_bench.elf
echo "=== .bin size ==="
ls -la nucleo_bench.bin

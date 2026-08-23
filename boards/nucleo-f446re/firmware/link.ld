/*
 * Linker script for the NUCLEO-F446RE Quarry benchmark harness.
 *
 * Real STM32F446RE silicon memory map (RM0390 / DS10693), unlike the
 * VisionCB-8M-STD M4 firmware's TCM-only map: this firmware boots
 * directly from flash on power-up (vector table at 0x08000000, matching
 * the chip's default BOOT0=0 mapping) and runs entirely out of the
 * chip's own SRAM -- no shared/DTCM region, no second CPU to hand off
 * to; the benchmark result is reported off-device over UART instead.
 *
 *   FLASH: 0x08000000 - 0x0807FFFF (512 KiB)
 *   SRAM:  0x20000000 - 0x2001FFFF (128 KiB)
 */

MEMORY
{
  m_text (RX) : ORIGIN = 0x08000000, LENGTH = 0x80000
  m_data (RW) : ORIGIN = 0x20000000, LENGTH = 0x20000
}

ENTRY(Reset_Handler)

SECTIONS
{
    .interrupts :
    {
        . = ALIGN(4);
        KEEP(*(.isr_vector))
        . = ALIGN(4);
    } > m_text

    .text :
    {
        . = ALIGN(4);
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    } > m_text

    .data :
    {
        . = ALIGN(4);
        __data_start = .;
        *(.data*)
        __data_end = .;
    } > m_data AT> m_text
    __data_load_start = LOADADDR(.data);

    .bss (NOLOAD) :
    {
        . = ALIGN(4);
        __bss_start = .;
        *(.bss*)
        *(COMMON)
        __bss_end = .;
    } > m_data

    .stack (NOLOAD) :
    {
        /* Same 4 KiB budget proven sufficient on the VisionCB M4 firmware
         * running the identical workload/call depth. */
        . = ALIGN(8);
        __StackLimit = .;
        . += 0x1000;
        __StackTop = .;
    } > m_data

    /DISCARD/ :
    {
        *(.ARM.exidx*)
        *(.ARM.extab*)
        *(.comment)
        *(.note*)
    }
}

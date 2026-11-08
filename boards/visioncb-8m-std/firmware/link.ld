/*
 * Linker script for the VisionSOM-8Mmini Cortex-M4 Quarry benchmark
 * harness (Phase 7). Derived from the proven Phase 5/6 link.ld
 * (/home/igor/work/visioncb-m4/quarry_hil/link.ld) -- same TCM memory
 * map, same DTCM-base shared-struct address (M4-local 0x20000000 = A53
 * phys 0x00800000, unchanged from every earlier phase), same ITCM/DTCM
 * split derived from the kernel's own remoteproc translation table.
 *
 * One deliberate change from Phase 6: `.shared_mem` is NOLOAD here
 * (zero-initialized by startup.c, like .bss) instead of copied from an
 * ITCM load image. The Phase 7 result struct is ~8 KiB (raw per-iteration
 * cycle-sample arrays, not just aggregates) and is overwhelmingly
 * zero-valued at reset; copying that from ITCM would waste ~8 KiB of
 * ITCM purely to hold zero bytes and cost real copy time at startup for
 * no benefit. Meaningful scalar fields (magic/version/etc.) are set at
 * runtime in main.c instead of via a static initializer.
 *
 *   TCML/U (M4-local 0x1FFE0000-0x2001FFFF, 256 KiB) <-> A53 phys 0x007E0000
 *     ITCM: M4-local 0x1FFE0000-0x1FFFFFFF (128 KiB) -> A53 phys 0x007E0000
 *     DTCM: M4-local 0x20000000-0x2001FFFF (128 KiB) -> A53 phys 0x00800000
 */

MEMORY
{
  m_text       (RX)  : ORIGIN = 0x1FFE0000, LENGTH = 0x20000
  m_shared     (RW)  : ORIGIN = 0x20000000, LENGTH = 0x2000
  m_data       (RW)  : ORIGIN = 0x20002000, LENGTH = 0x1E000
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

    .shared_mem (NOLOAD) :
    {
        . = ALIGN(4);
        __shared_start = .;
        KEEP(*(.shared_mem))
        __shared_end = .;
    } > m_shared

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
        /* Same 4 KiB budget proven sufficient in Phase 6 (decode's own
         * frame there was 1776 bytes; this benchmark's iteration loop
         * doesn't add call depth, just repetition). */
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

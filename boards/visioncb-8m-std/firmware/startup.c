#include <stdint.h>

extern uint32_t __StackTop;
extern uint32_t __StackLimit;
extern uint32_t __data_load_start;
extern uint32_t __data_start;
extern uint32_t __data_end;
extern uint32_t __shared_start;
extern uint32_t __shared_end;
extern uint32_t __bss_start;
extern uint32_t __bss_end;

void Reset_Handler(void);
void Default_Handler(void);
extern int main(void);

/* Stack high-water-mark painting: fills the stack region with a known
 * pattern before any call that could use it. Must run first, before the
 * prologue of any other function pushes anything -- at Reset_Handler's
 * very entry, SP == __StackTop exactly (word 0 of the vector table is
 * &__StackTop, loaded directly into SP by hardware on reset, before any
 * software runs). A 64-byte guard is left unpainted immediately below
 * __StackTop as a safety margin for Reset_Handler's own minimal prologue
 * (a handful of bytes at most) -- this makes the later watermark reading
 * a few bytes conservative (undercounts true peak usage by at most the
 * guard size), never wrong in the unsafe direction. */
#define QUARRY_BENCH_STACK_PAINT_PATTERN 0xEEEEEEEEu
#define QUARRY_BENCH_STACK_GUARD_WORDS   16u /* 64 bytes */

static void paint_stack(void)
{
    /* Integer (not pointer) arithmetic for the negative offset from
     * &__StackTop -- GCC's -Warray-bounds treats a linker-script symbol
     * like __StackTop as a single-element array and (harmlessly, but
     * noisily) flags any pointer arithmetic that steps below it, even
     * though it's not a real array and this is the standard way to
     * express "N bytes below the top of this memory region" in linker-
     * symbol-based startup code. Routing through uintptr_t sidesteps the
     * pointer-bounds heuristic entirely without disabling the warning. */
    uint32_t *dst = &__StackLimit;
    const uintptr_t stop_addr =
        (uintptr_t)&__StackTop - (QUARRY_BENCH_STACK_GUARD_WORDS * sizeof(uint32_t));
    while ((uintptr_t)dst < stop_addr) {
        *dst++ = QUARRY_BENCH_STACK_PAINT_PATTERN;
    }
}

static void enable_fpu(void)
{
    volatile uint32_t *const cpacr = (volatile uint32_t *)0xE000ED88u;
    *cpacr |= (0xFu << 20);
    __asm__ volatile("dsb" ::: "memory");
    __asm__ volatile("isb" ::: "memory");
}

static void copy_and_zero(void)
{
    uint32_t *src, *dst;

    src = &__data_load_start;
    for (dst = &__data_start; dst < &__data_end;)
        *dst++ = *src++;

    /* .shared_mem is NOLOAD here (see link.ld) -- zeroed, not copied from
     * an ITCM load image. Meaningful scalar fields are set at runtime in
     * main.c after this. */
    for (dst = &__shared_start; dst < &__shared_end;)
        *dst++ = 0;

    for (dst = &__bss_start; dst < &__bss_end;)
        *dst++ = 0;
}

void Reset_Handler(void)
{
    paint_stack();
    enable_fpu();
    copy_and_zero();
    main();
    for (;;)
        ;
}

void Default_Handler(void)
{
    for (;;)
        ;
}

void NMI_Handler(void) __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void MemManage_Handler(void) __attribute__((weak, alias("Default_Handler")));
void BusFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void UsageFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void) __attribute__((weak, alias("Default_Handler")));
void DebugMon_Handler(void) __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void) __attribute__((weak, alias("Default_Handler")));

typedef void (*isr_t)(void);

__attribute__((section(".isr_vector"), used))
const isr_t g_vector_table[144] = {
    (isr_t)&__StackTop,
    Reset_Handler,
    NMI_Handler,
    HardFault_Handler,
    MemManage_Handler,
    BusFault_Handler,
    UsageFault_Handler,
    0, 0, 0, 0,
    SVC_Handler,
    DebugMon_Handler,
    0,
    PendSV_Handler,
    SysTick_Handler,
    [16 ... 143] = Default_Handler,
};

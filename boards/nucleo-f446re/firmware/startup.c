#include <stdint.h>

extern uint32_t __StackTop;
extern uint32_t __StackLimit;
extern uint32_t __data_load_start;
extern uint32_t __data_start;
extern uint32_t __data_end;
extern uint32_t __bss_start;
extern uint32_t __bss_end;

void Reset_Handler(void);
void Default_Handler(void);
extern int main(void);

/* Stack high-water-mark painting -- identical technique and rationale to
 * the VisionCB-8M-STD M4 firmware's startup.c: at Reset_Handler's very
 * entry SP == __StackTop exactly (word 0 of the vector table is
 * &__StackTop, loaded directly into SP by hardware on reset), and a
 * 64-byte guard is left unpainted as a safety margin for this function's
 * own minimal prologue. */
#define QUARRY_BENCH_STACK_PAINT_PATTERN 0xEEEEEEEEu
#define QUARRY_BENCH_STACK_GUARD_WORDS   16u /* 64 bytes */

static void paint_stack(void)
{
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

/* 16 core exceptions + 97 STM32F446xx peripheral IRQs (WWDG .. FMPI2C1_ER,
 * per RM0390's vector table). None of these peripheral IRQs are ever
 * enabled in NVIC by this firmware (polling-only UART/DWT use), so
 * Default_Handler for all of them is correct, not just a placeholder --
 * matching the polling-only design already proven on the VisionCB M4
 * firmware, which uses the same "vector table present, nothing unmasked"
 * pattern. */
__attribute__((section(".isr_vector"), used))
const isr_t g_vector_table[16 + 97] = {
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
    [16 ... (16 + 97 - 1)] = Default_Handler,
};

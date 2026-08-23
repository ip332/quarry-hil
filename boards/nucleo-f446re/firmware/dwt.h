#ifndef QUARRY_BENCH_DWT_H
#define QUARRY_BENCH_DWT_H

/* Direct Cortex-M4 DWT (Data Watchpoint and Trace) cycle-counter register
 * definitions. No CMSIS dependency -- three registers, verified against
 * ARM's own CMSIS_5 core_cm4.h (ARM-software/CMSIS_5, CMSIS/Core/Include/
 * core_cm4.h) rather than guessed, and cross-checked against the ARMv7-M
 * DWT programmer's model (same addresses independently confirmed via the
 * Cortex-M7 TRM, which shares the same DWT architecture):
 *
 *   DEMCR    (Debug Exception and Monitor Control Register)
 *            CoreDebug_BASE (0xE000EDF0) + 0x00C offset = 0xE000EDFC
 *            bit 24 = TRCENA: must be set to enable the trace/debug
 *            subsystem (DWT, ITM) before any DWT register has effect.
 *   DWT_CTRL  0xE0001000 (DWT_BASE + 0x000)
 *            bit 0  = CYCCNTENA: enables the free-running cycle counter.
 *            bit 25 = NOCYCCNT (read-only): 1 if this core does NOT
 *            implement the cycle counter -- checked, not assumed.
 *   DWT_CYCCNT 0xE0001004 (DWT_BASE + 0x004), 32-bit free-running counter,
 *            increments once per core clock cycle while CYCCNTENA is set.
 *
 * TRCENA/CYCCNTENA are core-internal, software-controlled bits -- enabling
 * them does not require an external debug probe to be attached (verified
 * empirically on this exact board: see the Phase 7 report's overhead
 * calibration, which shows CYCCNT advancing with no SWD/JTAG connected at
 * any point in this phase).
 */

#include <stdint.h>

#define QUARRY_BENCH_DEMCR_ADDR      0xE000EDFCu
#define QUARRY_BENCH_DEMCR_TRCENA    (1u << 24)

#define QUARRY_BENCH_DWT_CTRL_ADDR      0xE0001000u
#define QUARRY_BENCH_DWT_CYCCNT_ADDR    0xE0001004u
#define QUARRY_BENCH_DWT_CTRL_CYCCNTENA (1u << 0)
#define QUARRY_BENCH_DWT_CTRL_NOCYCCNT  (1u << 25)

static inline volatile uint32_t *quarry_bench_reg(uint32_t addr) {
    return (volatile uint32_t *)addr;
}

/* Returns 1 if the DWT cycle counter is implemented and was successfully
 * enabled, 0 if this core reports NOCYCCNT (no cycle counter available). */
static inline int quarry_bench_dwt_init(void) {
    volatile uint32_t *const demcr = quarry_bench_reg(QUARRY_BENCH_DEMCR_ADDR);
    volatile uint32_t *const dwt_ctrl = quarry_bench_reg(QUARRY_BENCH_DWT_CTRL_ADDR);
    volatile uint32_t *const dwt_cyccnt = quarry_bench_reg(QUARRY_BENCH_DWT_CYCCNT_ADDR);

    *demcr |= QUARRY_BENCH_DEMCR_TRCENA;
    __asm__ volatile("dsb" ::: "memory");
    __asm__ volatile("isb" ::: "memory");

    if ((*dwt_ctrl & QUARRY_BENCH_DWT_CTRL_NOCYCCNT) != 0u) {
        return 0; /* Cycle counter not implemented on this core. */
    }

    *dwt_cyccnt = 0u; /* Reset before use. */
    *dwt_ctrl |= QUARRY_BENCH_DWT_CTRL_CYCCNTENA;
    __asm__ volatile("dsb" ::: "memory");
    __asm__ volatile("isb" ::: "memory");

    return 1;
}

static inline uint32_t quarry_bench_cycles(void) {
    __asm__ volatile("" ::: "memory"); /* Compiler barrier: don't reorder
                                           surrounding code across this read. */
    const uint32_t value = *quarry_bench_reg(QUARRY_BENCH_DWT_CYCCNT_ADDR);
    __asm__ volatile("" ::: "memory");
    return value;
}

#endif

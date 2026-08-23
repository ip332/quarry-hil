#ifndef QUARRY_BENCH_UART_H
#define QUARRY_BENCH_UART_H

/* Direct-register USART2 driver for STM32F446RE (NUCLEO-F446RE), no HAL/
 * CMSIS dependency -- same philosophy as dwt.h. Register offsets verified
 * against ST's RM0390 reference manual (STM32F446xx).
 *
 * USART2 on PA2 (TX) / PA3 (RX), alternate function AF7, is the NUCLEO-64
 * board's default ST-LINK Virtual COM Port route (VCP <-> USART2 via
 * solder bridges SB62/SB63/SB13/SB14 in their factory position) -- the
 * same UART a host picocom/miniterm session on the ST-LINK's VCP device
 * already talks to, no jumper changes required.
 *
 * Clock: deliberately left at the post-reset default (HSI, 16 MHz, no PLL,
 * no AHB/APB1 prescaling) -- this firmware does not touch RCC->CFGR at
 * all. That's a known-good, always-reachable state (skips PLL lock
 * polling entirely) at the cost of running well under the chip's 180 MHz
 * ceiling; DWT cycle counts are reported at this 16 MHz core clock, which
 * the host-side runner must know when interpreting them.
 */

#include <stdint.h>

#define QUARRY_BENCH_SYSCLK_HZ 16000000u /* HSI, untouched post-reset default */
#define QUARRY_BENCH_UART_BAUD 115200u

#define QUARRY_BENCH_RCC_BASE     0x40023800u
#define QUARRY_BENCH_RCC_AHB1ENR  (*(volatile uint32_t *)(QUARRY_BENCH_RCC_BASE + 0x30u))
#define QUARRY_BENCH_RCC_APB1ENR  (*(volatile uint32_t *)(QUARRY_BENCH_RCC_BASE + 0x40u))
#define QUARRY_BENCH_RCC_AHB1ENR_GPIOAEN  (1u << 0)
#define QUARRY_BENCH_RCC_APB1ENR_USART2EN (1u << 17)

#define QUARRY_BENCH_GPIOA_BASE  0x40020000u
#define QUARRY_BENCH_GPIOA_MODER (*(volatile uint32_t *)(QUARRY_BENCH_GPIOA_BASE + 0x00u))
#define QUARRY_BENCH_GPIOA_AFRL  (*(volatile uint32_t *)(QUARRY_BENCH_GPIOA_BASE + 0x20u))
#define QUARRY_BENCH_GPIO_MODER_AF 2u
#define QUARRY_BENCH_GPIO_AF7      7u

#define QUARRY_BENCH_USART2_BASE 0x40004400u
#define QUARRY_BENCH_USART2_SR   (*(volatile uint32_t *)(QUARRY_BENCH_USART2_BASE + 0x00u))
#define QUARRY_BENCH_USART2_DR   (*(volatile uint32_t *)(QUARRY_BENCH_USART2_BASE + 0x04u))
#define QUARRY_BENCH_USART2_BRR  (*(volatile uint32_t *)(QUARRY_BENCH_USART2_BASE + 0x08u))
#define QUARRY_BENCH_USART2_CR1  (*(volatile uint32_t *)(QUARRY_BENCH_USART2_BASE + 0x0Cu))
#define QUARRY_BENCH_USART_SR_TXE (1u << 7)
#define QUARRY_BENCH_USART_SR_TC  (1u << 6)
#define QUARRY_BENCH_USART_CR1_RE (1u << 2)
#define QUARRY_BENCH_USART_CR1_TE (1u << 3)
#define QUARRY_BENCH_USART_CR1_UE (1u << 13)

static inline void quarry_bench_uart_init(void)
{
    uint32_t usartdiv_x100, mantissa, fraction;

    QUARRY_BENCH_RCC_AHB1ENR |= QUARRY_BENCH_RCC_AHB1ENR_GPIOAEN;
    QUARRY_BENCH_RCC_APB1ENR |= QUARRY_BENCH_RCC_APB1ENR_USART2EN;
    __asm__ volatile("dsb" ::: "memory");

    /* PA2/PA3 -> alternate function mode, AF7 (USART2). */
    QUARRY_BENCH_GPIOA_MODER &= ~((3u << (2u * 2u)) | (3u << (2u * 3u)));
    QUARRY_BENCH_GPIOA_MODER |= (QUARRY_BENCH_GPIO_MODER_AF << (2u * 2u)) |
                                 (QUARRY_BENCH_GPIO_MODER_AF << (2u * 3u));
    QUARRY_BENCH_GPIOA_AFRL &= ~((0xFu << (4u * 2u)) | (0xFu << (4u * 3u)));
    QUARRY_BENCH_GPIOA_AFRL |= (QUARRY_BENCH_GPIO_AF7 << (4u * 2u)) |
                                (QUARRY_BENCH_GPIO_AF7 << (4u * 3u));

    /* USARTDIV (oversampling-by-16, OVER8=0 default), computed as an
     * integer-scaled fixed point (x100) to avoid needing float support:
     *   USARTDIV = fCK / (16 * baud)
     * At 16 MHz / 115200 baud this yields mantissa=8, fraction=11 (0x8B),
     * matching ST's own reference table for this exact clock/baud pair. */
    usartdiv_x100 = (QUARRY_BENCH_SYSCLK_HZ * 25u) / (4u * QUARRY_BENCH_UART_BAUD);
    mantissa = usartdiv_x100 / 100u;
    fraction = ((usartdiv_x100 - mantissa * 100u) * 16u + 50u) / 100u;
    if (fraction >= 16u) {
        mantissa += 1u;
        fraction -= 16u;
    }
    QUARRY_BENCH_USART2_BRR = (mantissa << 4) | fraction;

    QUARRY_BENCH_USART2_CR1 = QUARRY_BENCH_USART_CR1_UE | QUARRY_BENCH_USART_CR1_TE |
                               QUARRY_BENCH_USART_CR1_RE;
    __asm__ volatile("dsb" ::: "memory");
}

static inline void quarry_bench_uart_write_byte(uint8_t b)
{
    while ((QUARRY_BENCH_USART2_SR & QUARRY_BENCH_USART_SR_TXE) == 0u) {
    }
    QUARRY_BENCH_USART2_DR = b;
}

static inline void quarry_bench_uart_write_str(const char *s)
{
    while (*s) {
        quarry_bench_uart_write_byte((uint8_t)*s++);
    }
}

static inline void quarry_bench_uart_drain(void)
{
    while ((QUARRY_BENCH_USART2_SR & QUARRY_BENCH_USART_SR_TC) == 0u) {
    }
}

static const char quarry_bench_hex_digits[] = "0123456789abcdef";

/* Streams `n` bytes from `buf` as lower-case hex (2 chars/byte), no
 * separators -- the host-side runner reassembles with bytes.fromhex(). */
static inline void quarry_bench_uart_write_hex(const uint8_t *buf, uint32_t n)
{
    uint32_t i;
    for (i = 0; i < n; ++i) {
        quarry_bench_uart_write_byte((uint8_t)quarry_bench_hex_digits[buf[i] >> 4]);
        quarry_bench_uart_write_byte((uint8_t)quarry_bench_hex_digits[buf[i] & 0xFu]);
    }
}

#endif

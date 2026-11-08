/* Quarry Cortex-M4 hardware benchmark, NUCLEO-F446RE.
 *
 * Same workload, struct layout, and measurement design as the
 * VisionCB-8M-STD M4 firmware (boards/visioncb-8m-std/firmware/main.c): three
 * separately timed loops (encode-only, decode-only against a fixed valid
 * payload, and a combined round-trip), correctness validation always
 * performed outside the timed region. Host-API-free: no printf, no
 * assert(), no heap.
 *
 * Unlike VisionCB (a Linux-hosted M4 auxiliary core reached via a shared
 * DTCM struct read from the A53 side), this is a standalone chip with no
 * host OS of its own -- the result struct is streamed off-device as raw
 * hex over the ST-LINK VCP UART (USART2) once the benchmark completes,
 * framed with begin/end markers. The host-side quarry-hil runner decodes
 * it and computes the same summary statistics read_bench_result.c would.
 */

#include <stddef.h>

/* QUARRY_BENCH_DEVICE_ID comes from infrastructure/build/board_specs.py. */
#include "telemetry_workload.h"
#include "dwt.h"
#include "uart.h"

extern uint32_t __StackTop;
extern uint32_t __StackLimit;

#define QUARRY_BENCH_ENCODE_BUFFER_CAPACITY 256U
#define QUARRY_BENCH_ITERATIONS QUARRY_BENCH_MAX_SAMPLES
#define QUARRY_BENCH_OVERHEAD_SAMPLES 32U
#define QUARRY_BENCH_STACK_PAINT_PATTERN 0xEEEEEEEEu

/* Supplied by infrastructure/build/firmware_build.py; there are no defaults,
 * so firmware cannot be built outside that provenance-recording path. */
#ifndef QUARRY_BENCH_QUARRY_COMMIT_PREFIX
#error "QUARRY_BENCH_QUARRY_COMMIT_PREFIX must be defined by the build"
#endif
#ifndef QUARRY_BENCH_BUILD_INPUT_DIGEST
#error "QUARRY_BENCH_BUILD_INPUT_DIGEST must be defined by the build"
#endif

static volatile struct quarry_bench_result g_result;

/* Static, not stack -- same rationale as VisionCB: fixed/static buffers,
 * and the .stack region is a fixed budget. */
static uint8_t g_encode_buffer[QUARRY_BENCH_ENCODE_BUFFER_CAPACITY];
static uint8_t g_roundtrip_buffer[QUARRY_BENCH_ENCODE_BUFFER_CAPACITY];
static hil_telemetry_SensorSample_t g_sample;
static hil_telemetry_SensorSample_decode_result_t g_decoded;
static uint32_t g_overhead_samples[QUARRY_BENCH_OVERHEAD_SAMPLES];

/* Insertion sort -- small, fixed N, no library dependency. */
static void sort_u32(uint32_t *values, uint32_t count) {
    uint32_t i, j, key;
    for (i = 1U; i < count; ++i) {
        key = values[i];
        j = i;
        while (j > 0U && values[j - 1U] > key) {
            values[j] = values[j - 1U];
            --j;
        }
        values[j] = key;
    }
}

static uint32_t measure_overhead(void) {
    uint32_t i;
    for (i = 0; i < QUARRY_BENCH_OVERHEAD_SAMPLES; ++i) {
        const uint32_t start = quarry_bench_cycles();
        const uint32_t end = quarry_bench_cycles();
        g_overhead_samples[i] = end - start;
    }
    sort_u32(g_overhead_samples, QUARRY_BENCH_OVERHEAD_SAMPLES);
    return g_overhead_samples[QUARRY_BENCH_OVERHEAD_SAMPLES / 2U]; /* median */
}

static uint32_t stack_watermark_bytes(void) {
    const uint32_t *p = &__StackLimit;
    const uint32_t *const top = &__StackTop;
    while (p < top && *p == QUARRY_BENCH_STACK_PAINT_PATTERN) {
        ++p;
    }
    return (uint32_t)((const uint8_t *)top - (const uint8_t *)p);
}

static void run_benchmark(void) {
    uint32_t i;
    hil_telemetry_SensorSample_encode_result_t encode_result;
    hil_telemetry_SensorSample_encoded_size_result_t encoded_size;

    g_result.magic = QUARRY_BENCH_MAGIC;
    g_result.version = QUARRY_BENCH_VERSION;
    g_result.state = QUARRY_BENCH_STATE_TEST_STARTED;
    g_result.counter = 0U;
    g_result.validation_failures = 0U;
    g_result.stack_region_bytes = (uint32_t)((const uint8_t *)&__StackTop - (const uint8_t *)&__StackLimit);
    g_result.record_sizeof = (uint32_t)sizeof(hil_telemetry_SensorSample_t);
    g_result.presence_layout = QUARRY_BENCH_PRESENCE_LAYOUT;
    g_result.quarry_commit_prefix = QUARRY_BENCH_QUARRY_COMMIT_PREFIX;
    g_result.build_input_digest = QUARRY_BENCH_BUILD_INPUT_DIGEST;
    g_result.heap_bytes = 0U; /* No allocator linked; same confirmed-clean dependency profile as VisionCB. */

    g_result.dwt_supported = (uint32_t)quarry_bench_dwt_init();
    if (!g_result.dwt_supported) {
        g_result.result_code = QUARRY_BENCH_RESULT_FAIL_NO_DWT;
        g_result.state = QUARRY_BENCH_STATE_TEST_COMPLETE;
        return;
    }
    g_result.timing_overhead_cycles = measure_overhead();

    quarry_bench_populate_sample(&g_sample);

    /* Single-shot proof pass -- not timed. */
    encode_result = hil_telemetry_SensorSample_encode(&g_sample, g_encode_buffer,
                                                        QUARRY_BENCH_ENCODE_BUFFER_CAPACITY);
    g_result.quarry_encode_status = (uint32_t)encode_result.status;
    g_result.encoded_bytes = (uint32_t)encode_result.bytes_written;
    if (encode_result.status != QUARRY_C_STATUS_OK) {
        g_result.encode_stage = QUARRY_BENCH_STAGE_FAILED;
        g_result.result_code = QUARRY_BENCH_RESULT_FAIL_ENCODE;
        g_result.state = QUARRY_BENCH_STATE_TEST_COMPLETE;
        return;
    }
    /* _encoded_size() must agree with what encode() actually wrote. */
    encoded_size = hil_telemetry_SensorSample_encoded_size(&g_sample);
    if (encoded_size.status != QUARRY_C_STATUS_OK || encoded_size.size != encode_result.bytes_written) {
        g_result.encode_stage = QUARRY_BENCH_STAGE_FAILED;
        g_result.result_code = QUARRY_BENCH_RESULT_FAIL_ENCODE;
        g_result.state = QUARRY_BENCH_STATE_TEST_COMPLETE;
        return;
    }
    g_result.encoded_payload_fnv1a = quarry_bench_fnv1a32(g_encode_buffer, (uint32_t)encode_result.bytes_written);
    g_result.encoded_payload_captured_bytes = (encode_result.bytes_written < QUARRY_BENCH_PAYLOAD_CAPTURE_BYTES)
                                                  ? (uint32_t)encode_result.bytes_written
                                                  : QUARRY_BENCH_PAYLOAD_CAPTURE_BYTES;
    for (i = 0; i < g_result.encoded_payload_captured_bytes; ++i) {
        g_result.encoded_payload[i] = g_encode_buffer[i];
    }
    g_result.encode_stage = QUARRY_BENCH_STAGE_OK;

    g_decoded = hil_telemetry_SensorSample_decode(g_encode_buffer, encode_result.bytes_written);
    g_result.quarry_decode_status = (uint32_t)g_decoded.status;
    if (g_decoded.status != QUARRY_C_STATUS_OK || !quarry_bench_validate_decoded(&g_sample, &g_decoded.value)) {
        g_result.decode_stage = QUARRY_BENCH_STAGE_FAILED;
        g_result.result_code = QUARRY_BENCH_RESULT_FAIL_DECODE;
        g_result.state = QUARRY_BENCH_STATE_TEST_COMPLETE;
        return;
    }
    g_result.decode_stage = QUARRY_BENCH_STAGE_OK;
    g_result.validate_stage = QUARRY_BENCH_STAGE_OK;

    /* Loop 1: encode only. Timed region is exactly the encode() call. */
    g_result.encode_cycles_min = 0xFFFFFFFFU;
    g_result.encode_cycles_max = 0U;
    g_result.encode_cycles_sum = 0U;
    for (i = 0; i < QUARRY_BENCH_ITERATIONS; ++i) {
        hil_telemetry_SensorSample_encode_result_t r;
        uint32_t start, end, delta;

        start = quarry_bench_cycles();
        r = hil_telemetry_SensorSample_encode(&g_sample, g_encode_buffer,
                                               QUARRY_BENCH_ENCODE_BUFFER_CAPACITY);
        end = quarry_bench_cycles();
        delta = end - start;

        if (r.status != QUARRY_C_STATUS_OK || r.bytes_written != encode_result.bytes_written) {
            ++g_result.validation_failures;
        }

        g_result.encode_cycles_samples[i] = delta;
        if (delta < g_result.encode_cycles_min) g_result.encode_cycles_min = delta;
        if (delta > g_result.encode_cycles_max) g_result.encode_cycles_max = delta;
        g_result.encode_cycles_sum += delta;
    }

    /* Loop 2: decode only, repeatedly decoding the same known-good payload. */
    g_result.decode_cycles_min = 0xFFFFFFFFU;
    g_result.decode_cycles_max = 0U;
    g_result.decode_cycles_sum = 0U;
    for (i = 0; i < QUARRY_BENCH_ITERATIONS; ++i) {
        hil_telemetry_SensorSample_decode_result_t r;
        uint32_t start, end, delta;

        start = quarry_bench_cycles();
        r = hil_telemetry_SensorSample_decode(g_encode_buffer, encode_result.bytes_written);
        end = quarry_bench_cycles();
        delta = end - start;

        if (r.status != QUARRY_C_STATUS_OK || !quarry_bench_validate_decoded(&g_sample, &r.value)) {
            ++g_result.validation_failures;
        }

        g_result.decode_cycles_samples[i] = delta;
        if (delta < g_result.decode_cycles_min) g_result.decode_cycles_min = delta;
        if (delta > g_result.decode_cycles_max) g_result.decode_cycles_max = delta;
        g_result.decode_cycles_sum += delta;
    }

    /* Loop 3: round trip -- encode immediately followed by decode inside
     * one timed span, using a separate buffer from the other loops. */
    g_result.roundtrip_cycles_min = 0xFFFFFFFFU;
    g_result.roundtrip_cycles_max = 0U;
    g_result.roundtrip_cycles_sum = 0U;
    for (i = 0; i < QUARRY_BENCH_ITERATIONS; ++i) {
        hil_telemetry_SensorSample_encode_result_t er;
        hil_telemetry_SensorSample_decode_result_t dr;
        uint32_t start, end, delta;

        start = quarry_bench_cycles();
        er = hil_telemetry_SensorSample_encode(&g_sample, g_roundtrip_buffer,
                                                QUARRY_BENCH_ENCODE_BUFFER_CAPACITY);
        dr = hil_telemetry_SensorSample_decode(g_roundtrip_buffer, er.bytes_written);
        end = quarry_bench_cycles();
        delta = end - start;

        if (er.status != QUARRY_C_STATUS_OK || dr.status != QUARRY_C_STATUS_OK ||
            !quarry_bench_validate_decoded(&g_sample, &dr.value)) {
            ++g_result.validation_failures;
        }

        g_result.roundtrip_cycles_samples[i] = delta;
        if (delta < g_result.roundtrip_cycles_min) g_result.roundtrip_cycles_min = delta;
        if (delta > g_result.roundtrip_cycles_max) g_result.roundtrip_cycles_max = delta;
        g_result.roundtrip_cycles_sum += delta;
    }

    g_result.iteration_count = QUARRY_BENCH_ITERATIONS;
    g_result.stack_watermark_bytes = stack_watermark_bytes();

    g_result.result_code = (g_result.validation_failures == 0U) ? QUARRY_BENCH_RESULT_PASS
                                                                  : QUARRY_BENCH_RESULT_FAIL_VALIDATION;
    g_result.state = QUARRY_BENCH_STATE_TEST_COMPLETE;
}

/* Framing markers the host-side runner scans for on the UART stream --
 * same "distinct begin/end text markers around a payload" pattern already
 * used by quarry-hil's console-transfer code, just applied to firmware ->
 * host output instead of host -> target. */
static const char MARKER_BEGIN[] = "===NUCLEO_HIL_RESULT_BEGIN===\r\n";
static const char MARKER_END[] = "\r\n===NUCLEO_HIL_RESULT_END===\r\n";

/* Transmits only the prefix (magic .. encoded_payload), not the three
 * 500-entry raw per-iteration sample arrays that follow it in the struct.
 * Observed empirically (and expected on theoretical grounds -- no cache,
 * no branch predictor, no interrupts enabled): min==max==mean across all
 * 500 iterations of every loop, so the raw samples carry no information
 * the on-device aggregates don't already have. That matters here because,
 * unlike VisionCB's near-instant /dev/mem read, this is a polled UART
 * with no flow control: a real on-hardware run streaming the full ~6 KiB
 * struct (~1s at 115200 baud) was observed to occasionally truncate
 * mid-transfer. Sending only the ~400-byte prefix removes nearly all of
 * that transfer-reliability risk for no loss of the data this runner
 * actually uses. */
static void report_result(void) {
    const uint32_t prefix_bytes = (uint32_t)offsetof(struct quarry_bench_result, encode_cycles_samples);
    quarry_bench_uart_write_str(MARKER_BEGIN);
    quarry_bench_uart_write_hex((const uint8_t *)&g_result, prefix_bytes);
    quarry_bench_uart_write_str(MARKER_END);
    quarry_bench_uart_drain();
}

static void delay(volatile uint32_t loops) {
    while (loops--) {
        __asm__ volatile("nop");
    }
}

int main(void) {
    quarry_bench_uart_init();

    run_benchmark();
    report_result();

    for (;;) {
        g_result.counter++;
        delay(3000000u);
    }

    return 0;
}

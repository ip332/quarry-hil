/* Phase 7: Quarry Cortex-M4F hardware benchmark, VisionCB-8M-STD.
 *
 * Reuses the Phase-6 telemetry workload and boot/memory contract exactly
 * (DTCM shared struct at M4-local 0x20000000 / A53 phys 0x00800000, no
 * UART4, no peripherals other than the DWT cycle counter -- which is a
 * core-internal debug peripheral, not a board peripheral, and requires no
 * external debug probe). Host-API-free: no printf, no assert(), no heap.
 *
 * Three separately timed loops, matching the task's explicit measurement
 * design: encode-only, decode-only (decoding the same valid payload
 * repeatedly, matching "every measured decode must consume actual encoded
 * output"), and a combined round-trip (encode immediately followed by
 * decode inside one timed span). Correctness validation always happens
 * OUTSIDE the timed region for that iteration -- a benchmark result is
 * only trusted if validation_failures == 0 at the end.
 */

#include "hil/telemetry.generated.h"
#include "bench_result.h"
#include "dwt.h"

extern uint32_t __StackTop;
extern uint32_t __StackLimit;

#define QUARRY_BENCH_ENCODE_BUFFER_CAPACITY 256U
#define QUARRY_BENCH_ITERATIONS QUARRY_BENCH_MAX_SAMPLES
#define QUARRY_BENCH_OVERHEAD_SAMPLES 32U
#define QUARRY_BENCH_STACK_PAINT_PATTERN 0xEEEEEEEEu

__attribute__((section(".shared_mem"))) volatile struct quarry_bench_result g_result;

/* Static, not stack -- same rationale as Phase 6: fixed/static buffers,
 * and the .stack region is a fixed 4 KiB budget. */
static uint8_t g_encode_buffer[QUARRY_BENCH_ENCODE_BUFFER_CAPACITY];
static uint8_t g_roundtrip_buffer[QUARRY_BENCH_ENCODE_BUFFER_CAPACITY];
static hil_telemetry_SensorSample_t g_sample;
static hil_telemetry_SensorSample_decode_result_t g_decoded;
static uint32_t g_overhead_samples[QUARRY_BENCH_OVERHEAD_SAMPLES];

static void delay(volatile uint32_t loops) {
    while (loops--) {
        __asm__ volatile("nop");
    }
}

static void populate_sample(hil_telemetry_SensorSample_t *record) {
    static const char device_id[] = "m4-hil-01";
    static const uint8_t raw_payload[] = {0xDEU, 0xADU, 0xBEU, 0xEFU};
    static const int16_t readings[4] = {-120, 0, 340, 4095};
    uint32_t i;

    hil_telemetry_SensorSample_init(record);

    record->has_sequence = true;
    record->sequence = 1001U;
    record->has_timestamp_ms = true;
    record->timestamp_ms = 1700000000123ULL;
    record->has_temperature_c = true;
    record->temperature_c = 24.5f;
    record->has_battery_mv = true;
    record->battery_mv = 3700U;
    record->has_calibrated = true;
    record->calibrated = true;
    record->has_status = true;
    record->status = HIL_TELEMETRY_STATUS_WARNING;

    record->has_device_id = true;
    record->device_id_length = 0U;
    while (device_id[record->device_id_length] != '\0') {
        record->device_id[record->device_id_length] = device_id[record->device_id_length];
        ++record->device_id_length;
    }

    record->has_raw_payload = true;
    record->raw_payload_length = (uint32_t)sizeof(raw_payload);
    for (i = 0; i < record->raw_payload_length; ++i) {
        record->raw_payload[i] = raw_payload[i];
    }

    record->has_readings = true;
    record->readings_count = 4U;
    for (i = 0; i < record->readings_count; ++i) {
        record->readings[i] = readings[i];
    }
}

static int validate_decoded(const hil_telemetry_SensorSample_t *original,
                             const hil_telemetry_SensorSample_t *decoded) {
    uint32_t i;

    if (!decoded->has_sequence || decoded->sequence != original->sequence) return 0;
    if (!decoded->has_timestamp_ms || decoded->timestamp_ms != original->timestamp_ms) return 0;
    if (!decoded->has_temperature_c || decoded->temperature_c != original->temperature_c) return 0;
    if (!decoded->has_battery_mv || decoded->battery_mv != original->battery_mv) return 0;
    if (!decoded->has_calibrated || decoded->calibrated != original->calibrated) return 0;
    if (!decoded->has_status || decoded->status != original->status) return 0;
    if (!decoded->has_device_id || decoded->device_id_length != original->device_id_length) return 0;
    for (i = 0; i < original->device_id_length; ++i) {
        if (decoded->device_id[i] != original->device_id[i]) return 0;
    }
    if (!decoded->has_raw_payload || decoded->raw_payload_length != original->raw_payload_length) return 0;
    for (i = 0; i < original->raw_payload_length; ++i) {
        if (decoded->raw_payload[i] != original->raw_payload[i]) return 0;
    }
    if (!decoded->has_readings || decoded->readings_count != original->readings_count) return 0;
    for (i = 0; i < original->readings_count; ++i) {
        if (decoded->readings[i] != original->readings[i]) return 0;
    }
    return 1;
}

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

    g_result.magic = QUARRY_BENCH_MAGIC;
    g_result.version = QUARRY_BENCH_VERSION;
    g_result.state = QUARRY_BENCH_STATE_TEST_STARTED;
    g_result.counter = 0U;
    g_result.validation_failures = 0U;
    g_result.stack_region_bytes = (uint32_t)((const uint8_t *)&__StackTop - (const uint8_t *)&__StackLimit);
    g_result.heap_bytes = 0U; /* No allocator linked; no malloc/calloc/realloc call exists in this
                                 firmware, generated code, or Quarry::runtime_c -- confirmed by the
                                 Phase 6 `arm-none-eabi-nm -u` dependency check (memcpy/memset only),
                                 unchanged in this phase. */

    g_result.dwt_supported = (uint32_t)quarry_bench_dwt_init();
    if (!g_result.dwt_supported) {
        g_result.result_code = QUARRY_BENCH_RESULT_FAIL_NO_DWT;
        g_result.state = QUARRY_BENCH_STATE_TEST_COMPLETE;
        return;
    }
    g_result.timing_overhead_cycles = measure_overhead();

    populate_sample(&g_sample);

    /* Single-shot proof pass -- same semantics as Phase 6's quarry_hil,
     * kept for continuity with previous readers/tooling. Not timed. */
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
    g_result.encode_stage = QUARRY_BENCH_STAGE_OK;

    g_decoded = hil_telemetry_SensorSample_decode(g_encode_buffer, encode_result.bytes_written);
    g_result.quarry_decode_status = (uint32_t)g_decoded.status;
    if (g_decoded.status != QUARRY_C_STATUS_OK || !validate_decoded(&g_sample, &g_decoded.value)) {
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

    /* Loop 2: decode only, repeatedly decoding the same known-good payload
     * produced by the single-shot pass above. Timed region is exactly the
     * decode() call; validation happens after, outside the timing. */
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

        if (r.status != QUARRY_C_STATUS_OK || !validate_decoded(&g_sample, &r.value)) {
            ++g_result.validation_failures;
        }

        g_result.decode_cycles_samples[i] = delta;
        if (delta < g_result.decode_cycles_min) g_result.decode_cycles_min = delta;
        if (delta > g_result.decode_cycles_max) g_result.decode_cycles_max = delta;
        g_result.decode_cycles_sum += delta;
    }

    /* Loop 3: round trip -- encode immediately followed by decode inside
     * one timed span, using a separate buffer from the single-shot/encode
     * loops so this measurement is self-contained. */
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
            !validate_decoded(&g_sample, &dr.value)) {
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

int main(void) {
    run_benchmark();

    for (;;) {
        g_result.counter++;
        delay(3000000u);
    }

    return 0;
}

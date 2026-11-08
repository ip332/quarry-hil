#ifndef QUARRY_BENCH_RESULT_H
#define QUARRY_BENCH_RESULT_H

#include <stdint.h>

/* Phase 7 benchmark result struct. The first 11 fields keep the exact same
 * names/order/offsets as Phase 6's quarry_hil_result (which itself kept
 * Phase 5's hil_smoke_result as its own stable first four fields) -- a
 * previous reader that only knows about those fields still finds them at
 * the same place. Everything from `dwt_supported` onward is new.
 *
 * Placed at the same fixed DTCM base (M4-local 0x20000000 / A53 phys
 * 0x00800000) as every earlier phase -- see link.ld's enlarged m_shared
 * region (this struct is far bigger than Phase 6's 44 bytes because it
 * carries raw per-iteration cycle samples, not just aggregates, so the
 * A53-side reader can compute its own min/max/mean/median instead of
 * trusting on-device arithmetic it can't independently verify). */

#define QUARRY_BENCH_MAGIC   0x51424e31u /* "QBN1" */
#define QUARRY_BENCH_VERSION 1u

#define QUARRY_BENCH_STATE_INIT          0u
#define QUARRY_BENCH_STATE_TEST_STARTED  1u
#define QUARRY_BENCH_STATE_TEST_COMPLETE 2u

#define QUARRY_BENCH_STAGE_NOT_REACHED 0u
#define QUARRY_BENCH_STAGE_OK          1u
#define QUARRY_BENCH_STAGE_FAILED      2u

#define QUARRY_BENCH_RESULT_PENDING         0u
#define QUARRY_BENCH_RESULT_PASS            1u
#define QUARRY_BENCH_RESULT_FAIL_ENCODE     2u
#define QUARRY_BENCH_RESULT_FAIL_DECODE     3u
#define QUARRY_BENCH_RESULT_FAIL_VALIDATION 4u
#define QUARRY_BENCH_RESULT_FAIL_NO_DWT     5u

/* Per-array sample capacity. 500 iterations per loop (encode/decode/
 * roundtrip) was chosen as a starting point, not tuned from a separate
 * pilot run: TCM execution has no cache, no branch predictor, and this
 * firmware enables no interrupts, so iteration-to-iteration variance was
 * expected to be near-zero on theoretical grounds -- the 500-sample run
 * itself is treated as the investigation, and its observed spread (see
 * the Phase 7 report) is the actual evidence for or against that
 * assumption, rather than running a separate smaller pilot first and
 * costing an extra full hardware load/boot cycle to confirm it. */
#define QUARRY_BENCH_MAX_SAMPLES 500u

struct quarry_bench_result {
    /* Stable prefix -- see hil_smoke_result / quarry_hil_result. */
    uint32_t magic;
    uint32_t version;
    uint32_t state;
    uint32_t counter; /* heartbeat, incremented every loop iteration */

    uint32_t encode_stage;   /* QUARRY_BENCH_STAGE_*, single-shot proof pass */
    uint32_t decode_stage;
    uint32_t validate_stage;
    uint32_t quarry_encode_status; /* raw quarry_c_status_t from the single-shot pass */
    uint32_t quarry_decode_status;
    uint32_t encoded_bytes;        /* BRF payload size, single-shot pass */
    uint32_t result_code;          /* QUARRY_BENCH_RESULT_* -- final verdict */

    /* Phase 7 additions. */
    uint32_t dwt_supported;        /* 1 if DWT_CTRL.NOCYCCNT==0 (cycle counter usable) */
    uint32_t timing_overhead_cycles; /* median of an empty start/end read pair */

    uint32_t iteration_count;      /* N actually run per loop (<= QUARRY_BENCH_MAX_SAMPLES) */
    uint32_t validation_failures;  /* count across encode+decode+roundtrip loops */

    uint32_t encode_cycles_min;
    uint32_t encode_cycles_max;
    uint64_t encode_cycles_sum;

    uint32_t decode_cycles_min;
    uint32_t decode_cycles_max;
    uint64_t decode_cycles_sum;

    uint32_t roundtrip_cycles_min;
    uint32_t roundtrip_cycles_max;
    uint64_t roundtrip_cycles_sum;

    uint32_t stack_watermark_bytes; /* observed peak, via stack-painting */
    uint32_t stack_region_bytes;    /* configured budget, for context */
    uint32_t heap_bytes;            /* always 0 -- no allocator linked, no malloc call exists */

    /* Raw per-iteration samples, `iteration_count` of each valid. */
    uint32_t encode_cycles_samples[QUARRY_BENCH_MAX_SAMPLES];
    uint32_t decode_cycles_samples[QUARRY_BENCH_MAX_SAMPLES];
    uint32_t roundtrip_cycles_samples[QUARRY_BENCH_MAX_SAMPLES];
};

#endif

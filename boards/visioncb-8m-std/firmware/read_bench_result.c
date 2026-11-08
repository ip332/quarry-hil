/* On-target (A53/Linux) reader for the Phase 7 quarry_bench_result struct
 * at physical 0x00800000 (M4 DTCM base), same mmap()-based /dev/mem
 * method proven in Phase 5/6 (plain read() fails EFAULT on this
 * non-System-RAM address under CONFIG_STRICT_DEVMEM=y).
 *
 * Computes min/max/mean/median from the raw per-iteration sample arrays
 * on the A53 side (not trusting on-device aggregates alone) and emits a
 * single JSON object on stdout.
 *
 * This file is streamed to the target over the serial console and compiled
 * there on its own, so it carries its own copy of the struct instead of
 * including boards/common/bench_result.h. The host build compiles it with
 * -DQUARRY_BENCH_LAYOUT_CHECK, which pulls in that header and static-asserts
 * that every field offset and the total size still match.
 */
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <string.h>

#define QUARRY_BENCH_PHYS_ADDR 0x00800000UL
#define QUARRY_BENCH_MAP_SIZE  0x2000UL /* matches link.ld's m_shared region */
#define READER_MAX_SAMPLES 500u
#define READER_PAYLOAD_CAPTURE_BYTES 256u

struct reader_bench_result {
    uint32_t magic;
    uint32_t version;
    uint32_t state;
    uint32_t counter;

    uint32_t encode_stage;
    uint32_t decode_stage;
    uint32_t validate_stage;
    uint32_t quarry_encode_status;
    uint32_t quarry_decode_status;
    uint32_t encoded_bytes;
    uint32_t result_code;

    uint32_t dwt_supported;
    uint32_t timing_overhead_cycles;

    uint32_t iteration_count;
    uint32_t validation_failures;

    uint32_t encode_cycles_min;
    uint32_t encode_cycles_max;
    uint64_t encode_cycles_sum;

    uint32_t decode_cycles_min;
    uint32_t decode_cycles_max;
    uint64_t decode_cycles_sum;

    uint32_t roundtrip_cycles_min;
    uint32_t roundtrip_cycles_max;
    uint64_t roundtrip_cycles_sum;

    uint32_t stack_watermark_bytes;
    uint32_t stack_region_bytes;
    uint32_t heap_bytes;

    uint32_t record_sizeof;
    uint32_t presence_layout;
    uint32_t quarry_commit_prefix;
    uint32_t build_input_digest;
    uint32_t encoded_payload_fnv1a;
    uint32_t encoded_payload_captured_bytes;
    uint8_t encoded_payload[READER_PAYLOAD_CAPTURE_BYTES];

    uint32_t encode_cycles_samples[READER_MAX_SAMPLES];
    uint32_t decode_cycles_samples[READER_MAX_SAMPLES];
    uint32_t roundtrip_cycles_samples[READER_MAX_SAMPLES];
};

#ifdef QUARRY_BENCH_LAYOUT_CHECK
#include <stddef.h>
#include "bench_result.h"
#define READER_SAME(f) \
    _Static_assert(offsetof(struct reader_bench_result, f) == offsetof(struct quarry_bench_result, f) && \
                       sizeof(((struct reader_bench_result *)0)->f) == sizeof(((struct quarry_bench_result *)0)->f), \
                   "read_bench_result.c layout drifted from bench_result.h: " #f)
READER_SAME(magic); READER_SAME(version); READER_SAME(state); READER_SAME(counter);
READER_SAME(encode_stage); READER_SAME(decode_stage); READER_SAME(validate_stage);
READER_SAME(quarry_encode_status); READER_SAME(quarry_decode_status); READER_SAME(encoded_bytes);
READER_SAME(result_code); READER_SAME(dwt_supported); READER_SAME(timing_overhead_cycles);
READER_SAME(iteration_count); READER_SAME(validation_failures);
READER_SAME(encode_cycles_min); READER_SAME(encode_cycles_max); READER_SAME(encode_cycles_sum);
READER_SAME(decode_cycles_min); READER_SAME(decode_cycles_max); READER_SAME(decode_cycles_sum);
READER_SAME(roundtrip_cycles_min); READER_SAME(roundtrip_cycles_max); READER_SAME(roundtrip_cycles_sum);
READER_SAME(stack_watermark_bytes); READER_SAME(stack_region_bytes); READER_SAME(heap_bytes);
READER_SAME(record_sizeof); READER_SAME(presence_layout); READER_SAME(quarry_commit_prefix);
READER_SAME(build_input_digest); READER_SAME(encoded_payload_fnv1a);
READER_SAME(encoded_payload_captured_bytes); READER_SAME(encoded_payload);
READER_SAME(encode_cycles_samples); READER_SAME(decode_cycles_samples); READER_SAME(roundtrip_cycles_samples);
_Static_assert(sizeof(struct reader_bench_result) == sizeof(struct quarry_bench_result),
               "read_bench_result.c struct size drifted from bench_result.h");
_Static_assert(READER_MAX_SAMPLES == QUARRY_BENCH_MAX_SAMPLES, "sample capacity drifted");
#endif

static void sort_u32(uint32_t *values, uint32_t count) {
    for (uint32_t i = 1; i < count; ++i) {
        uint32_t key = values[i];
        uint32_t j = i;
        while (j > 0 && values[j - 1] > key) {
            values[j] = values[j - 1];
            --j;
        }
        values[j] = key;
    }
}

static double mean_u32(const uint32_t *values, uint32_t count) {
    if (count == 0) return 0.0;
    uint64_t sum = 0;
    for (uint32_t i = 0; i < count; ++i) sum += values[i];
    return (double)sum / (double)count;
}

static double median_u32_sorted(const uint32_t *sorted, uint32_t count) {
    if (count == 0) return 0.0;
    if (count % 2 == 1) return (double)sorted[count / 2];
    return ((double)sorted[count / 2 - 1] + (double)sorted[count / 2]) / 2.0;
}

static void print_stats(const char *name, uint32_t *samples, uint32_t count, uint32_t min,
                         uint32_t max, uint64_t sum, bool trailing_comma) {
    uint32_t sorted[READER_MAX_SAMPLES];
    memcpy(sorted, samples, count * sizeof(uint32_t));
    sort_u32(sorted, count);
    double mean = mean_u32(samples, count);
    double median = median_u32_sorted(sorted, count);
    printf("  \"%s\": {\"min\": %u, \"max\": %u, \"mean\": %.2f, \"median\": %.1f, \"sum\": %llu}%s\n",
           name, min, max, mean, median, (unsigned long long)sum, trailing_comma ? "," : "");
}

int main(void) {
    int fd = open("/dev/mem", O_RDONLY | O_SYNC);
    if (fd < 0) {
        perror("open /dev/mem");
        return 1;
    }

    void *map = mmap(NULL, QUARRY_BENCH_MAP_SIZE, PROT_READ, MAP_SHARED, fd, QUARRY_BENCH_PHYS_ADDR);
    if (map == MAP_FAILED) {
        perror("mmap");
        close(fd);
        return 1;
    }

    /* Copy out of the volatile/device mapping once before processing --
     * avoids re-reading MMIO repeatedly while sorting/computing stats. */
    static struct reader_bench_result r;
    memcpy(&r, map, sizeof(r));
    munmap(map, QUARRY_BENCH_MAP_SIZE);
    close(fd);

    uint32_t n = r.iteration_count;
    if (n > READER_MAX_SAMPLES) n = READER_MAX_SAMPLES;

    printf("{\n");
    printf("  \"platform\": \"VisionCB-8M-STD\",\n");
    printf("  \"cpu\": \"Cortex-M4F\",\n");
    printf("  \"workload\": \"telemetry\",\n");
    printf("  \"magic\": \"0x%08x\",\n", r.magic);
    printf("  \"version\": %u,\n", r.version);
    printf("  \"state\": %u,\n", r.state);
    printf("  \"result_code\": %u,\n", r.result_code);
    printf("  \"counter\": %u,\n", r.counter);
    printf("  \"dwt_supported\": %s,\n", r.dwt_supported ? "true" : "false");
    printf("  \"timing_overhead_cycles\": %u,\n", r.timing_overhead_cycles);
    printf("  \"iterations\": %u,\n", n);
    printf("  \"encoded_bytes\": %u,\n", r.encoded_bytes);
    printf("  \"validation_failures\": %u,\n", r.validation_failures);
    print_stats("encode_cycles", r.encode_cycles_samples, n, r.encode_cycles_min, r.encode_cycles_max,
                r.encode_cycles_sum, true);
    print_stats("decode_cycles", r.decode_cycles_samples, n, r.decode_cycles_min, r.decode_cycles_max,
                r.decode_cycles_sum, true);
    print_stats("roundtrip_cycles", r.roundtrip_cycles_samples, n, r.roundtrip_cycles_min,
                r.roundtrip_cycles_max, r.roundtrip_cycles_sum, true);
    printf("  \"stack_watermark_bytes\": %u,\n", r.stack_watermark_bytes);
    printf("  \"stack_region_bytes\": %u,\n", r.stack_region_bytes);
    printf("  \"heap_bytes\": %u,\n", r.heap_bytes);
    printf("  \"record_sizeof\": %u,\n", r.record_sizeof);
    printf("  \"presence_layout\": %u,\n", r.presence_layout);
    printf("  \"quarry_commit_prefix\": \"%08x\",\n", r.quarry_commit_prefix);
    printf("  \"build_input_digest\": \"%08x\",\n", r.build_input_digest);
    printf("  \"encoded_payload_fnv1a\": \"%08x\",\n", r.encoded_payload_fnv1a);
    uint32_t captured = r.encoded_payload_captured_bytes;
    if (captured > READER_PAYLOAD_CAPTURE_BYTES) captured = READER_PAYLOAD_CAPTURE_BYTES;
    printf("  \"encoded_payload_captured_bytes\": %u,\n", r.encoded_payload_captured_bytes);
    printf("  \"encoded_payload_hex\": \"");
    for (uint32_t i = 0; i < captured; ++i) printf("%02x", r.encoded_payload[i]);
    printf("\"\n");
    printf("}\n");

    return 0;
}

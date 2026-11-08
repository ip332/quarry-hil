/* On-target (A53/Linux) reader for the Phase 7 quarry_bench_result struct
 * at physical 0x00800000 (M4 DTCM base), same mmap()-based /dev/mem
 * method proven in Phase 5/6 (plain read() fails EFAULT on this
 * non-System-RAM address under CONFIG_STRICT_DEVMEM=y).
 *
 * Computes min/max/mean/median from the raw per-iteration sample arrays
 * on the A53 side (not trusting on-device aggregates alone) and emits a
 * single JSON object on stdout. Not yet run on hardware -- prepared ahead
 * of time, same workflow as Phase 6's read_result.c.
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
#define QUARRY_BENCH_MAX_SAMPLES 500u

struct quarry_bench_result {
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

    uint32_t encode_cycles_samples[QUARRY_BENCH_MAX_SAMPLES];
    uint32_t decode_cycles_samples[QUARRY_BENCH_MAX_SAMPLES];
    uint32_t roundtrip_cycles_samples[QUARRY_BENCH_MAX_SAMPLES];
};

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
    uint32_t sorted[QUARRY_BENCH_MAX_SAMPLES];
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
    static struct quarry_bench_result r;
    memcpy(&r, map, sizeof(r));
    munmap(map, QUARRY_BENCH_MAP_SIZE);
    close(fd);

    uint32_t n = r.iteration_count;
    if (n > QUARRY_BENCH_MAX_SAMPLES) n = QUARRY_BENCH_MAX_SAMPLES;

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
    printf("  \"heap_bytes\": %u\n", r.heap_bytes);
    printf("}\n");

    return 0;
}

#ifndef QUARRY_BENCH_TELEMETRY_WORKLOAD_H
#define QUARRY_BENCH_TELEMETRY_WORKLOAD_H

/* The telemetry workload shared by every board's firmware and by the
 * host-side layout-equivalence check (infrastructure/build/
 * layout_equivalence.c). It holds the populate/validate logic both board
 * harnesses used to duplicate in their own main.c, unchanged except for
 * two adaptations to the current generated C API:
 *
 *   - string/bytes/array fields no longer have has_<field> members. An
 *     empty value encodes as absent, and presence comes from
 *     <field>_length / <field>_count. The workload's values are all
 *     non-empty, so the wire content is unchanged.
 *   - scalar/enum presence goes through QB_SET_PRESENT / QB_HAS, which map
 *     to the bool members of the default layout or to the generated
 *     getters/setters of the --c-optimize-size bitmap layout.
 *
 * QUARRY_BENCH_PRESENCE_PACKED_LAYOUT is defined by the build (never by
 * hand) iff the bindings were generated with --c-optimize-size. A mismatch
 * between that macro and the bindings fails to compile, because each
 * branch references members/helpers that only exist in its own layout.
 *
 * The including file must define QUARRY_BENCH_DEVICE_ID (a string literal)
 * first. It is board-specific by design and predates this header: the
 * boards' payloads differ by the length of that string and must not be
 * compared as identical workloads. */

#include <stdbool.h>
#include <stdint.h>

#include "hil/telemetry.generated.h"
#include "bench_result.h"

#ifndef QUARRY_BENCH_DEVICE_ID
#error "define QUARRY_BENCH_DEVICE_ID before including telemetry_workload.h"
#endif

#if defined(QUARRY_BENCH_PRESENCE_PACKED_LAYOUT)
#define QUARRY_BENCH_PRESENCE_LAYOUT QUARRY_BENCH_PRESENCE_PACKED
#define QB_SET_PRESENT(rec, field) hil_telemetry_SensorSample_set_##field##_present((rec), true)
#define QB_HAS(rec, field) hil_telemetry_SensorSample_has_##field(rec)
#else
#define QUARRY_BENCH_PRESENCE_LAYOUT QUARRY_BENCH_PRESENCE_DEFAULT
#define QB_SET_PRESENT(rec, field) ((rec)->has_##field = true)
#define QB_HAS(rec, field) ((rec)->has_##field)
#endif

static inline void quarry_bench_populate_sample(hil_telemetry_SensorSample_t *record) {
    static const char device_id[] = QUARRY_BENCH_DEVICE_ID;
    static const uint8_t raw_payload[] = {0xDEU, 0xADU, 0xBEU, 0xEFU};
    static const int16_t readings[4] = {-120, 0, 340, 4095};
    uint32_t i;

    hil_telemetry_SensorSample_init(record);

    QB_SET_PRESENT(record, sequence);
    record->sequence = 1001U;
    QB_SET_PRESENT(record, timestamp_ms);
    record->timestamp_ms = 1700000000123ULL;
    QB_SET_PRESENT(record, temperature_c);
    record->temperature_c = 24.5f;
    QB_SET_PRESENT(record, battery_mv);
    record->battery_mv = 3700U;
    QB_SET_PRESENT(record, calibrated);
    record->calibrated = true;
    QB_SET_PRESENT(record, status);
    record->status = HIL_TELEMETRY_STATUS_WARNING;

    record->device_id_length = 0U;
    while (device_id[record->device_id_length] != '\0') {
        record->device_id[record->device_id_length] = device_id[record->device_id_length];
        ++record->device_id_length;
    }

    record->raw_payload_length = (uint32_t)sizeof(raw_payload);
    for (i = 0; i < record->raw_payload_length; ++i) {
        record->raw_payload[i] = raw_payload[i];
    }

    record->readings_count = 4U;
    for (i = 0; i < record->readings_count; ++i) {
        record->readings[i] = readings[i];
    }
}

static inline int quarry_bench_validate_decoded(const hil_telemetry_SensorSample_t *original,
                                                const hil_telemetry_SensorSample_t *decoded) {
    uint32_t i;

    if (!QB_HAS(decoded, sequence) || decoded->sequence != original->sequence) return 0;
    if (!QB_HAS(decoded, timestamp_ms) || decoded->timestamp_ms != original->timestamp_ms) return 0;
    if (!QB_HAS(decoded, temperature_c) || decoded->temperature_c != original->temperature_c) return 0;
    if (!QB_HAS(decoded, battery_mv) || decoded->battery_mv != original->battery_mv) return 0;
    if (!QB_HAS(decoded, calibrated) || decoded->calibrated != original->calibrated) return 0;
    if (!QB_HAS(decoded, status) || decoded->status != original->status) return 0;
    /* Variable-length presence is the non-zero length/count itself. */
    if (decoded->device_id_length == 0U || decoded->device_id_length != original->device_id_length) return 0;
    for (i = 0; i < original->device_id_length; ++i) {
        if (decoded->device_id[i] != original->device_id[i]) return 0;
    }
    if (decoded->raw_payload_length == 0U || decoded->raw_payload_length != original->raw_payload_length) return 0;
    for (i = 0; i < original->raw_payload_length; ++i) {
        if (decoded->raw_payload[i] != original->raw_payload[i]) return 0;
    }
    if (decoded->readings_count == 0U || decoded->readings_count != original->readings_count) return 0;
    for (i = 0; i < original->readings_count; ++i) {
        if (decoded->readings[i] != original->readings[i]) return 0;
    }
    return 1;
}

static inline uint32_t quarry_bench_fnv1a32(const uint8_t *data, uint32_t length) {
    uint32_t hash = 0x811C9DC5u;
    uint32_t i;
    for (i = 0; i < length; ++i) {
        hash ^= data[i];
        hash *= 0x01000193u;
    }
    return hash;
}

#endif

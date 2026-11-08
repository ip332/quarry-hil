/* Host-native codec gate for one presence layout (see firmware_build.py).
 *
 * Runs the same boards/common/telemetry_workload.h code the firmware runs:
 * populate, _encoded_size(), encode, decode, validate. Prints one JSON
 * object with the encoded bytes, which the build compares across layouts
 * and against what the board later reports. Exits non-zero on any codec
 * error. The build supplies QUARRY_BENCH_DEVICE_ID and, for the packed
 * layout, QUARRY_BENCH_PRESENCE_PACKED_LAYOUT. */

#include <stdio.h>

#include "telemetry_workload.h"

int main(void) {
    static hil_telemetry_SensorSample_t sample;
    static hil_telemetry_SensorSample_decode_result_t decoded;
    static uint8_t buffer[QUARRY_BENCH_PAYLOAD_CAPTURE_BYTES];
    hil_telemetry_SensorSample_encode_result_t encoded;
    hil_telemetry_SensorSample_encoded_size_result_t measured;
    size_t i;

    quarry_bench_populate_sample(&sample);

    measured = hil_telemetry_SensorSample_encoded_size(&sample);
    if (measured.status != QUARRY_C_STATUS_OK) {
        fprintf(stderr, "encoded_size status=%d\n", (int)measured.status);
        return 2;
    }
    encoded = hil_telemetry_SensorSample_encode(&sample, buffer, sizeof(buffer));
    if (encoded.status != QUARRY_C_STATUS_OK) {
        fprintf(stderr, "encode status=%d\n", (int)encoded.status);
        return 3;
    }
    if (encoded.bytes_written != measured.size) {
        fprintf(stderr, "encoded_size %zu != bytes_written %zu\n", measured.size, encoded.bytes_written);
        return 4;
    }
    decoded = hil_telemetry_SensorSample_decode(buffer, encoded.bytes_written);
    if (decoded.status != QUARRY_C_STATUS_OK) {
        fprintf(stderr, "decode status=%d\n", (int)decoded.status);
        return 5;
    }
    if (!quarry_bench_validate_decoded(&sample, &decoded.value)) {
        fprintf(stderr, "decoded record does not match the original\n");
        return 6;
    }

    printf("{\"presence_layout\": %u, \"record_sizeof\": %zu, \"encoded_bytes\": %zu, \"fnv1a\": \"%08x\", "
           "\"payload_hex\": \"",
           (unsigned)QUARRY_BENCH_PRESENCE_LAYOUT, sizeof(hil_telemetry_SensorSample_t), encoded.bytes_written,
           (unsigned)quarry_bench_fnv1a32(buffer, (uint32_t)encoded.bytes_written));
    for (i = 0; i < encoded.bytes_written; ++i) printf("%02x", buffer[i]);
    printf("\"}\n");
    return 0;
}

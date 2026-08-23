/* Minimal freestanding memcpy/memset. Quarry's generated C code and
 * Quarry::runtime_c reference exactly these two libc symbols and nothing
 * else (confirmed via `arm-none-eabi-nm -u` against the existing
 * cortex_m/quarry_cortex_m_smoke objects: no malloc, no I/O, no threading).
 * Providing them locally avoids pulling in newlib, keeping this firmware
 * fully freestanding like the proven Phase-5 hil_smoke build
 * (-nostdlib -nostartfiles, no --specs=). Built with -fno-builtin (see
 * build.sh) so GCC won't try to lower these into recursive calls to
 * themselves. */

#include <stddef.h>

void *memcpy(void *dest, const void *src, size_t n) {
    unsigned char *d = (unsigned char *)dest;
    const unsigned char *s = (const unsigned char *)src;
    while (n--) {
        *d++ = *s++;
    }
    return dest;
}

void *memset(void *dest, int value, size_t n) {
    unsigned char *d = (unsigned char *)dest;
    unsigned char v = (unsigned char)value;
    while (n--) {
        *d++ = v;
    }
    return dest;
}

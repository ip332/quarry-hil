/* Minimal freestanding memcpy/memmove/memset. Quarry's generated C code and
 * runtime_c/typed_record.c reference only these libc symbols (no malloc, no
 * I/O, no threading); memmove is used since Quarry e467968 to shift an
 * encoded array element when its length prefix grows. If a future Quarry
 * needs another symbol, the firmware link fails with "undefined reference"
 * naming it -- add it here rather than linking newlib. Providing them locally avoids pulling in newlib, keeping this firmware
 * fully freestanding like the proven Phase-5 hil_smoke build
 * (-nostdlib -nostartfiles, no --specs=). Built with -fno-builtin (see
 * TARGET_CFLAGS in infrastructure/build/firmware_build.py) so GCC won't
 * try to lower these into recursive calls to themselves. */

#include <stddef.h>

void *memcpy(void *dest, const void *src, size_t n) {
    unsigned char *d = (unsigned char *)dest;
    const unsigned char *s = (const unsigned char *)src;
    while (n--) {
        *d++ = *s++;
    }
    return dest;
}

void *memmove(void *dest, const void *src, size_t n) {
    unsigned char *d = (unsigned char *)dest;
    const unsigned char *s = (const unsigned char *)src;
    if (d == s || n == 0) {
        return dest;
    }
    if (d < s) {
        while (n--) {
            *d++ = *s++;
        }
    } else {
        d += n;
        s += n;
        while (n--) {
            *--d = *--s;
        }
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

### NUCLEO-F446RE @ 16 MHz -- Quarry `115e345223d3`

Cycles are mean over 500 iterations (min-max shown only if they differ), DWT CYCCNT, timing overhead (9 cycles) not subtracted.

| Layout | Encode | Decode | Round-trip | Payload B | sizeof(record) | .text | .data | .bss | typed_record.o .text | Stack watermark | Heap | Validation failures |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| default | 12596 / 787.2 us | 6827 / 426.7 us | 19415 / 1213.4 us | 88 | 88 | 6884 | 0 | 11336 | 3572 | 728 / 4096 | 0 | 0 |
| optimize-size | 12344 / 771.5 us | 6477 / 404.8 us | 18813 / 1175.8 us | 88 | 72 | 6868 | 0 | 11304 | 3572 | 680 / 4096 | 0 | 0 |

Wire output identical across layouts: **yes** (88 bytes, fnv1a a1bf5120)
optimize-size vs default: encode -252 cycles (-2.0%), decode -350 cycles (-5.1%), round-trip -602 cycles (-3.1%), sizeof -16 bytes, .text -16 bytes.

Provenance: quarry-hil `67438ba98ddd`, compiler `quarry-schema-compiler 0.1.266 (115e345)`, schema sha256 `3c4e394cf95a5e5c`, toolchain `arm-none-eabi-gcc (15:10.3-2021.07-4) 10.3.1 20210621 (release)`.
- default: firmware sha256 `3d7e9da47eb2cf5c41f562763c6aae81439f0a316c8804cc571e9ecc96e4ca25`, input digest `14ea62080c896fe1`, run `20261108T161244Z-default`
- optimize-size: firmware sha256 `730e186df59e4ebae8085f2b0750d750599a77c47a3839a2ea9bdc74df2e9522`, input digest `c29194d788a7593c`, run `20261108T161301Z-optimize-size`

### VisionCB-8M-STD @ 200 MHz -- Quarry `115e345223d3`

Cycles are mean over 500 iterations (min-max shown only if they differ), DWT CYCCNT, timing overhead (9 cycles) not subtracted.

| Layout | Encode | Decode | Round-trip | Payload B | sizeof(record) | .text | .data | .bss | typed_record.o .text | Stack watermark | Heap | Validation failures |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| default | 12447 / 62.2 us | 6761 / 33.8 us | 19200 / 96.0 us | 84 | 88 | 6728 | 0 | 11336 | 3572 | 728 / 4096 | 0 | 0 |
| optimize-size | 12195 / 61.0 us | 6411 / 32.1 us | 18598 / 93.0 us | 84 | 72 | 6704 | 0 | 11304 | 3572 | 680 / 4096 | 0 | 0 |

Wire output identical across layouts: **yes** (84 bytes, fnv1a 305fca0b)
optimize-size vs default: encode -252 cycles (-2.0%), decode -350 cycles (-5.2%), round-trip -602 cycles (-3.1%), sizeof -16 bytes, .text -24 bytes.

Provenance: quarry-hil `67438ba98ddd`, compiler `quarry-schema-compiler 0.1.266 (115e345)`, schema sha256 `3c4e394cf95a5e5c`, toolchain `arm-none-eabi-gcc (15:10.3-2021.07-4) 10.3.1 20210621 (release)`.
- default: firmware sha256 `bc6b1223b2e318f228c7fc8122d48c2d092b553b5a9aa8eb4b5ee24e41fbcd89`, input digest `dc07739a2baa625d`, run `20261108T161318Z-default`
- optimize-size: firmware sha256 `99cf278362cb78a6cf8804fa8d8655683e8f6efe55cd5fd5efc45a5428050b33`, input digest `a16dd4e795868a3e`, run `20261108T161459Z-optimize-size`

Boards are listed separately on purpose: they run different device_id strings (different payload sizes) at different clocks, so their numbers are not the same workload.

All checks passed: every run PASS, zero validation failures, clean trees, identical wire output across layouts per board.

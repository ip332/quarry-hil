"""Reproducible, provenance-recording firmware builds for quarry-hil boards.

Every build of a board firmware variant takes these steps, and fails rather
than falling back to anything it cannot vouch for:

 1. Host schema compiler: built from the selected Quarry tree into
    <cache>/host-build/<commit> and reused only if a stamp records the same
    commit and source tree and `quarry-schema-compiler --version` reports
    that commit. A dirty tree always gets a fresh build directory keyed by
    its diff.
 2. Bindings: generated from the board's schema into the variant's own build
    directory, with or without --c-optimize-size. Nothing generated is
    checked in. The generated API-version guard must match the version.h
    produced by configuring the same Quarry tree, and the presence layout
    must match what was requested.
 3. Firmware: the declared harness sources are copied into
    build/<board>/<variant>/src, so only declared files can be included, and
    compiled together with the generated bindings and Quarry's shared
    runtime_c/typed_record.c, using include/ and version.h from that same
    tree. The build directory is deleted first, so no object survives from
    an earlier build.
 4. Host layout-equivalence gate: the shared workload is compiled natively
    against both presence layouts. Their encoded bytes must be identical,
    and decode must validate in both. The bytes are recorded so the runner
    can compare them with what the board reports.
 5. Manifest: build/<board>/<variant>/build_manifest.json records every input
    hash, tool version, flag and generation option, the firmware hash and
    footprint, and an input digest that is compiled into the firmware and
    reported back by the board.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess

HIL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_BUILD_ROOT = os.path.join(HIL_ROOT, "build")
DEFAULT_CACHE_ROOT = os.environ.get("QUARRY_HIL_CACHE", os.path.expanduser("~/.cache/quarry-hil"))
PRESENCE_LAYOUTS = ("default", "optimize-size")

# Same architecture/ABI/optimization flags both harnesses always used.
TARGET_CFLAGS = [
    "-mcpu=cortex-m4", "-mthumb", "-mfpu=fpv4-sp-d16", "-mfloat-abi=hard",
    "-ffreestanding", "-fno-builtin", "-nostdlib", "-nostartfiles",
    "-Wall", "-Wextra", "-Os", "-g", "-std=c11",
]
TARGET_LDFLAGS = ["-Wl,--gc-sections", "-nostdlib", "-nostartfiles"]
HOST_CHECK_CFLAGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O1"]
FORBIDDEN_HEAP_SYMBOLS = ("malloc", "calloc", "realloc", "free", "_sbrk", "sbrk")


class BuildError(Exception):
    """Toolchain/infrastructure problem: the firmware could not be built."""


class CodecCheckError(Exception):
    """The host gate found Quarry codec behavior that is wrong (for example,
    layouts disagreeing on wire bytes). This is not an infrastructure fault."""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd, log=None, cwd=None, timeout=600, env=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    return result.returncode, result.stdout, result.stderr


def _run_ok(cmd, what, build_log=None, cwd=None, timeout=600):
    rc, out, err = _run(cmd, cwd=cwd, timeout=timeout)
    if build_log is not None:
        build_log.write("$ %s\n%s%s" % (" ".join(cmd), out, err))
        build_log.flush()
    if rc != 0:
        raise BuildError("%s failed (exit %d): %s\n%s%s" % (what, rc, " ".join(cmd), out[-4000:], err[-4000:]))
    return out, err


def _tool_version(tool):
    rc, out, _ = _run([tool, "--version"])
    return out.splitlines()[0].strip() if rc == 0 and out else None


# ---------------------------------------------------------------------------
# 1. Host schema compiler
# ---------------------------------------------------------------------------


def ensure_host_compiler(quarry_dir, quarry_info, log, cache_root=DEFAULT_CACHE_ROOT, jobs=8):
    commit = quarry_info["commit"]
    key = commit if not quarry_info["dirty"] else "%s-dirty-%s" % (commit, quarry_info["dirty_digest"][:12])
    build_dir = os.path.join(cache_root, "host-build", key)
    stamp_path = os.path.join(build_dir, "quarry-hil-stamp.json")
    binary = os.path.join(build_dir, "tools", "quarry-schema-compiler")
    version_header = os.path.join(build_dir, "generated", "quarry", "runtime_c", "version.h")

    cmake_args = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DQUARRY_WARNINGS_AS_ERRORS=ON",
        "-DQUARRY_ENABLE_CLANG_TIDY=OFF",
    ]

    def stamp_valid():
        if quarry_info["dirty"] or not (os.path.exists(stamp_path) and os.path.exists(binary)
                                        and os.path.exists(version_header)):
            return False
        with open(stamp_path) as f:
            stamp = json.load(f)
        if stamp.get("commit") != commit or stamp.get("tree") != quarry_info["tree"]:
            return False
        if stamp.get("binary_sha256") != sha256_file(binary):
            return False
        return True

    reused = stamp_valid()
    if reused:
        log.log("BUILD", "reusing host quarry-schema-compiler built from %s (%s)" % (commit[:12], build_dir))
    else:
        log.log("BUILD", "building host quarry-schema-compiler from %s into %s" % (quarry_dir, build_dir))
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)
        os.makedirs(build_dir)
        with open(os.path.join(build_dir, "quarry-hil-host-build.log"), "w") as blog:
            _run_ok(["cmake", "-S", quarry_dir, "-B", build_dir] + cmake_args, "host compiler configure", blog)
            _run_ok(["cmake", "--build", build_dir, "--target", "quarry_schema_compiler", "--parallel", str(jobs)],
                    "host compiler build", blog, timeout=1800)
        if not os.path.exists(binary) or not os.path.exists(version_header):
            raise BuildError("host build finished but %s or %s is missing" % (binary, version_header))

    rc, version_out, err = _run([binary, "--version"])
    if rc != 0:
        raise BuildError("quarry-schema-compiler --version failed: %s" % err)
    version_out = version_out.strip()
    # The compiler embeds the commit it was built from; refuse a binary that
    # does not say it came from the selected commit.
    if "(%s" % commit[:7] not in version_out:
        raise BuildError("host compiler reports %r, which does not name commit %s" % (version_out, commit[:7]))

    binary_sha = sha256_file(binary)
    if not reused:
        with open(stamp_path, "w") as f:
            json.dump({"commit": commit, "tree": quarry_info["tree"], "dirty": quarry_info["dirty"],
                       "binary_sha256": binary_sha, "cmake_args": cmake_args}, f, indent=2)

    with open(version_header) as f:
        m = re.search(r"#define\s+QUARRY_C_GENERATED_CODE_API_VERSION\s+(\d+)U", f.read())
    if not m:
        raise BuildError("could not read QUARRY_C_GENERATED_CODE_API_VERSION from %s" % version_header)

    return {
        "path": binary,
        "version": version_out,
        "sha256": binary_sha,
        "build_dir": build_dir,
        "reused_cached_build": reused,
        "cmake_args": cmake_args,
        "version_header": version_header,
        "version_header_dir": os.path.join(build_dir, "generated"),
        "version_header_sha256": sha256_file(version_header),
        "c_generated_code_api_version": int(m.group(1)),
    }


# ---------------------------------------------------------------------------
# 2. Binding generation
# ---------------------------------------------------------------------------


def generation_options(layout):
    if layout not in PRESENCE_LAYOUTS:
        raise BuildError("unknown presence layout %r (expected one of %s)" % (layout, ", ".join(PRESENCE_LAYOUTS)))
    return ["--language", "c"] + (["--c-optimize-size"] if layout == "optimize-size" else [])


def generate_bindings(host_compiler, schema, out_dir, layout, log, build_log=None):
    options = generation_options(layout)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    _run_ok([host_compiler["path"]] + options + ["--output-directory", out_dir, schema], "binding generation", build_log)

    files = {}
    for root, _, names in os.walk(out_dir):
        for name in sorted(names):
            path = os.path.join(root, name)
            files[os.path.relpath(path, out_dir)] = sha256_file(path)
    headers = [p for p in files if p.endswith(".h")]
    if not headers:
        raise BuildError("schema compiler produced no C header in %s" % out_dir)

    expected_api = host_compiler["c_generated_code_api_version"]
    for rel in headers:
        with open(os.path.join(out_dir, rel)) as f:
            text = f.read()
        m = re.search(r"#if QUARRY_C_GENERATED_CODE_API_VERSION != (\d+)U", text)
        if not m or int(m.group(1)) != expected_api:
            raise BuildError("%s: generated API guard %s does not match version.h's %d"
                             % (rel, m.group(1) if m else "missing", expected_api))
        if "quarry/runtime_c/typed_record.h" not in text:
            raise BuildError("%s does not use the shared typed runtime (quarry/runtime_c/typed_record.h)" % rel)
        packed = "quarry_presence_bits" in text
        if packed != (layout == "optimize-size"):
            raise BuildError("%s: generated presence layout (packed=%s) does not match requested %r"
                             % (rel, packed, layout))
    log.log("BUILD", "generated %d file(s) with %s (API %d)" % (len(files), " ".join(options), expected_api))
    return {"options": options, "files": files}


# ---------------------------------------------------------------------------
# 3-5. Firmware build
# ---------------------------------------------------------------------------


def _quarry_runtime_inputs(quarry_dir):
    inputs = {}
    typed = os.path.join(quarry_dir, "runtime_c", "typed_record.c")
    if not os.path.exists(typed):
        raise BuildError("%s not found -- this Quarry revision predates the shared typed C runtime" % typed)
    inputs["runtime_c/typed_record.c"] = sha256_file(typed)
    hdr_dir = os.path.join(quarry_dir, "include", "quarry", "runtime_c")
    for name in sorted(os.listdir(hdr_dir)):
        if name.endswith(".h"):
            inputs["include/quarry/runtime_c/" + name] = sha256_file(os.path.join(hdr_dir, name))
    return typed, inputs


def _size_berkeley(elf_or_obj):
    rc, out, _ = _run(["arm-none-eabi-size", elf_or_obj])
    if rc != 0:
        raise BuildError("arm-none-eabi-size failed on %s" % elf_or_obj)
    parts = out.strip().splitlines()[1].split()
    return {"text": int(parts[0]), "data": int(parts[1]), "bss": int(parts[2])}


def _host_layout_check(spec, quarry_dir, host_compiler, variant_dir, log, build_log):
    """Encode the shared workload natively under both layouts and require
    identical bytes. Returns {layout: {"payload_hex", "record_sizeof"}}."""
    check_src = os.path.join(HIL_ROOT, "infrastructure", "build", "layout_equivalence.c")
    typed = os.path.join(quarry_dir, "runtime_c", "typed_record.c")
    out = {}
    for layout in PRESENCE_LAYOUTS:
        work = os.path.join(variant_dir, "host-check", layout)
        gen = os.path.join(work, "generated")
        generate_bindings(host_compiler, spec["schema"], gen, layout, log, build_log)
        exe = os.path.join(work, "layout_equivalence")
        cmd = (["cc"] + HOST_CHECK_CFLAGS
               + ['-DQUARRY_BENCH_DEVICE_ID="%s"' % spec["device_id"]]
               + (["-DQUARRY_BENCH_PRESENCE_PACKED_LAYOUT=1"] if layout == "optimize-size" else [])
               + ["-I", os.path.join(HIL_ROOT, "boards", "common"), "-I", gen,
                  "-I", os.path.join(quarry_dir, "include"), "-I", host_compiler["version_header_dir"],
                  "-o", exe, check_src, typed])
        _run_ok(cmd, "host layout-equivalence build (%s)" % layout, build_log)
        rc, stdout, stderr = _run([exe])
        build_log.write("$ %s\n%s%s" % (exe, stdout, stderr))
        if rc != 0:
            raise CodecCheckError("host codec check failed for %s layout (exit %d): %s%s" % (layout, rc, stdout, stderr))
        result = json.loads(stdout)
        out[layout] = result
    a, b = out["default"]["payload_hex"], out["optimize-size"]["payload_hex"]
    if a != b:
        raise CodecCheckError("default and optimize-size layouts encode the same %s input differently:\n"
                              "  default:       %s\n  optimize-size: %s" % (spec["board"], a, b))
    log.log("BUILD", "host gate: both layouts encode identical %d-byte payloads (native sizeof default=%d, "
                     "optimize-size=%d)" % (len(a) // 2, out["default"]["record_sizeof"],
                                             out["optimize-size"]["record_sizeof"]))
    return out


def build_variant(spec, layout, quarry_dir, quarry_info, hil_info, log, build_root=DEFAULT_BUILD_ROOT,
                  cache_root=DEFAULT_CACHE_ROOT):
    """Build one firmware variant from scratch. `spec` keys:
         board, schema_rel, device_id, sources (list of repo-relative paths),
         linker_script, output_name, defines (list of extra -D flags)."""
    spec = dict(spec)
    spec["schema"] = os.path.join(HIL_ROOT, spec["schema_rel"])
    # Wiped first, before anything can fail, so no file from an earlier
    # build (manifest, firmware, objects) can be mistaken for this one's.
    variant_dir = os.path.join(build_root, spec["board"], layout)
    if os.path.exists(variant_dir):
        shutil.rmtree(variant_dir)
    host_compiler = ensure_host_compiler(quarry_dir, quarry_info, log, cache_root=cache_root)
    src_dir = os.path.join(variant_dir, "src")
    gen_dir = os.path.join(variant_dir, "generated")
    obj_dir = os.path.join(variant_dir, "obj")
    for d in (src_dir, obj_dir):
        os.makedirs(d)
    build_log_path = os.path.join(variant_dir, "build.log")
    build_log = open(build_log_path, "w")
    try:
        # Copy declared harness sources (flat namespace; names must be unique).
        harness = {}
        for rel in spec["sources"] + [spec["linker_script"], spec["schema_rel"]]:
            src = os.path.join(HIL_ROOT, rel)
            dst = os.path.join(src_dir, os.path.basename(rel))
            if os.path.exists(dst):
                raise BuildError("duplicate harness file name %s" % os.path.basename(rel))
            shutil.copy2(src, dst)
            harness[rel] = sha256_file(src)

        generated = generate_bindings(host_compiler, spec["schema"], gen_dir, layout, log, build_log)
        typed_src, quarry_inputs = _quarry_runtime_inputs(quarry_dir)
        host_gate = _host_layout_check(spec, quarry_dir, host_compiler, variant_dir, log, build_log)

        gcc_version = _tool_version("arm-none-eabi-gcc")
        if gcc_version is None:
            raise BuildError("arm-none-eabi-gcc not found on PATH")

        inputs = {
            "board": spec["board"],
            "presence_layout": layout,
            "generation_options": generated["options"],
            "quarry_commit": quarry_info["commit"],
            "quarry_dirty_digest": quarry_info.get("dirty_digest"),
            "harness_sources": harness,
            "generated_files": generated["files"],
            "quarry_runtime_inputs": quarry_inputs,
            "version_header_sha256": host_compiler["version_header_sha256"],
            "schema_compiler_sha256": host_compiler["sha256"],
            "target_cflags": TARGET_CFLAGS,
            "target_ldflags": TARGET_LDFLAGS,
            "board_defines": spec.get("defines", []),
            "device_id": spec["device_id"],
            "compiler": gcc_version,
        }
        input_digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()

        defines = list(spec.get("defines", [])) + [
            '-DQUARRY_BENCH_DEVICE_ID="%s"' % spec["device_id"],
            "-DQUARRY_BENCH_QUARRY_COMMIT_PREFIX=0x%su" % quarry_info["commit"][:8],
            "-DQUARRY_BENCH_BUILD_INPUT_DIGEST=0x%su" % input_digest[:8],
        ]
        if layout == "optimize-size":
            defines.append("-DQUARRY_BENCH_PRESENCE_PACKED_LAYOUT=1")
        include_flags = ["-I", src_dir, "-I", gen_dir, "-I", os.path.join(quarry_dir, "include"),
                         "-I", host_compiler["version_header_dir"]]
        cflags = TARGET_CFLAGS + defines + include_flags

        units = [os.path.join(src_dir, os.path.basename(r)) for r in spec["sources"] if r.endswith(".c")]
        units += [os.path.join(gen_dir, r) for r in sorted(generated["files"]) if r.endswith(".c")]
        units.append(typed_src)
        objects = {}
        warnings = []
        for unit in units:
            name = os.path.basename(unit)[:-2]
            obj = os.path.join(obj_dir, name + ".o")
            if obj in objects.values():
                raise BuildError("object name collision for %s" % unit)
            _, err = _run_ok(["arm-none-eabi-gcc"] + cflags + ["-c", unit, "-o", obj], "compile %s" % name, build_log)
            warnings += [ln for ln in err.splitlines() if "warning:" in ln]
            objects[name] = obj

        elf = os.path.join(variant_dir, spec["output_name"] + ".elf")
        bin_path = os.path.join(variant_dir, "%s-%s.bin" % (spec["output_name"], layout))
        map_path = os.path.join(variant_dir, spec["output_name"] + ".map")
        _run_ok(["arm-none-eabi-gcc"] + TARGET_CFLAGS + ["-T", os.path.join(src_dir, os.path.basename(spec["linker_script"])),
                 "-Wl,-Map=%s" % map_path] + TARGET_LDFLAGS + ["-o", elf] + list(objects.values()),
                "link", build_log)
        _run_ok(["arm-none-eabi-objcopy", "-O", "binary", elf, bin_path], "objcopy", build_log)

        nm_out, _ = _run_ok(["arm-none-eabi-nm", elf], "nm", build_log)
        symbols = {}
        for line in nm_out.splitlines():
            parts = line.split()
            if len(parts) == 3:
                symbols[parts[2]] = parts[0]
        heap_syms = [s for s in FORBIDDEN_HEAP_SYMBOLS if s in symbols]
        if heap_syms:
            raise BuildError("firmware links heap symbols %s; heap_bytes=0 would be false" % heap_syms)
        readelf_out, _ = _run_ok(["arm-none-eabi-readelf", "-h", elf], "readelf", build_log)
        entry = None
        for line in readelf_out.splitlines():
            if "Entry point address" in line:
                entry = line.split(":")[1].strip()

        footprint = {"elf": _size_berkeley(elf),
                     "objects": {name: _size_berkeley(obj) for name, obj in sorted(objects.items())}}
        fw = {
            "bin_path": bin_path,
            "elf_path": elf,
            "bin_name": os.path.basename(bin_path),
            "sha256": sha256_file(bin_path),
            "elf_sha256": sha256_file(elf),
            "bin_bytes": os.path.getsize(bin_path),
            "text_bytes": footprint["elf"]["text"],
            "data_bytes": footprint["elf"]["data"],
            "bss_bytes": footprint["elf"]["bss"],
            "compiler": gcc_version,
            "flags": " ".join(TARGET_CFLAGS + spec.get("defines", [])),
            "stack_top": "0x" + symbols["__StackTop"] if "__StackTop" in symbols else None,
            "entry_point": entry,
        }
        manifest = {
            "manifest_version": 1,
            "board": spec["board"],
            "presence_layout": layout,
            "input_digest": input_digest,
            "inputs": inputs,
            "quarry": quarry_info,
            "hil": hil_info,
            "schema_compiler": {k: v for k, v in host_compiler.items() if k != "path"},
            "schema_compiler_path": host_compiler["path"],
            "quarry_runtime_source": typed_src,
            "full_cflags": cflags,
            "firmware": {k: v for k, v in fw.items() if not k.endswith("_path")},
            "footprint": footprint,
            "compile_warnings": warnings,
            "host_layout_gate": host_gate,
        }
        manifest_path = os.path.join(variant_dir, "build_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        log.log("BUILD", "%s/%s firmware sha256=%s text=%d data=%d bss=%d input_digest=%s warnings=%d"
                % (spec["board"], layout, fw["sha256"][:16], fw["text_bytes"], fw["data_bytes"], fw["bss_bytes"],
                   input_digest[:8], len(warnings)))
        manifest["manifest_path"] = manifest_path
        manifest["build_log"] = build_log_path
        manifest["firmware_paths"] = {"bin": bin_path, "elf": elf, "map": map_path}
        return manifest
    finally:
        build_log.close()

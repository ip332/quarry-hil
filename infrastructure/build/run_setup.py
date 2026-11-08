"""Command-line options and the build stage shared by every board runner.

Both runners call add_source_args() and then prepare_firmware(). That
selects the Quarry sources, records quarry-hil's own provenance, refuses
dirty trees unless allowed, and builds the requested presence-layout
variant from scratch (firmware_build.build_variant). The result is the
provenance block that goes into result.json.
"""

import os
import shutil

import firmware_build
import quarry_source


def add_source_args(parser):
    group = parser.add_argument_group("Quarry source selection")
    sel = group.add_mutually_exclusive_group()
    sel.add_argument(
        "--quarry-ref",
        help="branch, tag or SHA to resolve and check out (detached) in the dedicated CI checkout "
             "(default: main, used when --quarry-dir is not given)",
    )
    sel.add_argument(
        "--quarry-dir",
        help="build from this existing Quarry tree as it is; it is never fetched or switched",
    )
    group.add_argument(
        "--quarry-ci-checkout", default=quarry_source.DEFAULT_CI_CHECKOUT,
        help="dedicated CI-only Quarry clone used with --quarry-ref (default: %(default)s)",
    )
    group.add_argument(
        "--expect-quarry-sha",
        help="fail unless the selected sources are exactly this commit (used by the workflows)",
    )
    parser.add_argument(
        "--presence-layout", choices=firmware_build.PRESENCE_LAYOUTS, default="default",
        help="generated C record layout: 'default' (bool has_* members) or 'optimize-size' "
             "(--c-optimize-size presence bitmap)",
    )
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="permit a dirty Quarry or quarry-hil tree (the result is marked dirty)",
    )
    parser.add_argument(
        "--build-root", default=firmware_build.DEFAULT_BUILD_ROOT,
        help="where per-board/per-layout build directories are created (default: %(default)s)",
    )


def prepare_firmware(args, spec, log):
    """Returns (provenance_doc, manifest). Raises quarry_source.SourceError,
    firmware_build.BuildError or firmware_build.CodecCheckError."""
    requested_ref = None
    if args.quarry_dir:
        quarry_dir = os.path.abspath(args.quarry_dir)
        mode = "existing-tree"
    else:
        requested_ref = args.quarry_ref or "main"
        quarry_dir = os.path.abspath(args.quarry_ci_checkout)
        quarry_source.resolve_ref(quarry_dir, requested_ref, log)
        mode = "ci-checkout"

    quarry_info = quarry_source.capture_provenance(quarry_dir, "Quarry", log)
    quarry_info["selection"] = mode
    quarry_info["requested_ref"] = requested_ref
    quarry_source.require_clean(quarry_info, "Quarry", args.allow_dirty, log)
    if args.expect_quarry_sha and quarry_info["commit"] != args.expect_quarry_sha:
        raise quarry_source.SourceError(
            "selected Quarry commit %s is not the expected %s" % (quarry_info["commit"], args.expect_quarry_sha)
        )

    hil_info = quarry_source.capture_provenance(firmware_build.HIL_ROOT, "quarry-hil", log,
                                                count_untracked_as_dirty=False)
    quarry_source.require_clean(hil_info, "quarry-hil", args.allow_dirty, log)

    manifest = firmware_build.build_variant(
        spec, args.presence_layout, quarry_dir, quarry_info, hil_info, log, build_root=args.build_root,
    )
    provenance = {
        "quarry": quarry_info,
        "hil": hil_info,
        "build": {
            "presence_layout": args.presence_layout,
            "generation_options": manifest["inputs"]["generation_options"],
            "input_digest": manifest["input_digest"],
            "manifest_path": manifest["manifest_path"],
            "schema_compiler": manifest["schema_compiler"]["version"],
            "schema_compiler_sha256": manifest["schema_compiler"]["sha256"],
            "c_generated_code_api_version": manifest["schema_compiler"]["c_generated_code_api_version"],
            "schema_sha256": manifest["inputs"]["harness_sources"][spec["schema_rel"]],
            "generated_files": manifest["inputs"]["generated_files"],
            "quarry_runtime_inputs": manifest["inputs"]["quarry_runtime_inputs"],
            "harness_sources": manifest["inputs"]["harness_sources"],
            "full_cflags": manifest["full_cflags"],
            "footprint": manifest["footprint"],
            "compile_warnings": manifest["compile_warnings"],
            "host_layout_gate": manifest["host_layout_gate"],
        },
    }
    return provenance, manifest


def copy_build_evidence(args, spec, run_dir):
    """Copy the variant's manifest and build log next to the run's logs so
    a run directory is self-contained evidence. Best effort: called on
    failure paths too, when the files may not exist."""
    variant_dir = os.path.join(args.build_root, spec["board"], args.presence_layout)
    for name in ("build_manifest.json", "build.log"):
        src = os.path.join(variant_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(run_dir, name))

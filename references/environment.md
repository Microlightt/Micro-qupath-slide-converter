# Linux environment preparation

The skill is installed on the Agent host. The conversion runtime is
prepared on the **machine that already holds the slide**. V1 automatic setup is
Linux x86_64 with glibc >= 2.28, as an ordinary user. macOS, Windows, ARM Linux,
Alpine/musl and root/sudo are not supported by this installer. A Linux host or
compatible user-provided container is an alternative; do not imply that installing
the skill on a macOS Agent host installs Linux tools locally.

## Inspect first

```bash
python3 scripts/environment.py doctor
python3 scripts/environment.py setup --root "$HOME/.local/share/qupath-slide-converter" --with-qupath --dry-run
```

`doctor` and `--dry-run` are read-only: no downloads, package installations, server
connections or slide uploads. Tool presence is not evidence that a runtime works.
The doctor reports paths and platform limitations, not a fabricated validation PASS.

## Prepare an isolated runtime

After explaining where tools will be downloaded and that setup needs at least
8 GiB free space (separate from slide/conversion scratch space), run:

```bash
python3 scripts/environment.py setup --root "$HOME/.local/share/qupath-slide-converter" --with-qupath
```

This uses a dedicated user-owned directory, does not invoke sudo, does not change
shell startup files, and does not change system Python/Java or an existing Conda
environment. It downloads the official Miniforge installer, runs it noninteractively
in that directory, creates a separate Conda prefix from **conda-forge only**, and
installs version-pinned converter ZIP distributions. An official QuPath Linux
application is installed independently if `--with-qupath` is requested and no
launcher is supplied or found on PATH. Existing QuPath is reused and actually tested;
if it fails, the error is surfaced rather than silently replacing it. To install a
separate pinned QuPath despite an incompatible PATH installation, run setup in a
clean process PATH containing only the required system commands, or point `--qupath`
to a separately prepared official installation. Never delete another installation.

Download artifacts are verified against embedded SHA-256 before execution or
extraction. Corrupt cached downloads and incomplete environments are preserved with
an error. Partial downloads are never treated as complete and are fetched again on
retry. ZIP/TAR extraction rejects traversal, external links and special files;
QuPath's internal relative links are preserved. A direct executable probe rejects
`noexec` runtime locations. JVM/native temporary files go into the runtime's own
`tmp` directory, avoiding a system `/tmp` mounted `noexec`.

Tool versions pinned in v1:

| Component | Version | Official source |
|---|---|---|
| Miniforge | 25.3.1-0 | [conda-forge release](https://github.com/conda-forge/miniforge/releases/tag/25.3.1-0) |
| bioformats2raw | 0.12.1 | [Glencoe release](https://github.com/glencoesoftware/bioformats2raw/releases/tag/v0.12.1) |
| raw2ometiff | 0.10.1 | [Glencoe release](https://github.com/glencoesoftware/raw2ometiff/releases/tag/v0.10.1) |
| QuPath | 0.6.0 | [QuPath release](https://github.com/qupath/qupath/releases/tag/v0.6.0) |

The official GitHub release asset digests were checked when v1 was prepared. The
SHA-256 strings and official URLs are in `scripts/environment.py`; update these only
through a deliberate version change with the same tests. QuPath/third-party
licenses remain with their upstream distributions. No upstream binaries are bundled.

Conda dependencies include Python 3.12, Java 17, NumPy, tifffile, Zarr 3,
imagecodecs, JPype, Pillow and OpenSlide. Direct major-version constraints are in the
installer. Package-build versions are solved by conda-forge and the **actual build
lock** is exported as `conda-explicit.txt`. This is a recorded installation, not a
claim that an unconstrained future solve is bit-for-bit reproducible. Conda channels
supply package integrity metadata; these packages do not have individual hardcoded
hashes in the skill. Network/proxy restrictions or unavailable compatible packages
are reported as setup failures. No unofficial mirrors are added automatically.

## Explicit headless QuPath bridge

If an official native QuPath launcher crashes before application startup, explain
that failure and rerun setup with **`--qupath-bridge`**. This is explicit opt-in;
there is no silent fallback. Supply the official application's `bin/QuPath` path
if reusing an installation. The adapter first probes the native launcher. When
native startup and actual reading succeed it keeps that launcher. A failed native
version probe or a fatal signal during the actual QuPath smoke read enables the
explicitly requested bridge. An ordinary metadata/reader validation failure does
not trigger this fallback; it remains a failed validation. Arbitrary custom wrappers cannot be translated into an application root.

The bridge starts a separate prepared Python/JPype process, loads the **official
QuPath application's bundled JVM and JARs**, and invokes QuPath's own command-line
entry point. It does not emulate QuPath, replace its reader, or bypass the final
QuPath JSON PASS gate. It creates task-specific Java/native caches and preferences
inside the runtime root, with a 2 GiB heap and 4 JVM-visible CPUs. The generated
`qupath-headless` launcher is for headless scripts, not the desktop GUI. The record
includes `qupath_native_probe_exit`, an actual native read failure when applicable,
native launcher path, application root, and
`qupath_launch_method: explicit_jpype_bundled_jvm_headless`. Report that method to
the user when delivering results.

```bash
python3 scripts/environment.py setup --root "$HOME/.local/share/qupath-slide-converter" \
  --with-qupath --qupath-bridge
```

## Reuse existing tools

```bash
python3 scripts/environment.py setup --root /data/wsi-runtime-record \
  --reuse-python /data/tools/analysis-env/bin/python \
  --bioformats2raw-home /data/tools/bioformats2raw-0.12.1 \
  --raw2ometiff-home /data/tools/raw2ometiff-0.10.1 \
  --qupath /data/tools/QuPath/bin/QuPath \
  --with-qupath
```

The supplied paths are local paths on the execution machine. They are never copied
or modified. The reused Python must already contain the required Python libraries
and a working JVM must be available beside it or on PATH. OpenSlide is optional for
the OME smoke test; absence is recorded and does not constitute SVS validation.
Use a fresh isolated environment if its packages need changing. The installer
never adds packages to a reused environment.

Completed managed installations can be reused. Setup takes a lock to prevent
concurrent changes and reruns the functional smoke test. Incomplete Miniforge or
Conda prefixes are **not** advertised as resumable; preserve them for diagnosis and
choose another root. An unknown existing converter directory is never overwritten.

## Verified installer boundary

V1 offline tests cover dry-run/read-only inspection, incompatible platforms,
checksums, partial/network failures, safe archives and refusal to overwrite unknown
installations. A Linux test ran the actual two-channel uint16 conversion roundtrip
with existing tools. Official QuPath download/checksum/extraction was exercised.
On that Linux host the native QuPath launcher exited with a segmentation fault
before initialization, while the explicit public bridge loaded the same official application through its
bundled JVM and passed actual QuPath validation. This is not evidence that every
host can launch QuPath normally. A fresh Miniforge installation and Conda dependency solve were also exercised on
Linux x86_64/glibc 2.31. The fresh Python/Java/OpenSlide environment passed the complete
two-channel uint16 conversion roundtrip and actual QuPath validation through the
explicit public bridge. Converter distributions were reused for that environment
test; QuPath was separately downloaded, checksum-verified and extracted from its
official release. Each installation must pass the functional checks below before being reported READY.

## Success gate

Before writing `runtime.json`, setup performs an actual 2-channel uint16 synthetic
OME-TIFF -> bioformats2raw Zarr -> raw2ometiff LZW OME-TIFF conversion. It compares
**all pixels**, shape and dtype. When QuPath is available, the bundled Groovy script
also opens this image in QuPath, checks dimensions, independent channel names/order,
full-resolution reads, every reported pyramid level, and exports a small preview.
Startup or `--version` alone is insufficient. Logs and the small synthetic test
files are retained inside the selected root. This tiny test does not validate an
arbitrary manufacturer's real slide or replace each slide's final checks.

`runtime.json` includes absolute paths to `python`, `java`, `java_home`, `jvm`,
`bioformats2raw_home`, `raw2ometiff_home`, `qupath` (nullable), the smoke result and
package versions. `managed_release_pins` describes the versions this installer
would download; it does **not** assert that arbitrary user-supplied/reused tool
paths have those versions. Reused tool paths are recorded separately, and smoke
checks prove the exercised behavior only. Absolute runtime paths are local execution records, not source
code; do not commit personal runtime.json/logs when publishing the skill.

For actual conversion subprocesses, propagate `JAVA_HOME`, the runtime library
path when needed, and `TMPDIR`/`-Djava.io.tmpdir` pointing at a writable **executable**
scratch filesystem. Set a bounded JVM heap/workers based on available resources.
Installing the environment does not launch a large conversion.

## QuPath verification interface

```bash
/path/to/QuPath script -i /data/output.ome.tiff \
  -a /data/qc/qupath.json -a /data/qc/expected.json \
  scripts/validate_qupath.groovy
```

`expected.json` accepts optional `expected_pyramid_levels` (exact reported level
count), `X/Y/C/Z/T` (or `SizeX/SizeY/...`), `rgb`, `dtype`, channel
names as strings or objects with `name`, and optional `physical_size_x/y` in µm.
Only compare a value when the source actually provided it; never invent calibration
or channel marker names. Optional `roi_checks` contains objects with
`c,x,y,w,h,sha256`; pixel hashes are row-major single-band little-endian uint8,
uint16, int16 or float32 bytes. RGB hashes use canonical R/G/B values. Without
reference ROI hashes, the QuPath script reports source-pixel comparison as
`NOT_REQUESTED`, not PASS. Full source/output pixel QC is a separate conversion gate.

The script validates actual server reads headlessly and exports a small preview.
It does not open a remote desktop window, modify the source, perform segmentation,
or claim that an analysis method is biologically validated. The output always
states `gui_opened: false`. Both a nonzero exit and missing/non-PASS JSON mean that
QuPath validation has not passed.

Official command-line documentation:
[QuPath headless scripts and arguments](https://qupath.readthedocs.io/en/0.6/docs/advanced/command_line.html).

## KFB readiness (v1.1)

KFB support here means the bundled strict reader for **KFB 1.6 JPEG tiles with
uint8 RGB pixels**. RGB's three color components are not three independent
fluorescence channels. This backend does not need a proprietary vendor SDK and
does not download or install one. Its Python dependencies (NumPy, Pillow, tifffile,
imagecodecs) are already included in the standard isolated environment setup.

Check an existing runtime before installing anything:

```bash
python3 scripts/environment.py doctor --config /data/runtime/runtime.json
python3 scripts/environment.py doctor --config /data/runtime/runtime.json \
  --kfb-test-input /data/slides/sample.kfb
```

The doctor runs the KFB check in a separate process using the **Python recorded in
runtime.json**, even if the bootstrap Python lacks image libraries. With no
configuration, it checks the current Python and reports its limitations. It never
installs packages, writes to the source, downloads data, or changes the runtime.
Missing packages or an unusable interpreter are reported with an isolated setup
command; do not add packages to a user's reused environment without changing to a
new managed environment root.

Available packages alone produce `DEPENDENCIES_AVAILABLE_UNVALIDATED`, with
`file_read_probe_passed: false`. With a test input, the probe checks the header,
index and base-level coverage, then decodes one indexed full-resolution JPEG tile.
A valid nonempty uint8 RGB decode produces `FILE_READ_PASS` and records dimensions,
tile shape, dtype, SHA-256 and coverage. `full_slide_validated` remains **false**:
this small read test does not replace whole-slide conversion, pixel validation,
or actual QuPath reading.

Missing coverage is reported separately as `coverage_policy_required: true`.
The environment probe neither fills gaps nor grants approval to do so. An explicit
user-approved policy is still required before the conversion backend writes any
background pixels. A successful read probe is possible when the source contains
valid JPEG tiles alongside uncovered canvas regions.

To prepare a new runtime and include a real KFB read test at the end:

```bash
python3 scripts/environment.py setup --root /data/new-wsi-runtime --with-qupath \
  --kfb-test-input /data/slides/sample.kfb
```

All existing setup reuse options remain supported. The KFB probe runs after the
generic conversion and requested QuPath smoke checks. `runtime.json` records the
KFB result under `capabilities.kfb`. If a requested file probe fails, setup preserves
the generic environment and its diagnostic record, returns an error, and does not
claim KFB readiness. Without a test input, setup records available dependencies
without claiming that a real KFB has been read.

The **brightfield KFB probe** explicitly refuses `KFBF` and directs the Agent to
`--kfbf-test-input`. Other KFB revisions, non-JPEG tiles and grayscale/high-bit-depth
brightfield variants are outside this RGB reader. An ordinary RGB KFB read does
not prove independent fluorescence readiness; that has a separate reader and probe.

## KFBF readiness (v1.2)

The independent-channel branch supports the strict **KFBF 2.1 grayscale JPEG
uint8** layout with strict per-channel coverage checks. It uses NumPy, Pillow, tifffile,
imagecodecs and Zarr already present in the standard runtime. No proprietary SDK,
Rust installation or automatic `kfb2zarr` execution is needed.

```bash
python3 scripts/environment.py doctor --config /data/runtime/runtime.json \
  --kfbf-test-input /data/slides/sample.kfbf
python3 scripts/environment.py setup --root /data/new-fluorescence-runtime --with-qupath \
  --kfbf-test-input /data/slides/sample.kfbf
```

The doctor runs under the configured runtime Python. Without a sample, installed
packages produce `DEPENDENCIES_AVAILABLE_UNVALIDATED`. With a sample, the probe
validates supported metadata, index geometry and per-channel coverage,
then decodes one base-level ROI **from every independent channel**, recording its
byte count and hash. `FILE_READ_PASS` proves these reads only;
`full_slide_validated` remains false. A real all-channel conversion and final
QuPath checks remain mandatory. Sample ROIs may be background, so this environment
probe is not a tissue-presence check.

Setup records the result under `capabilities.kfbf` after the generic conversion
and requested QuPath smoke checks. A failed requested KFBF probe returns an error
and preserves diagnostics; it does not claim KFBF readiness. Missing dependencies
are prepared in a new isolated runtime, never added to a reused analysis environment.
The probe never authorizes or silently fills missing pixels. Conversion defaults
to rejection; only an explicit user choice enables `--missing-pixels black`, with
per-channel coordinates/counts retained. Overlap, ambiguous geometry and failed
JPEG decoding remain fatal and cannot be reclassified as missing pixels. Unsupported
precision, RGB JPEG tiles and unknown revisions are not silently coerced. See
[KFBF conversion](kfbf.md) for the source-to-output validation contract.

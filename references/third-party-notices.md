# Third-party components

This repository contains orchestration instructions and conversion/validation code.
It does not redistribute converter binaries, QuPath, Java, Python or sample slides.
Installers fetch official distributions; each upstream license and attribution
continues to apply. The original code and documentation in this repository are
licensed under the MIT License; see [LICENSE](../LICENSE). Third-party dependencies
and sample data retain their own licenses and are not covered by this repository's
MIT License.

| Component | Purpose and upstream |
|---|---|
| bioformats2raw / Bio-Formats | Read vendor formats and generate pyramidal Zarr. [Upstream code and licenses](https://github.com/glencoesoftware/bioformats2raw) and [Bio-Formats](https://github.com/ome/bioformats). |
| raw2ometiff | Create pyramidal OME-TIFF. [Upstream code and license](https://github.com/glencoesoftware/raw2ometiff). |
| QuPath | Actual image-reader verification. [Upstream code and license](https://github.com/qupath/qupath). |
| OpenSlide | Optional independent SVS compatibility checks. [Upstream](https://openslide.org/). |
| Miniforge / conda-forge | Isolated environment and package distribution. [Miniforge](https://github.com/conda-forge/miniforge) and [conda-forge](https://conda-forge.org/). |
| NumPy, tifffile, Zarr, imagecodecs, JPype, Pillow | Pixel arrays, TIFF/Zarr encoding, Java bridge and synthetic smoke fixtures; each installed package carries its own license. |
| KFB format references | The bundled strict parser is an independent implementation informed by [OpenSlide KFB development](https://github.com/openslide/openslide/pull/669) and [kfbslide](https://github.com/yifanfeng97/kfbslide). Their source code and binaries are not vendored here; no proprietary SDK is included. |
| KFBF format reference | [kfb2zarr](https://github.com/camlloyd/kfb2zarr), MIT OR Apache-2.0, was researched for its independent-channel metadata/index interface. Its code, binary and heuristics are not bundled or executed. The strict KFBF reader is an independent implementation; format-reference attribution does not imply upstream or vendor certification. |
| Public KFBF test samples | The [KFBIO demonstration portal](https://view-asia.kfbiopathology.com/sample?tab=Immunofluorescence) provides viewable public fluorescence examples. No explicit sample redistribution license has been established. Sample files and derived data are not included in the skill package, and repository code licenses must not be applied to them. |
| Java and Python distributions | Runtime dependencies supplied by the selected official package channels, under their distribution licenses. |

Do not relabel third-party code as covered by the repository's MIT License.
If redistributing an environment or binary image, review and retain the relevant
upstream license texts, attribution and source-distribution obligations separately.
This package offers no proprietary vendor SDK or claim of vendor certification.

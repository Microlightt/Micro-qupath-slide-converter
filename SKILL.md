---
name: micro-qupath-slide-converter
license: MIT
description: "将病理切片转换为 QuPath 可读格式，识别原始格式与独立通道、检查配套文件，明场 WSI（含已支持的 KFB）输出 SVS，荧光（含已支持的 KFBF 独立通道）输出 OME-TIFF，并准备 Linux 转换及 QuPath 环境；当用户需要解决切片打不开或进行保留像素的格式转换时使用，不用于截图转切片、图像染色分析、细胞分割或常规文件传输。"
metadata:
  version: "1.2.0"
  compatibility: "Agent 需要文件读写、进程执行与官方工具下载能力。自动环境构建支持 Linux x86_64、glibc >= 2.28、Python >= 3.10 引导；转换需要隔离的 Python 3.12 和 Java 环境。其他系统可安装指令文件，但原生转换未验证。"
---

# Micro QuPath Slide Converter

把完整原始切片转成可供 QuPath 分析的文件，并给出可核验的读取结果。保持原文件；不从合成预览恢复独立通道，不把缩略图当成全分辨率切片。

## 1. 确认输入完整

定位用户提供的文件或目录，确认它在哪台机器上。所有转换在数据所在机器执行；服务器访问只能使用用户已授权的连接。运行结果、环境和日志都放在 Skill 目录之外。没有路径时只询问路径。

用已确认版本不低于 3.10 的 Python 运行：

```text
<python> <skill>/scripts/slide_probe.py --input <input> --preflight-only
```

如返回 `SELECT_INPUT`，让用户选择具体入口文件。如返回 `INCOMPLETE_SLIDE`，解释缺少什么，并按 [输入与格式](references/formats.md) 提供配套目录示例；停在此处，等待完整数据。不得尝试把仅有缩略图的入口转成分析文件。

按文件头区分 KFB 与 KFBF，不只看扩展名。JPEG KFB 1.6 明场读 [KFB 分支](references/kfb.md)；KFBF 独立荧光读 [KFBF 分支](references/kfbf.md)，支持的 2.1 灰度 JPEG uint8 布局走 OME-TIFF。未知版本、编码、位深或通道布局停止，禁止转成 RGB。

## 2. 准备执行环境

先读 [环境准备](references/environment.md)，运行 `environment.py doctor`。复用已可用环境；缺少依赖时，在用户目录建立专用环境，并包含 QuPath。说明下载内容、目的和位置后，在现有授权内继续可撤销的准备。不得使用 sudo、改变系统库或向已有分析环境安装包。需要额外权限或专有厂商 SDK 时再询问用户。重新分发工具环境或解释许可证时，读 [第三方依赖](references/third-party-notices.md)。

```text
<python> <skill>/scripts/environment.py setup --root <runtime-directory> --with-qupath
```

只有实际的小图读取、转换、回读测试通过后才能使用输出的 `runtime.json`。后续 `<runtime-python>` 使用该 JSON 的 `python` 字段。若 QuPath 安装或启动失败，按环境文档处理；不能宣称环境验收通过。非支持系统只运行检查并说明可行的 Linux 执行方案，不擅自连接其他机器。

## 3. 读取原始信息并选择分支

```text
<runtime-python> <skill>/scripts/slide_probe.py --input <input> --runtime <runtime.json> --output <inspection.json>
```

用短信息卡告知实际格式、全分辨率尺寸、独立通道数/名称/顺序、RGB 组成、位深、像素尺寸、Z/T 和多图像情况。元数据未知就标为未知，禁止根据文件名猜测抗体、荧光标记或扫描倍率。

| 输入证据 | 处理 |
|---|---|
| 已确认明场 WSI，RGB uint8 | `--image-type brightfield`，输出 SVS |
| 独立强度通道，uint8/uint16，单通道或多通道 | 输出 OME-TIFF；已知荧光可用 `--image-type fluorescence` |
| RGB 但染色类型未知 | 询问是否为明场；RGB 三色不是三个实验通道 |
| 已合成的荧光 RGB | 说明独立通道无法恢复，停止此版本的自动转换 |
| Z/T > 1、明场非 RGB uint8、不支持的位深/布局 | 停止并说明限制，不截取首层或降低位深 |

非 RGB 不自动证明荧光；模态未知时明确说明按独立强度通道保存 OME-TIFF。遇到 `SELECT_SERIES`，列出各 series 的名称和尺寸，只在用户已指定目标或证据唯一时选用；否则询问。读取与输出限制见 [输入与格式](references/formats.md)。

## 4. 预检后转换

选新的输出目录。根据内存和 CPU 设置并发、堆内存；为系统留余量。优先在 Linux 本地支持硬链接的文件系统上工作。告诉用户空间预算和会保留中间文件，不承诺未经测量的耗时。

```text
<runtime-python> <skill>/scripts/convert_slide.py --input <input> --output-dir <new-output-directory> --runtime <runtime.json> --image-type <chosen-type> --dry-run
```

读取预检结果；遇到全部抽样为常量的 `NEEDS_TISSUE_ROI`，先查找实际组织坐标并提供 `--roi-json`，禁止关闭像素校验。预检通过后移除 `--dry-run` 执行同一命令。用户已经授权转换时不重复询问。

明场 KFB 元数据包含各原生层的覆盖检查。缺失像素默认拒绝；若用户明确选择以黑色显示缺失区，增加 `--missing-pixels black` 并记录其决定。用户当前会话已经选择时不重复询问。必须交付全部缺失区域坐标和像素数量，禁止把填黑称为恢复了原始组织。

脚本完成完整分辨率读取、格式转换、像素与元数据校验，以及实际 QuPath 读取。明场 KFB 直接逐块写入 SVS 并验证原生图层；KFBF 保留全部基础层独立通道，严格检查每通道覆盖，经 TCZYX Zarr 转为 OME-TIFF，缺失像素默认停止，明确授权后才可填黑并逐通道记录；其他格式仍经过通用 Zarr 转换。SVS 细节按需读 [SVS 写入](references/svs.md)。禁止仅改扩展名、整张 WSI 一次载入内存、自动重染色或压低位深。

## 5. 失败恢复与结果验收

以输出目录内 `status.json` 为依据。失败时检查对应阶段日志，不因存在 TIFF 文件便视为成功。再次执行同一命令加 `--resume`；参数、输入清单、脚本和运行配置必须匹配。恢复的是完成阶段，不是中断文件内的任意字节。详见 [校验与恢复](references/validation.md)。

默认必须通过 QuPath 实际读取。只有用户明确接受暂缺 QuPath 验证时才使用 `--skip-qupath`；随后输出状态必须保留“QuPath 待读取验证”。不要自动启动 GUI；无桌面的服务器可完成 headless 读取，但报告中必须区分两者。

## 6. 交付

读取 `status.json`、`pixel_validation.json` 和 `qupath_validation.json`，报告最终绝对路径、格式、大小、通道名称/数量/顺序、位深、像素尺寸、QuPath 版本、具体校验级别和限制。KFB 还需报告源覆盖缺失、采用的显示填充值，以及去除的重复尺寸缩略层；即使验证通过也不能省略这些信息。KFBF 还需说明基础层像素完全保留、显示金字塔重新生成、曝光原始值仅留在附属记录且单位未知。

状态为 `QUPATH_VERIFIED_WITH_READER_LIMITATION` 时，必须说明 `reader_limitations`，分别报告文件层数与 QuPath 可读层数。不能套用“所有层均读取”的描述；已知末端 1×1 层限制见 KFB 文档。

全部通过时可说：“🎉 转换完成！✅ 已通过 QuPath 实际读取验证（无界面模式），全分辨率、通道与抽样像素检查通过。📂 文件：… 🔬 可以在 QuPath 中开始分析。”未操作 GUI 就补充“尚未在桌面窗口中打开”。成功鼓励必须与证据一致。

只完成文件转换时说：“🟡 转换与文件检查完成，QuPath 读取验证尚未完成。📂 文件：…”，并指出剩余步骤。参考 [交付状态](references/validation.md)；不得把图片预览、命令退出码或“能看到图像”单独当成完整切片验收。

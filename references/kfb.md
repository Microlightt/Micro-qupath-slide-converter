# KFB 明场

## 执行路线

当前启用：JPEG KFB 1.6 明场、RGB uint8。内置 `kfb_reader.py` 严格解析受支持的文件头和图块索引，用 Pillow 解码原始 JPEG；`kfb_backend.py` 逐块写入 BigTIFF SVS。不需要厂商闭源 SDK，也不把 KFB 交给通用 Bio-Formats 读取器。

读取器使用图块记录内的实际相对偏移，不依据压缩大小累计猜测位置。不搜索相邻 JPEG 修复索引，不把错误尺寸的图块缩放到预期大小，不开启截断图像容错。版本、压缩方式、范围、索引或图块解码不符合契约时停止。

环境依赖沿用通用隔离环境中的 Python、NumPy、Pillow、tifffile 和 imagecodecs。实际 QuPath 验证仍使用已有安装或官方隔离安装。先运行：

```text
<python> <skill>/scripts/environment.py doctor --config <runtime.json> --kfb-test-input <input.kfb>
```

依赖缺失时，在新用户目录准备环境；可以复用已有可用工具路径：

```text
<python> <skill>/scripts/environment.py setup --root <runtime-directory> --with-qupath --kfb-test-input <input.kfb>
```

检测报告区分“依赖可导入”和“真实 KFB 图块可读取”。实际探针读取一个有效的基础层 JPEG，并报告覆盖缺失；这不是全片转换验收，也不会自动填写缺失区域。需要的工具下载遵循 [环境准备](environment.md)，不修改系统或已存在的分析环境。

## 缺失像素必须有明确策略

某些 KFB 的逻辑图像范围大于已记录图块的覆盖范围，尤其在边缘。不能仅凭位置就断言缺失区域全是玻片背景，不能把缺失称为完整组织数据。

- 默认 `--missing-pixels error`：出现缺失就停止转换，并报告数量/坐标。
- 用户明确选择 `--missing-pixels black`：缺失区域显示为 RGB=(0,0,0)，有效源像素不改动；记录每个原生层的完整缺失矩形、总面积和所选策略。
- 未选择时询问；当前会话已经明确选择时沿用，不重复询问。不能仅因为别人的测试选择过黑色，就替新用户作同样决定。

坐标属于报告指定的原生层，单位为该层像素；换到基础层坐标要同时使用该层 downsample。矩形面积之和必须等于该层 missing_pixels。输出成功时仍须说明哪些区域没有源像素，不能将它们纳入“原组织像素已恢复”的描述。

示例（仅在用户已选择黑色时）：

```text
<runtime-python> <skill>/scripts/convert_slide.py --input <input.kfb> --output-dir <new-directory> --runtime <runtime.json> --image-type brightfield --missing-pixels black --dry-run
```

预检通过后移除 `--dry-run`。停止/恢复和输出保护与通用流程相同，但 KFB 直接从原生图块写 TIFF，不生成 Zarr。

## 元数据、金字塔与验证

输出保留原始基础层大小和可读取的 MPP；三个 RGB 颜色分量不命名为实验通道。JPEG 原先的有损压缩不能逆转，后续 SVS 使用无损 Deflate，不进行 JPEG 再压缩、颜色增强或位深降低。

原生金字塔按递减尺寸写入。多个最小层可能都落在 1×1 等重复尺寸；输出采用明确记录的唯一尺寸序列，省略项仍保留在原文件及源元数据清单中。生成的显示缩略图来自最小层，不作为组织验收证据。源关联标签、宏观图、厂商专有数据不会被宣称全部迁移。

逐图块核对 JPEG 中可见的 ICC 信息，发现不同图块配置不一致时停止；不因第三方 API 返回 None 就断言原文件没有 ICC。无可映射的原始字段保持未知。

写入后按块读取 SVS 与 KFB 比较各保留层，再用 QuPath 验证基础层尺寸、RGB、位深、MPP、层数和源 ROI。缺失填充必须单独说明。必须读取 `pixel_validation.json`、`kfb_write.json`、`source.json` 和 `qupath_validation.json` 后才能交付。

QuPath 0.6.0 有一个已复现的末端层限制：[官方层级构建器](https://raw.githubusercontent.com/qupath/qupath/v0.6.0/qupath-core/src/main/java/qupath/lib/images/servers/ImageServerMetadata.java)可能把 2×1/1×2 和 1×1 两层的缩放率取成同一值，从而不单独暴露最后的 1×1 层。SVS 中仍保留该层并完成全部像素比较。仅当版本为 0.6.0、输入为本 KFB 分支、缺少的仅是该末层、其余尺寸逐层完全一致，而且 QuPath 自己的构建器计算结果与实际列表完全一致时，流程接受此已知读取器限制。输出状态是 `QUPATH_VERIFIED_WITH_READER_LIMITATION`；报告同时列出文件层数、QuPath 可读层数和缺少的显示层。不得宣称所有导出层都被 QuPath 读取。任何其他层数/尺寸不一致仍失败。

格式参考：[OpenSlide 的 KFB 开发分支](https://github.com/openslide/openslide/pull/669)、[kfbslide 维护者实现](https://github.com/yifanfeng97/kfbslide)。它们是独立实现，不能代替厂商规格或所有 KFB 版本的实样测试。

## KFBF 单独分支

文件头为 `KFBF` 时，使用 [KFBF 独立通道分支](kfbf.md)。即使扩展名是 `.kfb` 或用户要求 SVS，也不能交给本明场 RGB 读取器。支持的 KFBF 2.1 灰度 JPEG uint8 布局输出 OME-TIFF；未知布局或位深停止，不尝试合成 RGB。

# KFBF 独立荧光通道

## 格式与输出契约

`KFBF` 是独立荧光通道容器，与明场 KFB 的 RGB 图像分开处理。按文件头选择读取器，即使后缀为 `.kfb` 也不能自动转成 SVS。当前严格读取范围为 **KFBF 2.1、独立灰度 JPEG、编码与解码均为 uint8、单 Z/单 T**；输出带金字塔的 OME-TIFF。

`kfbf_reader.py` 按受支持的头结构、通道元数据、索引偏移与实际 JPEG 尺寸验证数据。坐标含义必须由索引、图块尺寸和边缘覆盖一致地确认，不能仅按头字段排列顺序猜宽高，不能用转置、缩放或搜索邻近 JPEG 修复不一致。未知版本、编码、RGB JPEG、无法解释的位深、索引越界或重叠都停止；不能以 `.convert('L')` 或强制 `uint8` 来绕过校验。

通道数动态读取。一个灰度 JPEG 表示一个独立通道的强度，不是待拆分的 RGB 颜色分量。原始 JPEG 可能已经有损压缩；本转换精确保留其解码值，但不能逆转原扫描压缩。

读取器对累计通道图块数和覆盖网格设有资源上限，在分配大型索引前检查；超出时返回 `KFBF_RESOURCE_BUDGET`，不能关闭限制后继续。当前通道数上限为 32，但这不等于已用 32 通道厂商实样验证。

## 环境探针与执行

先使用已有环境的 Python 检查真实输入；工具不足时在新的用户目录准备隔离环境：

```text
<python> <skill>/scripts/environment.py doctor --config <runtime.json> --kfbf-test-input <input.kfbf>
<python> <skill>/scripts/environment.py setup --root <new-runtime-directory> --with-qupath --kfbf-test-input <input.kfbf>
```

此分支复用 NumPy、Pillow、tifffile、imagecodecs、Zarr 与 raw2ometiff，无需闭源 SDK，不自动安装或执行 kfb2zarr。环境探针检查元数据、索引与覆盖，并从每个通道读取实际基础层图块/区域。仅有模块导入不算文件读取通过；一次区域读取也不等于全片或 QuPath 验收。

```text
<runtime-python> <skill>/scripts/slide_probe.py --input <input.kfbf> --runtime <runtime.json> --output <inspection.json>
<runtime-python> <skill>/scripts/convert_slide.py --input <input.kfbf> --output-dir <new-output-directory> --runtime <runtime.json> --image-type fluorescence --dry-run
```

预检通过后移除 `--dry-run` 执行同一命令。输出使用新目录；失败按通用 `status.json` 和 `--resume` 流程恢复。不得改用明场分支规避荧光限制。

## 未记录像素与解码错误

默认 `--missing-pixels error`：任一通道有未记录区域时停止，说明受影响通道、坐标和像素数量。未知区域不能当成真实零表达。先前明场任务的填黑选择不自动授权新的荧光样本；若当前会话已明确选择本次荧光缺失策略，则无需重复询问。

只有用户明确选择后才使用 `--missing-pixels black`。此时未记录位置写入强度 0，在显示中为黑色；已有有效像素保持不变。报告必须逐通道保留全部缺失矩形、面积、占比和采用的策略，并将这些位置与真实测得的零值区分。量化时不能把填充位置视为观测到的阴性区域。

矩形坐标以基础层像素表示；`x,y,width,height` 所指区域和通道必须明确。矩形面积之和应与该通道缺失计数一致，不能只给一个全片百分比。

**仅未记录的索引位置可按授权填充。** 索引存在但 JPEG 损坏、位深不符、尺寸不符或读取失败时仍停止；不得把解码错误重分类为缺失后填黑。

## 元数据与显示金字塔

保留所有基础层通道的尺寸、次序和解码数值。名称、显示颜色和物理像素尺寸只从真实源字段读取，未知保持未知；不根据文件名推测抗体、染色标志物或单位。显示颜色不改变通道强度。曝光字段的原始数值留在元数据附属记录，单位未证实则不写成 OME 的定量曝光时间。

`kfbf_backend.py` 分块写出标准 **TCZYX** 多尺度 Zarr；T=1、Z=1，C 为源通道数。完整基础层保持原样，低分辨率显示层使用前一级的 `a[::2, ::2]` 最近邻抽取再生成。随后由 raw2ometiff 写入无损 LZW OME-TIFF。

这些显示层不是厂商原有低分辨率层。厂商原生层和专有字段仍保留在未修改的源文件及读取审计中，不能声称所有原生金字塔像素已迁移。关联标签、宏观图、注释或分析结果不保证迁入输出。

部分源文件包含没有空间索引的小型逐通道 JPEG 平面。读取器记录其偏移、尺寸、编码及摘要，并保留在原文件中；不会编造位置、把它们当成基础层或擅自插入输出金字塔。

## 必须通过的验证

1. 检查每个通道的完整基础层索引、图块解码、覆盖及显式缺失策略。
2. 按块比较原始独立通道与基础层 Zarr 的全部有效像素；单独验证授权缺失填充，并验证生成显示层符合已记录的抽取方式。
3. 按块比较 Zarr 与 OME-TIFF 的全部通道和输出层；检查 OME 中维度、位深、名称、次序、颜色与已知 MPP。
4. 由实际 QuPath 读取 OME-TIFF，检查独立通道、基础层尺寸、位深、层级和源 ROI 像素。图像能显示或进程正常退出不能替代报告中的通过状态。

交付时读取 `source.json`、`kfbf_write.json`、`kfbf_source_zarr_validation.json`、`pixel_validation.json`、`qupath_validation.json` 和缺失区域记录。报告文件绝对路径、通道元数据、采用的缺失策略、重新生成的金字塔及验证级别。无界面读取通过不等于已经打开桌面 GUI。

对话中提取通道和覆盖汇总即可；完整坐标列表保存在结果文件中，不把长列表逐项展开到对话。

## 验证状态与公开样本

已验证官方公开的四通道和六通道 KFBF 2.1 灰度 JPEG8：全部已记录基础层通道像素与授权缺失填充通过核对，所有 OME-TIFF 输出层与已验证中间数据一致；QuPath 0.6.0 的真实无界面读取通过。具体数据、哈希和检查报告保存在该版本对应的外部验证记录中。这个结论限定于实测版本和布局，文件头、环境探针或合成测试本身仍不能替代新切片的完整验收。

公开样本来自 [KFBIO 官方荧光演示门户](https://view-asia.kfbiopathology.com/sample?tab=Immunofluorescence)。[四通道原片](https://file.kfbiopathology.com/upload/kfbf/kfbf/Green%20CY%205%20Red.kfbf)与[六通道原片](https://file.kfbiopathology.com/upload/kfbf/kfbf/520%20540%20570%20620%20700%201.kfbf)可用于有来源记录的本地验证。下载时记录最终 URL、字节数和 SHA-256，并用原厂查看器核对几何与通道。公开可访问不代表获得再分发许可，样本及其派生数据不随本 Skill 分发。

格式探索参考 [kfb2zarr 0.1.3](https://github.com/camlloyd/kfb2zarr/tree/85dc23f2c5fc5ff1e5724c5a4e49d8bb9f5caece) 的逐通道接口；本分支使用独立严格读取器，没有执行或捆绑该候选工具。源码可行性不代表所有 KFBF 变体都受支持。16 位、其他编码、Z/T 堆栈和未测试的厂商变体仍需新的实样与对应读取实现。

# 输入完整性与格式边界

## 一张切片可能是一套文件

MRXS 通常需要同名入口和配套目录，结构例如：

```text
slides/
├── sample.mrxs
└── sample/
    ├── Slidedat.ini
    ├── Index.dat
    ├── Data0000.dat
    ├── Data0001.dat
    └── ...
```

只交付 `sample.mrxs` 可能只能看到缩略图。对缺失情况说明：“目前缺少同名数据目录/以下文件；请把完整切片数据放到可访问位置，并保持入口与配套目录的相对关系。例如上面的 sample.mrxs 和 sample/ 需要一起提供。”列出实际缺失项，不宣称缩略图可以恢复成原始 WSI。

预检检查 `Slidedat.ini`、`Index.dat`、至少一个 `Data*.dat` 及 INI 声明的数据文件。此检查不是完整二进制解析；后续必须真实读取主图和所有通道的多区域像素。损坏的索引、截断数据、未覆盖区域的缺块仍可能需要更深入验证。

VSI、NDPI 集合等也可能依赖其他文件；读取器给出的 used-files 清单全部登记并检查存在性。第一版没有逐个厂商的完整配套文件解析器。无法打开、仅读到概览或尺寸明显与扫描记录不符时停止，让用户提供扫描仪导出的完整数据。不得跳过报错继续用 thumbnail/overview。

## 主图与模态

目录只自动接受一个候选入口；不会递归批量转换。多个 series 时给出索引、名称、尺寸、Z/C/T/RGB，避免选择 label、macro 或缩略图。仅明确标识为 label/thumbnail/macro/overview 的关联图默认排除；多个主图需选择。保存全部 series 清单及选中编号，不默默丢弃未说明的主图。

读取器的 RGB 标志、SamplesPerPixel、有效通道数决定数据布局；C=3 本身不能决定模态。明场 RGB 与荧光三通道分别处理。非 RGB 单通道也可能是灰度明场或其他成像；已知为灰度明场时当前 SVS 写入器会拒绝，不能声称已支持所有明场布局。

读取器未暴露的通道名称记为未知；不从文件名或三种颜色推断标记。不要因通道显示很暗就归一化、阈值化、gamma 校正或转 8 位。

## 支持与拒绝

支持列表表示允许尝试相应读取路线，并非厂商认证。JPEG KFB 1.6 使用 [KFB 严格读取分支](kfb.md)，KFBF 2.1 灰度 JPEG8 使用 [独立荧光读取分支](kfbf.md)，两者按真实文件头区分，不交给通用 Bio-Formats。MRXS 使用 bioformats2raw 提供的 MiraxReader，避免通用读取器误选。其他入口使用 Bio-Formats ImageReader，读取格式字段作为实际识别证据。

严格限制 Z=T=1，uint8 或 uint16 独立通道；SVS 则要求 RGB uint8。Z/T 堆栈、signed/float、RGBA、未知布局、未支持的专有变体或加密格式保留源文件并停止。不要自动提取首层、丢 alpha 或改扩展名。用户选择其他方案时作为新任务说明信息损失后实施，不能绕过本 Skill 的验证。

## 组织抽样

默认在全分辨率图的 12 个空间位置检查每个独立通道。抽样全为常量时不能证明读到了组织，脚本返回 `NEEDS_TISSUE_ROI`。通过低分辨率预览定位组织，再提供原始像素坐标 JSON，例如：

```json
[{"x": 1200, "y": 1800, "width": 128, "height": 128}]
```

坐标必须来自实际主图，不能原样使用示例。单个 ROI 每边最多 2048 像素，保留的所有通道抽样总量最多 512 MiB，超限时需要缩小区域或减少个数。可加多个区域覆盖组织边缘、中心和不同区域；每个区域都会读取所有通道。抽样一致不能代替所有 WSI 像素的证明，交付时保留此范围说明。

参考：[QuPath 图像格式](https://qupath.readthedocs.io/en/0.6/docs/intro/formats.html)、[OpenSlide MRXS](https://openslide.org/formats/mirax/)、[Bio-Formats 支持格式](https://bio-formats.readthedocs.io/en/stable/supported-formats.html)。

# 测试与复现

先按 [环境准备](../references/environment.md)获得验收通过的 `runtime.json`，再从 Skill 根目录执行。以下 `/data/` 路径均为示例，请替换为实际运行环境和新的工作目录；合成集成测试会运行转换器与 QuPath，需要预留空间。

## 单元检查

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

覆盖输入完整性、通道路由、拒绝覆盖、失败恢复、输出发布门控、安装包安全、SVS 结构，以及严格 KFB／KFBF 解析和缺失策略。v1.2.0 发布验收记录为 134 项通过；当前机器仍应实际执行后判定。

## 合成集成测试

```bash
python3 tests/integration.py --runtime /data/runtime/runtime.json --workdir /data/synthetic-test-new
python3 tests/integration_kfbf.py --runtime /data/runtime/runtime.json --workdir /data/synthetic-kfbf-test-new
python3 tests/integration_kfb.py --runtime /data/runtime/runtime.json --workdir /data/synthetic-kfb-test-new
python3 tests/integration_kfb.py --runtime /data/runtime/runtime.json --workdir /data/synthetic-kfb-tail-new --tiny-tail-case
python3 tests/qupath_kfb_guards.py --runtime /data/runtime/runtime.json --converted-dir /data/synthetic-kfb-tail-new/converted --workdir /data/qupath-negative-tests-new
```

- **通用流程**：生成合成图，实际转换并由 QuPath 读取，比较完整基础层像素。
- **KFBF**：检查 1／3／6／8 动态通道、文件头分流、严格解码、覆盖策略、OME 元数据和完整像素一致性。
- **KFB**：比较全部保留原生层像素，检查缺失策略、重复最小层记录及 1×1 末层读取器限制。
- **QuPath 负例**：确保已知末端层例外不能掩盖其他层数、尺寸或像素错误。

合成数据不含患者或项目切片，不能代替新厂商文件验收，也不能证明所有超大 WSI、输入变体、系统或 QuPath 版本兼容。

## 判定实际结果

检查运行目录中的 `status.json`、`pixel_validation.json` 与 `qupath_validation.json`。KFBF 还需通过 `kfbf_source_zarr_validation.json`；KFB 需核对原生层与缺失区域报告。仅有 TIFF、缩略图或进程退出码不代表验收完成。

| 状态 | 可以报告什么 |
|---|---|
| `QUPATH_VERIFIED` | 指定版本 QuPath 实际读取通过，验证范围以报告为准 |
| `QUPATH_VERIFIED_WITH_READER_LIMITATION` | 读取通过并有已复现限制，必须分别报告导出层与可读层 |
| `CONVERTED_NOT_QUPATH_VERIFIED` | 转换完成，QuPath 读取验证待补充 |

`--skip-qupath` 仅在明确接受降级时使用。`--resume` 复用通过校验的完成阶段，要求源文件清单、代码、配置和参数匹配；输入清单不是全源文件内容哈希。详见 [验收与恢复规则](../references/validation.md)。

真实四／六通道样本的已记录证据见 [v1.2.0 验证摘要](validation-v1.2.0.md)。

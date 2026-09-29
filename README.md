# Estatecut

本地中文口播与房源视频工具：使用 FFmpeg 剪辑、调色、导出，保留可核查的剪辑决定和质量检查结果。

**状态：0.1.0，首次公开版本。** 从持续维护的本地项目中提取可独立使用的核心功能，以 MIT 许可公开。公开历史从本次导出开始，不等于已有社区采用。见 [LICENSE](LICENSE)、[第三方声明](THIRD_PARTY_NOTICES.md) 和 [来源说明](docs/provenance.md)。

Local, reviewable Chinese speech and property-video editing with FFmpeg, explicit review gates, and reproducible synthetic tests.

## 两条工作流

- `talkcut`：素材导入、mock 或显式真实 ASR、人工可编辑 EDL、转录/剪点/字幕审核锁、字幕和多尺寸导出。
- `propcut`：无旁白视频稳定起点检测、手动覆盖、调色、可选用户自带音乐、拼接和导出验证。

不包含客户素材、私人曲库、生产台账、Google Drive/Sheets、发布脚本、模型和字体。音乐授权标签只是用户输入，不能证明拥有使用权。文本关键词检查只是提示，不能代替平台规则、事实核验或人工审核。

## 安装

需要 Python 3.11+，并单独安装带 libx264、libass 的 FFmpeg/FFprobe，把两者放在当前终端 PATH。HDR 色调映射还需要 zscale/tonemap。软件包不捆绑这些外部二进制、模型或字体。

先下载并解压 [源码](https://github.com/yyxone/estatecut/archive/refs/heads/main.zip)，或 `git clone https://github.com/yyxone/estatecut.git`，然后进入仓库目录。

Windows PowerShell：
```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\talkcut.exe --help
.\.venv\Scripts\propcut.exe --help
```

macOS / Linux：
```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/talkcut --help
```

平台状态和实测结果见 VALIDATION.md。macOS/Linux 命令为安装说明，不代表已在对应系统验收。

## 先跑合成素材

无需 API key、客户素材或真实 ASR：
```powershell
$env:ESTATECUT_ENCODER = "libx264"
.\.venv\Scripts\python.exe -m talkcut smoke --out ./out/synthetic-smoke
```

smoke 的审核锁由测试模拟；通过测试不能证明真实视频已获人工认可。它生成并转码短小合成视频，不应用于用户源文件目录。

## 处理自己的视频

复制并编辑 examples/propcut.yaml，设置输入与输出目录，输出必须在输入目录之外。默认示例关闭配乐并保留原声。
```sh
propcut detect --config examples/propcut.yaml
propcut process --config examples/propcut.yaml
```

talkcut 的逐步命令见 `talkcut --help`。paper-edit 只生成默认保留全片的剪辑骨架，不自动判断内容。检查 transcript.json、edl.json 和字幕后再上对应审核锁。编辑后审核锁会失效，需要重新核验。

真实 ASR 只有显式选 real 才执行。可在专用环境安装 `pip install ".[asr]"`，通过当前进程环境变量 `ESTATECUT_ASR_PYTHON` 指向其 Python；缺省使用当前解释器。真实转录默认选择 CUDA，要使用 CPU 须显式传入 `--device cpu`，视频编码器设置不会控制 ASR。首次真实运行可能下载模型；公开版本尚未运行或验证真实 ASR。不要把授权密码写入配置。

可用 `ESTATECUT_ASR_TIMEOUT` 设置有限正秒数的超时；缺省不限时。时间线图可用 `ESTATECUT_FONT` 指定自己的中文字体文件，软件不捆绑字体。自定义预设中的相对 LUT 路径按预设文件所在目录解析。

选曲推荐的兼容索引结构见 docs/music-index.md；基本处理不需要索引，试听可显式提供 `--tracks`。

## 开发与测试

```sh
python -m pip install ".[dev]"
python -B -m pytest -q -p no:cacheprovider
python -m build --wheel
```

测试全部使用临时合成数据，禁止联网。FFmpeg 不可用时部分媒体测试会跳过，不能把跳过视为媒体链路通过。默认软件会优先尝试 NVENC；设 `ESTATECUT_ENCODER=libx264` 可限定 CPU。选曲记录只在用户显式配置时使用；不要把记录提交进仓库。

## 反馈与贡献

欢迎通过 [Issues](https://github.com/yyxone/estatecut/issues) 提交可复现的问题，注明系统、Python、FFmpeg 版本、命令和脱敏日志。优先使用短小的合成素材复现，不上传客户媒体、密钥或私人台账。提交 PR 前运行核心测试，并注明是否验证过真实 ASR。

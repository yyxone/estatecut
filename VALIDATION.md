# 验证范围

2026-09-28，本地 Windows、Python 3.13.12、FFmpeg/FFprobe 8.1。

- LIVE VERIFIED：Windows 隔离环境核心回归 274 passed，0 failed、0 skipped，66.89 秒；包含移植、ASR 超时、LUT 路径、字体选择和不同音乐循环长度的回归。
- LIVE VERIFIED：189 个列明的现用代码、配置、规则及 Git 元数据文件哈希未变；HEAD 与 Git 状态摘要未变。
- CODE VERIFIED：内置配置改为 wheel 包内资源，真实 ASR Python 由环境变量或当前解释器选择；mock ASR 仍为默认。
- CODE VERIFIED：无客户媒体、私人曲库清单、生产数据库、Drive/Sheets 或发布工具被纳入导出白名单。
- LIVE VERIFIED：wheel 在新的独立环境安装，并从源码目录之外导入；两个 CLI 入口正常，talkcut 七阶段合成 QA 为 PASS，propcut 合成视频导出和完整解码为 PASS，合成源文件哈希未变。没有依赖冲突。
- LIVE VERIFIED：Linux / Ubuntu GitHub Actions，Python 3.11、3.13，FFmpeg 6.1.1，两组均通过 274 项测试、wheel 构建及源码目录之外安装后的七阶段 smoke。[运行记录](https://github.com/yyxone/estatecut/actions/runs/36515893974)。
- LIVE VERIFIED：已复现并修复 FFmpeg 6.1.1 连续 acrossfade 丢失音轨的问题；改用线性淡入淡出加延迟混音，保留音轨校验。Windows FFmpeg 6.1.1 相关 33 项回归通过。
- UNVERIFIED：macOS 实际运行、真实 ASR 模型、真实用户成片质量、外部用户采用情况。

测试使用合成音视频与 mock ASR，模拟人审锁不能代替实际审核。CPU 编码并限制测试进程为两个逻辑核、低优先级；不改生产环境。

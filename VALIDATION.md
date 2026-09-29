# 验证范围

2026-09-28，本地 Windows、Python 3.13.12、FFmpeg/FFprobe 8.1。

- LIVE VERIFIED：隔离环境核心回归 272 passed，0 failed、0 skipped；发布副本复跑 66.25 秒，包含移植、ASR 超时、LUT 路径和字体选择回归。
- LIVE VERIFIED：189 个列明的现用代码、配置、规则及 Git 元数据文件哈希未变；HEAD 与 Git 状态摘要未变。
- CODE VERIFIED：内置配置改为 wheel 包内资源，真实 ASR Python 由环境变量或当前解释器选择；mock ASR 仍为默认。
- CODE VERIFIED：无客户媒体、私人曲库清单、生产数据库、Drive/Sheets 或发布工具被纳入导出白名单。
- LIVE VERIFIED：wheel 在新的独立环境安装，并从源码目录之外导入；两个 CLI 入口正常，talkcut 七阶段合成 QA 为 PASS，propcut 合成视频导出和完整解码为 PASS，合成源文件哈希未变。没有依赖冲突。
- CONFIGURED, NOT SMOKED：GitHub Actions 已配置，尚未公开仓库，未运行远程 CI。
- UNVERIFIED：macOS/Linux 实际运行、真实 ASR 模型、真实用户成片质量、外部用户采用情况。

测试使用合成音视频与 mock ASR，模拟人审锁不能代替实际审核。CPU 编码并限制测试进程为两个逻辑核、低优先级；不改生产环境。

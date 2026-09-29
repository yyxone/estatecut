"""propcut — 房产视频批量处理管线。

自动识别"正式进入稳定房间展示"的起点（不是固定剪几秒）→ 裁掉开头
（门牌号 / 开门 / 进门晃动）→ 调色预设 → 本地音乐库配乐 → 导出指定规格。

与 talkcut（口播剪辑）并列的独立管线；复用 estatecut.ffmpeg_tools / utils。
检测结果落盘可复用（reprocess 不重分析），人工 overrides 永远优先于自动检测。
"""

__version__ = "0.1.0"

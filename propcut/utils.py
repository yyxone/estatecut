"""propcut 内部小工具——目前只有源文件内容指纹（C1/C2/C3 共用）。"""
from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 64 * 1024

# 指纹算法版本：头尾 64KB sha256 采样。采样方案变了（块大小/取样位置/摘要算法）
# 就换版本号——旧 sidecar/缓存的 quick_hash 与新算法不可比，靠这个字段判失效。
_ALGO = "ht64k-sha256-v1"


def src_identity(path: Path) -> dict:
    """源文件身份指纹：size + mtime_ns + 头尾 64KB sha256（quick_hash）+ algo 版本。

    只靠路径/秒级 mtime 判"同一个文件"会漏同名替换（重拍同机位、跨文件系统
    拷贝 mtime 复原）；全文件 hash 对几 GB 视频太贵——头尾采样够区分实拍素材，
    字段联合命中才算同源。≤128KB 的小文件直接整文件 hash。
    """
    p = Path(path)
    stat = p.stat()
    h = hashlib.sha256()
    with p.open("rb") as fh:
        if stat.st_size <= 2 * _CHUNK:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                h.update(chunk)
        else:
            h.update(fh.read(_CHUNK))
            fh.seek(-_CHUNK, 2)
            h.update(fh.read(_CHUNK))
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "quick_hash": h.hexdigest(), "algo": _ALGO}

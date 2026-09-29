"""ASR adapter — 外部桥接调 transcription conda env 的 faster-whisper。

硬规则：subprocess shell=False（argv 列表 + env python 绝对路径，不靠 shell activate）；
真实 ASR 仅显式（mode='real'）；tests/smoke 用 mode='mock' 永不真调；记录 model/params/device。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# 显式选择安装了 faster-whisper 的解释器；缺省使用当前环境。
def asr_python() -> Path:
    return Path(os.environ.get("ESTATECUT_ASR_PYTHON") or sys.executable)
RUNNER = Path(__file__).with_name("_asr_runner.py")

# 抗幻觉默认参数（地下室如何看房实战验证）
ANTI_HALLUC = {
    "vad_filter": True,
    "condition_on_previous_text": False,
    "temperature": 0.0,
    "beam_size": 5,
    "initial_prompt": None,
    "word_timestamps": True,
}


def transcribe_wav(wav: Path, mode: str = "mock", device: str = "cuda") -> dict:
    """返回 {'asr': {...}, 'segments': [{start,end,text,words:[{start,end,word}]}]}。

    硬规则：默认 mode='mock'（安全，绝不误跑模型）；真实 ASR 必须显式 mode='real'。
    """
    if mode == "mock":
        return _stamp(_mock(wav), "mock", datetime.now(timezone.utc).isoformat())
    if mode != "real":
        raise ValueError(f"未知 ASR mode: {mode}（real 需显式传入）")
    interpreter = asr_python()
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise FileNotFoundError(f"ESTATECUT_ASR_PYTHON 必须指向存在的 Python 绝对路径: {interpreter}")
    print(f"[ASR] REAL faster-whisper (device={device}) on {wav} —— 显式 mode='real' 触发", file=sys.stderr)
    argv = [str(interpreter), str(RUNNER), str(wav), "--device", device]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # R2 G2 round-2：invoked_at 取发起时刻——长视频同步等完再取时间会差几十分钟
    invoked_at = datetime.now(timezone.utc).isoformat()
    raw_timeout = os.environ.get("ESTATECUT_ASR_TIMEOUT")
    timeout = None
    if raw_timeout:
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("ESTATECUT_ASR_TIMEOUT 必须为正秒数") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("ESTATECUT_ASR_TIMEOUT 必须为有限正秒数")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", shell=False, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ASR 超过 {timeout} 秒；可调整或移除 ESTATECUT_ASR_TIMEOUT") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ASR runner 失败 (exit {proc.returncode}): {(proc.stderr or '').strip()[-800:]}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"ASR runner 输出非 JSON: {e}\n--- stdout 头 ---\n{(proc.stdout or '')[:400]}\n"
            f"--- stderr 尾 ---\n{(proc.stderr or '')[-400:]}"
        ) from e
    # R2 G2 round-2/round-3：runner 输出契约校验——外部进程返回合法 JSON 但结构不对
    #（{"asr": null}/顶层 list/缺 asr 或 segments 键等）不能裸出 TypeError 或被
    # 默认值静默放行，要报明确的 runner-contract 错。get 不带默认：缺键 = 违约。
    if (not isinstance(result, dict)
            or not isinstance(result.get("asr"), dict)
            or not isinstance(result.get("segments"), list)):
        raise RuntimeError(
            f"ASR runner 输出不符合契约（需顶层 dict + asr dict + segments list）: {str(result)[:200]}")
    return _stamp(result, "real", invoked_at)


def _stamp(result: dict, mode: str, invoked_at: str) -> dict:
    """R2 G2（2026-07-16）：转录溯源——transcript.asr 带 mode + invoked_at（发起时刻），
    事后可判这份转录是 mock 还是真实 ASR、什么时候发起的。"""
    result.setdefault("asr", {})
    result["asr"]["mode"] = mode
    result["asr"]["invoked_at"] = invoked_at
    return result


def _mock(wav: Path) -> dict:
    """确定性假转录——供 tests/smoke，绝不真调模型。"""
    segs = [
        {"start": 0.0, "end": 2.0, "text": "大家好这是一段测试口播",
         "words": [{"start": 0.0, "end": 1.0, "word": "大家好"}, {"start": 1.0, "end": 2.0, "word": "这是一段测试口播"}]},
        {"start": 2.0, "end": 4.0, "text": "第二句讲一个要点",
         "words": [{"start": 2.0, "end": 3.0, "word": "第二句"}, {"start": 3.0, "end": 4.0, "word": "讲一个要点"}]},
    ]
    return {"asr": {"engine": "mock", "model": "mock", "device": "cpu", "params": ANTI_HALLUC}, "segments": segs}

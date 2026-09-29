"""跑在 transcription conda env 里（被 asr_adapter 以 env python 调用）。

抗幻觉 faster-whisper 转录 + 词级时间戳 → stdout JSON。
本文件只在显式真实转录时由选定的 ASR Python 解释器执行。
"""
import argparse
import json
import sys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--model", default="large-v3")
    args = ap.parse_args()

    from faster_whisper import WhisperModel

    compute = "float16" if args.device == "cuda" else "int8"
    try:
        model = WhisperModel(args.model, device=args.device, compute_type=compute)
    except Exception:
        # GPU 不可用/崩溃 → CPU fallback（硬规则：明确 fallback，不静默失败）
        model = WhisperModel(args.model, device="cpu", compute_type="int8")
        args.device = "cpu"

    segments, _info = model.transcribe(
        args.wav, language="zh",
        vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500),
        condition_on_previous_text=False, temperature=0.0, beam_size=5,
        word_timestamps=True,
    )
    out_segs = []
    for s in segments:
        words = [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word.strip()} for w in (s.words or [])]
        out_segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(), "words": words})

    print(json.dumps({
        "asr": {"engine": "faster-whisper", "model": args.model, "device": args.device,
                "params": {"vad_filter": True, "condition_on_previous_text": False, "temperature": 0.0,
                           "beam_size": 5, "initial_prompt": None, "word_timestamps": True}},
        "segments": out_segs,
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())

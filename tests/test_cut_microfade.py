"""build_filtergraph micro-fade + clamp 单元测试（锁住 2026-06-13 回灌行为，防回归）。

覆盖 Codex re-review P2-2 建议的 case：单段 / crossfade=0 / 三段 fade 位置 / 短段 clamp / crossfade>>dur / 格式化。
"""
from talkcut.cut import _fmt, build_filtergraph


def _audio_lines(keep, **kw):
    g, _ = build_filtergraph(keep, **kw)
    return [l for l in g.split("\n") if l.startswith("[0:a]")]


def test_fmt_strips_trailing_zeros_and_avoids_sci_notation():
    assert _fmt(0.012) == "0.012"          # 正常 fade 不变
    assert _fmt(10.288) == "10.288"        # st 去尾零
    assert _fmt(0.0030000000000001137) == "0.003"  # clamp 长浮点尾巴
    assert _fmt(0.00001) == "0.00001"      # 极小值不输出科学计数法 1e-05
    assert _fmt(0.0) == "0"                # st=0 头 fade


def test_single_segment_no_fade():
    g, _ = build_filtergraph([{"src_start": 0, "src_end": 10}])
    assert "afade" not in g  # 单段 head=tail=False，无 fade


def test_crossfade_off():
    g, _ = build_filtergraph(
        [{"src_start": 0, "src_end": 10}, {"src_start": 10, "src_end": 20}], crossfade=0)
    assert "afade" not in g


def test_three_segments_fade_positions():
    # 首段只尾 fade / 中段头尾 / 尾段只头
    al = _audio_lines([{"src_start": 0, "src_end": 10},
                       {"src_start": 10, "src_end": 20},
                       {"src_start": 20, "src_end": 30}])
    assert "afade=t=in" not in al[0] and "afade=t=out" in al[0]
    assert "afade=t=in" in al[1] and "afade=t=out" in al[1]
    assert "afade=t=in" in al[2] and "afade=t=out" not in al[2]


def test_normal_segment_fade_value_clean():
    g, _ = build_filtergraph([{"src_start": 0, "src_end": 26.5},
                              {"src_start": 34.4, "src_end": 44.7}])
    assert "d=0.012:curve=qsin" in g       # 正常段 fade 干净 0.012
    assert "0.0030000" not in g            # 无长浮点尾巴


def test_short_segment_no_negative_st():
    # 中间段 6ms，clamp 后不应有负 st
    g, _ = build_filtergraph([{"src_start": 0, "src_end": 10},
                              {"src_start": 10, "src_end": 10.006},
                              {"src_start": 20, "src_end": 30}])
    assert "st=-" not in g


def test_crossfade_larger_than_segment_no_negative_st():
    # crossfade 远大于段长，仍无负 st（clamp 收敛）
    g, _ = build_filtergraph([{"src_start": 0, "src_end": 1},
                              {"src_start": 1, "src_end": 2},
                              {"src_start": 2, "src_end": 3}], crossfade=5.0)
    assert "st=-" not in g

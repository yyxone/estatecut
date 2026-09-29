"""§7 qa-export — loudness 归一 + 多版本导出 + 小红书/抖音违禁词 screen。

竖屏 9:16（模糊背景填充，不裁人）为主版 + 横屏 master + review proxy。
固定 preset：1080×1920 / H.264 / 30fps。平台时长/大小/码率上限**导出时实查不硬编码**。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from estatecut.compliance import load_terms
from estatecut.ffmpeg_tools import probe_media, run_command, run_video_encode

from .state import load_state, mark_stage, media_fingerprint, require_lock

from estatecut.resources_path import resource_path
XHS_COMPLIANCE = resource_path("xhs_compliance.yaml")

# target 保持 I=-16（Steven 决策，未改成 -14；小红书/抖音实际目标响度发布前实查）
LOUDNORM_TARGET = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _measure_loudnorm(src: Path, log: Path | None = None) -> dict | None:
    """第一遍 loudnorm 测量（print_format=json），返回 measured 参数；失败/无音频/纯静音 → None（回退一遍）。

    借鉴 video-use render.py measure_loudness（评估见 docs/references/video-use-eval-20260701.md）：
    两遍比一遍更准——先测真实 I/TP/LRA/thresh/offset，第二遍 linear 校正到 target。
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(src), "-af",
         f"{LOUDNORM_TARGET}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, shell=False)
    stderr = proc.stderr
    if log:
        with log.open("a", encoding="utf-8") as h:
            h.write("$ [loudnorm measure]\n" + stderr[-1500:] + "\n")
    lb, rb = stderr.rfind("{"), stderr.rfind("}")
    if lb < 0 or rb < 0 or rb < lb:
        return None
    try:
        data = json.loads(stderr[lb:rb + 1])
    except json.JSONDecodeError:
        return None
    need = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not need <= set(data):
        return None
    # ffmpeg 偶发返回 inf/-inf（纯静音）——不可用于第二遍，回退一遍
    if any(str(data[k]).lstrip("-").lower().startswith("inf") for k in need):
        return None
    return data


def _loudnorm_filter(measured: dict | None) -> str:
    """构造 loudnorm 滤镜串：有 measured → 两遍 linear 校正；无 → 一遍（回退，行为同改动前）。"""
    if not measured:
        return LOUDNORM_TARGET
    return (f"{LOUDNORM_TARGET}:measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:linear=true")


def _src_for_export(out_dir: Path, state: dict) -> Path:
    """选导出源：文件存在还不够——state 里对应阶段必须是本轮 done + verified，
    且文件指纹与阶段完成时记录的一致。

    上游重跑后下游被失效回 pending（state.DOWNSTREAM），此时磁盘上残留的
    fine_cut.mp4/rough_cut.mp4 是旧产物；status=done 也只是可手改的声明——
    完成后被同名替换的文件、未经校验（verified=False）的产物、无指纹记录的
    legacy state 一律 fail-closed，不出门。
    """
    for name, stage in (("fine_cut.mp4", "fine_cut"), ("rough_cut.mp4", "rough_cut")):
        p = out_dir / name
        if p.exists():
            entry = state["stages"][stage]
            if entry["status"] != "done":
                raise RuntimeError(
                    f"{name} 存在但 state 显示 {stage} 未完成本轮（status={entry['status']}）——"
                    f"上游重跑后须重跑 {stage.replace('_', '-')}，或移走陈旧产物再导出"
                )
            if entry.get("verified") is not True:
                raise RuntimeError(
                    f"{name} 对应阶段 {stage} 的 verified={entry.get('verified')!r}——"
                    f"未经校验的产物不导出，重跑 {stage.replace('_', '-')} 并确认校验通过"
                )
            identity = entry.get("outputs_identity")
            if not isinstance(identity, dict) or name not in identity:
                raise RuntimeError(
                    f"{stage} 的 state 缺 {name} 的 outputs_identity 指纹（legacy state 无法"
                    f"证明文件来历）——重跑 {stage.replace('_', '-')} 后再导出"
                )
            if media_fingerprint(p) != identity[name]:
                raise RuntimeError(
                    f"{name} 与 {stage} 完成时记录的指纹不符——文件在阶段完成后被替换/改动过，"
                    f"fail-closed 拒绝导出；重跑 {stage.replace('_', '-')} 或核对该文件来历"
                )
            return p
    raise FileNotFoundError("无 fine_cut/rough_cut 可导出")


def compliance_screen(out_dir: Path) -> tuple[list[str], dict]:
    """对 字幕.srt 做违禁词扫描（advisory，不阻断）。返回 (hits, check)。

    复用 estatecut.compliance.load_terms 的结构化词表（中英文皆扫、大小写无关子串匹配），
    取代旧的正则刮 yaml 原文——后者漏掉全部英文 Fair-Housing 词，且把 `100%有效`
    这类含标点词碎片化（整词漏报 + 碎片误报）。

    round-2 P2-11：三种态可区分——无 SRT（N/A，不降级）/ 词表配置缺失（SKIPPED
    → PARTIAL，**不可与"已检零命中"混同**）/ 已扫（PASS，命中数进 reason，
    advisory 人工复核不阻断）。
    """
    srt = out_dir / "字幕.srt"
    if not srt.exists():
        return [], {"status": "SKIPPED", "applicable": False,
                    "reason": "N/A：无 字幕.srt，违禁词 screen 无扫描对象"}
    if not XHS_COMPLIANCE.exists():
        return [], {"status": "SKIPPED",
                    "reason": (f"违禁词配置缺失（{XHS_COMPLIANCE.name}）——screen 未执行，"
                               "与'已检零命中'不可混同；补配置后重跑")}
    text = srt.read_text(encoding="utf-8").lower()
    terms = load_terms(XHS_COMPLIANCE)
    hits = sorted({t for t in terms if t and t.lower() in text})
    return hits, {"status": "PASS",
                  "reason": (f"已扫 {len(terms)} 词，命中 {len(hits)}"
                             + ("（advisory 人工复核，不阻断）" if hits else "（0 命中）"))}


def _duration_tolerance(expected: float) -> float:
    """R4 round-2 P1-7：2% 相对容差加 1s 硬上限——30 分钟片允许 36s 缺失还判
    PASS 是假绿。短片下限 0.5s（容器/编码器固有抖动）。"""
    return max(0.5, min(0.02 * expected, 1.0))


def _stream_durations(path: Path) -> tuple[float | None, float | None]:
    """(视频流时长, 音频流时长)——A/V 流时长互核用；读不到的返回 None。"""
    out = []
    for sel in ("v:0", "a:0"):
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries",
             "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, shell=False)
        try:
            out.append(float((proc.stdout or "").strip()) if proc.returncode == 0 else None)
        except ValueError:
            out.append(None)
    return out[0], out[1]


def _check_export(path: Path, src_dur: float | None, expected_fps: float | None,
                  spec: dict | None = None, log: Path | None = None) -> dict:
    """单个导出产物的 QA 检查（R4 Q5 + round-2）：probe / 完整解码扫描 / 时长
    （含 A/V 流互核）/ fps / 规格（codec/分辨率），每项
    `{"status": PASS|FAIL|SKIPPED, "reason"[, "applicable"]}`。
    `applicable: False` 的 SKIPPED = 契约性不适用（不降级 verdict）；
    其余 SKIPPED = 想查查不了 → PARTIAL。"""
    path = Path(path)
    try:
        pm = probe_media(path)
    except Exception as exc:  # noqa: BLE001 — probe 直接炸 = 产物根子坏，整体 FAIL
        return {"probe": {"status": "FAIL", "reason": f"ffprobe 失败：{exc}"}}
    checks: dict[str, dict] = {}
    size = path.stat().st_size
    good = size > 0 and pm["audio_present"] and pm["width"] > 0 and pm["height"] > 0
    checks["probe"] = {"status": "PASS" if good else "FAIL",
                       "reason": f"{pm['width']}x{pm['height']} audio={pm['audio_present']} {size}B"}
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                          capture_output=True, text=True, shell=False)
    if log:
        with log.open("a", encoding="utf-8") as h:
            h.write(f"$ [decode scan] {path.name}\n{(proc.stderr or '')[-800:]}\n[exit {proc.returncode}]\n")
    decode_ok = proc.returncode == 0 and not (proc.stderr or "").strip()
    checks["decode"] = ({"status": "PASS", "reason": "完整解码无错误"} if decode_ok else
                        {"status": "FAIL",
                         "reason": ((proc.stderr or "").strip() or f"解码扫描 exit {proc.returncode}")[:300]})
    if src_dur is None:
        checks["duration"] = {"status": "SKIPPED", "reason": "源时长不可得，无法核对"}
    else:
        tol = _duration_tolerance(src_dur)
        dur = pm["duration_sec"]
        if abs(dur - src_dur) > tol:
            checks["duration"] = {"status": "FAIL",
                                  "reason": f"{dur:.2f}s 与源 {src_dur:.2f}s 偏差超 ±{tol:.2f}s"}
        else:
            checks["duration"] = {"status": "PASS",
                                  "reason": f"{dur:.2f}s ≈ 源 {src_dur:.2f}s（±{tol:.2f}s）"}
    # round-2 P1-7 + round-3 P1-D：A/V 流互核独立成项——任一流时长读不到不再
    # 借 duration 的 PASS 蒙混（原实现落 else 还谎称"互核一致"），SKIPPED → PARTIAL
    v_dur, a_dur = _stream_durations(path)
    if v_dur is None or a_dur is None:
        checks["av_streams"] = {"status": "SKIPPED",
                                "reason": (f"流时长不可读（v={v_dur} a={a_dur}），互核未执行"
                                           "——人工核对成片音画完整（音频是主轴）")}
    else:
        av_tol = _duration_tolerance(src_dur if src_dur is not None else max(v_dur, a_dur))
        if abs(v_dur - a_dur) > av_tol:
            checks["av_streams"] = {"status": "FAIL",
                                    "reason": (f"音视频流时长不一致：v={v_dur:.2f}s a={a_dur:.2f}s"
                                               f"（差超 ±{av_tol:.2f}s）——疑似半截产物")}
        else:
            checks["av_streams"] = {"status": "PASS",
                                    "reason": f"A/V 流互核一致：v={v_dur:.2f}s a={a_dur:.2f}s（±{av_tol:.2f}s）"}
    if expected_fps is None:
        checks["fps"] = {"status": "SKIPPED", "applicable": False,
                         "reason": "N/A：master -c:v copy 跟源帧率（docs/workflow.md §7）"}
    else:
        fps = pm["fps"] or 0.0
        checks["fps"] = ({"status": "PASS", "reason": f"{fps:.2f}fps == {expected_fps:g}"}
                         if abs(fps - expected_fps) < 0.05 else
                         {"status": "FAIL", "reason": f"{fps:.2f}fps ≠ 固定 preset {expected_fps:g}fps"})
    # round-2 P1-8：固定规格真核对——vertical 必须 1080×1920/H.264，proxy 720p/H.264，
    # master 对照源（spec 由调用方给；None = 源规格不可得等，SKIPPED 不适用不降级）
    if spec is None:
        checks["spec"] = {"status": "SKIPPED", "applicable": False,
                          "reason": "N/A：未定义固定规格（源探测不可得时 master 无对照）"}
    else:
        mismatch = []
        if "codec" in spec and (pm.get("codec_name") or "").lower() != spec["codec"]:
            mismatch.append(f"codec={pm.get('codec_name')}≠{spec['codec']}")
        if "width" in spec and pm["width"] != spec["width"]:
            mismatch.append(f"width={pm['width']}≠{spec['width']}")
        if "height" in spec and pm["height"] != spec["height"]:
            mismatch.append(f"height={pm['height']}≠{spec['height']}")
        checks["spec"] = ({"status": "PASS",
                           "reason": "规格符合：" + "/".join(f"{k}={v}" for k, v in spec.items())}
                          if not mismatch else
                          {"status": "FAIL", "reason": "规格不符：" + "；".join(mismatch)})
    return checks


_VALID_STATUSES = {"PASS", "FAIL", "SKIPPED"}

# round-7（Codex round-6 P1）：collector 遇非 JSON 类型容器时上报的毒叶 status。
# 刻意取一个**不在** _VALID_STATUSES 的字符串——生产分支身份核对（毒叶非任何
# prod_leaf）与通用树聚合（`_valid_status` 判非法）都会因它 fail-closed 返回 FAIL。
_UNKNOWN_CONTAINER_STATUS = "__UNKNOWN_CONTAINER__"

# round-3 P1-C：每个 export 必须交齐的检查集合——缺项/空 checks 不再从聚合里
# 静默消失（原递归 walk 会让 checks={} 的 export 贡献零叶子，靠别的分支 PASS 假绿）
_REQUIRED_EXPORT_CHECKS = frozenset(
    {"probe", "decode", "duration", "av_streams", "fps", "spec"})
_REQUIRED_TOP_BRANCHES = frozenset({"subtitle", "loudnorm", "compliance"})
# round-4（Codex round-3 P1）：任一生产报告标记在场（exports 分支或任一顶层分支）
# 即按生产报告强校验——缺 exports 分支本身也是"缺角"，不能因缺键就整体跳过结构门。
_PRODUCTION_MARKERS = _REQUIRED_TOP_BRANCHES | {"exports"}


def _valid_status(value) -> bool:
    """status 合法性——**精确** str 类型 + 合法枚举。round-5（Codex round-4 P2）：
    不可哈希（[]/{}）或非字符串（None/int）的 status 一律 False（→ FAIL），
    不让 `x in set` 抛 TypeError（验证器崩溃比诚实返回 FAIL 更糟）。round-8（Codex
    round-7 P1）：用 `type(value) is str` 而非 `isinstance`——str **子类**可覆写
    `__eq__`/`__hash__` 把 FAIL 伪装成合法 PASS（撒谎子类），精确类型一律拒绝、且短路避免
    对恶意 `__hash__` 求值崩溃。生产 status 恒为原生 str 字面量，无假红。"""
    return type(value) is str and value in _VALID_STATUSES


def _is_status_leaf(node) -> bool:
    """合法 status 叶子 = **精确** dict 且 status 属合法枚举。round-4：结构门光查键在场不够——
    值为 {}/缺 status/非法枚举的"检查"在 walk 里贡献零叶子，会被顶层 PASS 假绿掩盖。
    round-8（Codex round-7 P1）：`type(node) is dict` 而非 `isinstance`——dict 子类覆写
    `get()`/`values()` 可伪装叶子或藏内层 FAIL；精确类型 + collector 毒叶双保险。"""
    return type(node) is dict and _valid_status(node.get("status"))


def _aggregate_leaves(leaves: list[dict]) -> str:
    """一组已收集的 status 叶子 → verdict（round-2 P2-9 fail-closed 版）：
    空集合 / 非法 status → FAIL；任一 FAIL → FAIL；存在 `applicable` 非 False 的
    SKIPPED → PARTIAL；其余 → PASS。status 合法性走 `_valid_status`（不可哈希不崩）。"""
    if not leaves:
        return "FAIL"
    if any(not _valid_status(leaf.get("status")) for leaf in leaves):
        return "FAIL"
    if any(leaf["status"] == "FAIL" for leaf in leaves):
        return "FAIL"
    if any(leaf["status"] == "SKIPPED" and leaf.get("applicable") is not False
           for leaf in leaves):
        return "PARTIAL"
    return "PASS"


def _collect_status_nodes(node, acc: list) -> None:
    """收集树中**每一个**带 status 键的 dict——遇 status 继续下钻、进 list。收进 `acc`
    的节点供生产分支全账核对（`n is leaf` 身份比对）/ 通用树全量聚合。fail-closed 真值门
    的三条铁律（round-6 红队 + Codex round-6 加固，round-7 落地）：
    ① **显式栈迭代（非递归）**——超深无环树不再 RecursionError（「崩溃比诚实返回 FAIL
       更糟」，同 round-5 P2 立约）；
    ② **只认 JSON 反序列化能产生的类型、且精确匹配**（dict/list/str/bool/int/float/None，
       用 `type(x) is …` 而非 `isinstance`）——出现任何其它类型，包括 tuple/deque/dict
       view/自定义可迭代，**以及 dict/list/str/int 的子类**，一律当"看不懂、可能藏
       status"，上报一个非法 status 毒叶 `{"status": _UNKNOWN_CONTAINER_STATUS}` → 生产
       分支身份核对（毒叶非任何 prod_leaf → FAIL）与通用树聚合（毒叶 status 非法 → FAIL）
       都 fail-closed。round-8（Codex round-7 P1）：`isinstance` 会放行 dict/list 子类，
       其覆写的 `values()`/`__iter__()` 能让 collector 拿不到内层子节点 → 真实 FAIL 被
       隐藏假绿；**精确 `type() is` 一次性封死子类逃逸**。**不逐容器类型 whitelist 追补**
       ——list→tuple→deque→dict_view→子类 是打不完的地鼠，"非精确 JSON 类型即拒"是唯一
       治本（生产报告经 json.load / dict·list 字面量构造只产原生类型，真实 cmd 恒为原生
       list，故无生产误伤）；
    ③ **按 id 去重**——自引用/环状/共享对象图不重复下钻、也不漏后代 FAIL。"""
    seen: set[int] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if type(cur) is dict:
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            if "status" in cur:
                acc.append(cur)
            stack.extend(cur.values())
        elif type(cur) is list:
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            stack.extend(cur)
        elif type(cur) in (str, bool, int, float) or cur is None:
            continue  # JSON 原生标量，无 status
        else:
            # 非精确 JSON 类型（tuple/deque/dict view/子类/自定义容器…）——看不懂即 fail-closed 毒叶
            acc.append({"status": _UNKNOWN_CONTAINER_STATUS})


def _all_dict_keys_are_str(node) -> bool:
    """round-11（Codex round-8 P1）：**无 dunder 键类型预检**——整个对象图里每个**精确** dict
    的 key 必须 `type(key) is str`，否则返回 False（调用方 fail-closed FAIL）。

    背景（第 7 种绕过，contract #1 假绿）：round-8 的精确类型只护容器与 **value 侧**；核心逻辑随后
    对**键**做 `set(all_checks)`（marker 门）/`_REQUIRED_EXPORT_CHECKS <= set(checks)`（子集门）/
    `"status" in cur`（成员门）等运算，会触发 key 的 `__eq__`/`__hash__`。恶意键的 `__eq__` 可以
    **不抛异常、原地删除承载 FAIL 的兄弟 value、返回 NotImplemented**——真实 FAIL 被静默抹掉、
    `except BaseException` 因无异常抛出而从不介入 → PASS 假绿（round-9/10 的「撒谎键藏不住 value
    FAIL」只对**只读**键成立，对**图变异键为假**）。同根因还放行非 JSON 键。

    本预检**只用** `type()` + 键/值迭代 + `id()`，**绝不触发 key 的 dunder**：`for k in cur` 产出
    dict 内部已存的键对象（不重算 hash、不比较）、`type(k) is str` 是类型槽比对（不可被 `__class__`
    伪造）、`stack.extend(cur.values())` 只迭代值——故恶意键的副作用/抛异常代码在预检里**根本不执行**，
    预检自身对任意键都不崩、不被变异。遍历形状与 `_collect_status_nodes` 一致（只下钻精确 dict 的
    values 与精确 list 的元素 + `id()` 环去重），覆盖后续所有会对键做集合/子集/成员/`.get()` 运算的
    精确 dict 节点（这些运算全部只发生在已被结构门 / root 守卫收窄为精确 dict 的节点上）；非精确容器
    由 collector 毒叶 / 结构门另行 fail-closed，此处不下钻。

    这是**正向不变式**（"全图键恒为精确 str" ⟹ 后续所有键运算纯净：无副作用、无抛异常、无非 str 混入），
    一次性封死键通道，非逐类型打地鼠。"""
    seen: set[int] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if type(cur) is dict:
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            for k in cur:
                if type(k) is not str:
                    return False
            stack.extend(cur.values())
        elif type(cur) is list:
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            stack.extend(cur)
    return True


def _verdict_from_checks(all_checks: dict) -> str:
    """QA verdict 真值门（对**任意** in-memory 输入 fail-closed）。

    round-8 收口内红队（5 视角实跑）：round-8 的精确类型只护住容器类型与 **value 侧**，
    护不到 dict 的 **KEY**——恶意键（覆写 `__eq__`/`__hash__` 抛异常或哈希碰撞 marker）能过
    root 类型守卫，随后核心逻辑的 `set(all_checks)`（marker 选择门）与
    `_REQUIRED_EXPORT_CHECKS <= set(checks)`（子集门）/ collector 的 `"status" in cur`（成员门）
    对键做集合/子集/成员运算 → 触发键 dunder 抛异常穿透 → **raise 而非 return**，违反
    contract #2（绝不崩溃，崩溃比诚实 FAIL 更糟）。

    前 4 轮（容器白名单→JSON-only→精确 type→KEY 通道）证明**枚举攻击面打不完**。此处不再
    逐通道打补丁，改为最外层 `try/except → FAIL` **全局兜底**：把「任何错误 → 诚实 FAIL」作为
    不变式在边界一次性强制，覆盖 KEY 通道抛异常及任何未预见的 dunder 触发点。
    - **不可能假红**：生产 all_checks 全由普通 dict/list/str 字面量构造，永不抛，永不进兜底。
    - **兜底捕 `BaseException`（round-10 收敛红队收口）**：round-9 曾写 `except Exception`，但
      Exception 是 BaseException 的**严格子集**——KEY dunder 抛非-Exception 的 BaseException
      子类（自定义 `class X(BaseException)`）时 `except Exception` 抓不到 → 仍穿透。「任何错误
      → FAIL」本就该覆盖整个 BaseException（raise 层级的根，无更上层可抛物），故收敛到
      `except BaseException`——crash 通道修完无残留。同时**显式重抛 KeyboardInterrupt/SystemExit**：
      Ctrl-C 与进程退出信号不能被真值门吞掉（否则破坏用户中断/退出能力），它们是仅有的两个
      刻意放行的控制流信号。

    **round-11（Codex round-8 P1）订正**：上面的全局兜底只封死了 KEY 通道的 **crash**（抛异常）。
    Codex round-8 挖出**第 7 种绕过 = KEY 通道的 false-green（contract #1，最坏）**：恶意键的 `__eq__`
    可以**不抛异常**、在成员/marker 门触发时**原地删除**承载 FAIL 的兄弟 value、返回 NotImplemented——
    真实 FAIL 被静默抹掉、`except BaseException` 因无异常而从不介入 → PASS 假绿。故 round-9/10「value 侧
    FAIL 与键无关、撒谎键藏不住」的论证**只对只读键成立，对图变异键为假**。真正治本是 `_verdict_from_checks_core`
    在任何键运算**之前**做的**无 dunder 键类型预检**（`_all_dict_keys_are_str`：全图每个精确 dict 的
    key 必须 `type(key) is str`）——正向不变式让恶意键在其 dunder 被触发前即遭 fail-closed 拦下，删/改
    FAIL 的副作用根本不发生（`eq_calls==0`）；兜底 `except BaseException` 退居第二层，仅兜任何未预见的抛异常。
    判别逻辑全在 `_verdict_from_checks_core`。"""
    try:
        return _verdict_from_checks_core(all_checks)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return "FAIL"


def _verdict_from_checks_core(all_checks: dict) -> str:
    """收 `{"status", ...}` 叶子聚合 verdict。

    生产报告（带 exports 或顶层分支标记）走**直接聚合**：显式索引 exports→entry→
    checks→叶子 + 三个顶枝。round-6（Codex round-5 P1）：仅拒绝「容器**自身**带
    status」不够——还有两类未纳入聚合的 status 会藏真 FAIL：① 名义叶子内部再藏
    status 后代（`{"status":"PASS","detail":{"status":"FAIL"}}`）；② 顶层或 entry
    新增的未知 status 分支（`postflight={"status":"FAIL"}`）被显式索引静默跳过。
    故收齐显式叶子后再做**全账核对**：递归扫出树中每一个 status 节点，凡不在已纳入
    聚合的叶子集合内一律 FAIL（fail-closed，任何看不懂的 status 都当可能藏 FAIL）。
    结构缺角（exports 缺失/为空、缺必需检查项、检查值非合法 status 叶子、缺顶枝）一律
    FAIL。通用检查树（无生产标记）改用同 collector 收全部 status 再聚合——round-5 的
    walk 见祖先 status 即止步仍会遮蔽子级 FAIL（P2），继续下钻方能让埋深的 FAIL 浮出。
    status 合法性判断统一走 `_valid_status`，不可哈希/非字符串不崩。round-8（Codex
    round-7 P1）：① **root 守卫**——非**精确** dict 根（list/int/str/None/dict 子类）
    直接 FAIL，避免 `set(all_checks)`（list-of-dict unhashable / int not-iterable）或
    子类覆写的 `__contains__` 在结构门里崩溃；② 所有先于 collector 的结构门（exports/
    entry/checks）由 `isinstance` 收紧为 `type() is dict`——dict 子类不再蒙混过门。"""
    if type(all_checks) is not dict:
        return "FAIL"
    # round-11（Codex round-8 P1）：任何 set()/成员/`.get()` 键运算**之前**，先无 dunder 预检全图
    # 键类型——非精确 str 键（含图变异 __eq__ 删 FAIL 的恶意键、非 JSON 键）一律 fail-closed，
    # 封死键通道的假绿/假崩，且预检不触发键 dunder（恶意副作用不发生）。
    if not _all_dict_keys_are_str(all_checks):
        return "FAIL"
    if _PRODUCTION_MARKERS & set(all_checks):
        if "status" in all_checks:
            return "FAIL"
        exports = all_checks.get("exports")
        if type(exports) is not dict or not exports or "status" in exports:
            return "FAIL"
        prod_leaves: list[dict] = []
        for entry in exports.values():
            if type(entry) is not dict or "status" in entry:
                return "FAIL"
            checks = entry.get("checks")
            if type(checks) is not dict or "status" in checks:
                return "FAIL"
            if not _REQUIRED_EXPORT_CHECKS <= set(checks):
                return "FAIL"
            for v in checks.values():
                if not _is_status_leaf(v):
                    return "FAIL"
                prod_leaves.append(v)
        if not _REQUIRED_TOP_BRANCHES <= set(all_checks):
            return "FAIL"
        for b in _REQUIRED_TOP_BRANCHES:
            node = all_checks.get(b)
            if not _is_status_leaf(node):
                return "FAIL"
            prod_leaves.append(node)
        # 全账核对（round-6 P1-A/P1-B）：树中每个 status 节点都必须是上面显式纳入
        # 聚合的叶子；任何未纳入的 status（藏叶内 / 未知顶层或 entry 分支）→ FAIL。
        all_status_nodes: list[dict] = []
        _collect_status_nodes(all_checks, all_status_nodes)
        for n in all_status_nodes:
            if not any(n is leaf for leaf in prod_leaves):
                return "FAIL"
        return _aggregate_leaves(prod_leaves)

    leaves: list[dict] = []
    _collect_status_nodes(all_checks, leaves)
    return _aggregate_leaves(leaves)


def qa_export(out_dir: Path, project: str, slug: str | None = None,
              log: Path | None = None) -> dict:
    out_dir = Path(out_dir)
    slug = slug or project

    # R4 Q7：对客字幕门控最前置——字幕.srt 存在但 subtitle_lock 未上/被改 → 整个导出拒绝
    #（未经人审的对客文本不得随包出门）；不存在 → SKIPPED（N/A，无字幕流程合法）
    if (out_dir / "字幕.srt").exists():
        require_lock(out_dir, project, "subtitle_lock")
        subtitle_check = {"status": "PASS", "reason": "subtitle_lock 审批通过且内容未变"}
    else:
        subtitle_check = {"status": "SKIPPED", "applicable": False,
                          "reason": "N/A：无 字幕.srt 产物（无字幕流程）"}

    src = _src_for_export(out_dir, load_state(out_dir, project))
    exports = out_dir / "exports"; exports.mkdir(parents=True, exist_ok=True)
    # round-2 P2-11：两遍 loudnorm 的退化不再静默——measure 失败 = 一遍处理，
    # 响度精度降级，进结构化 check（→ PARTIAL）
    measured = _measure_loudnorm(src, log)
    loudnorm_af = _loudnorm_filter(measured)
    loudnorm_check = ({"status": "PASS", "reason": "两遍 loudnorm（measured linear 校正）"}
                      if measured else
                      {"status": "SKIPPED",
                       "reason": "loudnorm 第一遍测量失败/不可用——已退化为一遍处理，响度精度降级"})
    try:
        pm_src = probe_media(src)
    except Exception:  # noqa: BLE001 — 源读不出 → duration/master spec 均 SKIPPED
        pm_src = None
    src_dur = (pm_src or {}).get("duration_sec") or None

    # 竖屏 9:16：模糊背景填充（不裁人）。固定 preset 30fps（docs/workflow.md §7，R4 Q6）
    vertical = exports / f"{slug}_vertical_9x16.mp4"
    vgraph = ("[0:v]split=2[bg][fg];"
              "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=20[bgb];"
              "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
              "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[outv]")
    vproc = run_video_encode(lambda enc: ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
                 "-filter_complex", vgraph, "-map", "[outv]", "-map", "0:a",
                 *enc, "-pix_fmt", "yuv420p", "-af", loudnorm_af,
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                 "-r", "30", str(vertical), "-y"], log)

    # 横屏 master：loudnorm，视频 copy（帧率跟源，契约明文不强制 30）
    master = exports / f"{slug}_master_landscape.mp4"
    mproc = run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
                 "-c:v", "copy", "-af", loudnorm_af,
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(master), "-y"], log)

    # review proxy：720p 小体积，同固定 preset 30fps。round-3 P2-1：proxy 同样过
    # loudnorm——全局 loudnorm check 声称的处理必须覆盖全部三版，不留未处理版本
    proxy = exports / f"{slug}_proxy.mp4"
    pproc = run_video_encode(lambda enc: ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
                 "-vf", "scale=-2:720", *enc, "-pix_fmt", "yuv420p", "-af", loudnorm_af,
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                 "-r", "30", str(proxy), "-y"], log)

    hits, compliance_check = compliance_screen(out_dir)

    # R4 Q5 + round-2 P1-8：每版 probe + 完整解码扫描 + 时长（A/V 互核）+ fps +
    # 固定规格核对；报告记实际执行的命令 argv（proc.args——NVENC 回退后是真正
    # 跑成的那条）+ 产物指纹，可追溯。master -c:v copy → 规格对照源。
    master_spec = ({"codec": (pm_src.get("codec_name") or "").lower(),
                    "width": pm_src["width"], "height": pm_src["height"]}
                   if pm_src and pm_src.get("codec_name") else None)
    plan = ((vertical, vproc, 30.0, {"codec": "h264", "width": 1080, "height": 1920}),
            (master, mproc, None, master_spec),
            (proxy, pproc, 30.0, {"codec": "h264", "height": 720}))
    entries = {}
    for p, proc, fps, spec in plan:
        entries[p.name] = {
            "path": str(p),
            "identity": media_fingerprint(p),
            "cmd": [str(c) for c in proc.args],
            "checks": _check_export(p, src_dur, fps, spec=spec, log=log),
        }

    advisory = ("⚠️ 平台规格（最大时长/文件大小/码率）发布前到小红书/抖音当前规格实查，本工具不硬编码也未自动拉取。")
    all_checks = {"exports": entries, "subtitle": subtitle_check,
                  "loudnorm": loudnorm_check, "compliance": compliance_check}
    verdict = _verdict_from_checks(all_checks)
    report = {
        "src": {"path": str(src), "identity": media_fingerprint(src)},
        "exports": entries,
        "subtitle": subtitle_check,
        "loudnorm": loudnorm_check,
        "compliance": compliance_check,
        "compliance_hits": hits,
        "platform_advisory": advisory,
        "verdict": verdict,
    }
    report_path = exports / "_qa_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    outs = [str(vertical), str(master), str(proxy)]
    verified = verdict == "PASS"
    # round-2 P2-10：报告自身也进阶段 outputs——mark_stage 记指纹，报告被换可自证
    mark_stage(out_dir, project, "qa_export", "done", outs + [str(report_path)],
               verified=verified,
               note=f"竖屏+master+proxy；QA verdict={verdict}；违禁词 {len(hits)} 命中；{advisory}")
    return {"exports": outs, "compliance_hits": hits, "checks": all_checks,
            "subtitle": subtitle_check, "verdict": verdict, "verified": verified,
            "advisory": advisory, "report": report, "report_path": str(report_path)}

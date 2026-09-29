"""项目 state.json 读写 + 3 锁门控。conforms schemas/state.schema.json。

R3 起：锁 = 绑定产物内容的审批记录（不是布尔）；state 原子写；
损坏 state 显式报错不静默修补；上游重跑按 DAG 失效下游阶段与锁。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import STAGES

LOCKS = ["transcript_lock", "cut_lock", "subtitle_lock"]

# 锁 → 被人审批的产物（相对 out_dir）
LOCK_ARTIFACTS = {
    "transcript_lock": "transcript.json",
    "cut_lock": "edl.json",
    "subtitle_lock": "字幕.srt",
}

# 锁 → 产出该产物的阶段（上游重跑失效锁用）
LOCK_STAGE = {
    "transcript_lock": "transcribe",
    "cut_lock": "paper_edit",
    "subtitle_lock": "subtitle",
}

# 阶段失效 DAG（非线性：subtitle 依赖 transcript+edl，不依赖 rough_cut 成片）
DOWNSTREAM = {
    "ingest": ["transcribe", "paper_edit", "rough_cut", "fine_cut", "subtitle", "qa_export"],
    "transcribe": ["paper_edit", "rough_cut", "fine_cut", "subtitle", "qa_export"],
    "paper_edit": ["rough_cut", "fine_cut", "subtitle", "qa_export"],
    "rough_cut": ["fine_cut", "qa_export"],
    "fine_cut": ["qa_export"],
    "subtitle": ["qa_export"],
    "qa_export": [],
}

_STATUS_ENUM = {"pending", "done", "skipped", "failed"}
_LOCK_RECORD_REQUIRED = ("artifact", "sha256", "approved_at")
_MISSING = object()
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TMP_SEQ = itertools.count()

_CHUNK = 64 * 1024
_FINGERPRINT_ALGO = "ht64k-sha256-v1"


def media_fingerprint(path: Path) -> dict:
    """文件内容指纹：size + 头尾 64KB 采样 sha256（小文件全量）。

    用途 = 检测"同路径被意外替换/覆盖"（重拍覆盖、陈旧产物顶替），非对抗性防伪——
    多 GB 视频全量 hash 成本不成比例，威胁模型里没有恶意构造碰撞的攻击者。
    algo 字段随采样策略升版，旧指纹自动失配 fail-closed。
    """
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256()
    with p.open("rb") as f:
        if size <= 2 * _CHUNK:
            h.update(f.read())
        else:
            h.update(f.read(_CHUNK))
            f.seek(-_CHUNK, os.SEEK_END)
            h.update(f.read(_CHUNK))
    return {"size": size, "quick_hash": h.hexdigest(), "algo": _FINGERPRINT_ALGO}


def state_path(out_dir: Path) -> Path:
    return Path(out_dir) / "state.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    """同目录唯一临时名 + os.replace：写一半崩溃/替换失败时原文件保持完好。

    临时名带 pid+序号（固定 .tmp 名会被并发写者互覆：A 写完暂停、B 覆盖同名 tmp、
    A replace 进 B 的 payload）；写入本身也在 try 内，写一半失败同样清理残骸。
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{next(_TMP_SEQ)}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _validate_state(data, p: Path) -> dict:
    """手写结构校验（jsonschema 是 dev-only extra，运行时不引入）。

    坏 state 显式报错（带路径 + 字段），不静默修补——静默 setdefault 会把
    真实损坏（半写/手改/版本漂移）洗成"看起来正常"的默认值。
    """
    def bad(msg: str):
        raise ValueError(f"[state] {p} 损坏：{msg}——不自动修补，请人工核对或删除该文件后重跑")

    if not isinstance(data, dict):
        bad("顶层不是 JSON 对象")
    if not isinstance(data.get("project"), str):
        bad("project 缺失或不是字符串")
    stages = data.get("stages")
    if not isinstance(stages, dict):
        bad("stages 缺失或不是对象")
    for s in STAGES:
        st = stages.get(s)
        if not isinstance(st, dict):
            bad(f"stages.{s} 缺失或不是对象")
        if st.get("status") not in _STATUS_ENUM:
            bad(f"stages.{s}.status={st.get('status')!r} 非法（须 ∈ {sorted(_STATUS_ENUM)}）")
        if not isinstance(st.get("verified"), bool):
            bad(f"stages.{s}.verified 缺失或不是布尔")
        if not isinstance(st.get("outputs"), list):
            bad(f"stages.{s}.outputs 缺失或不是数组")
        if "outputs_identity" in st and not isinstance(st["outputs_identity"], dict):
            bad(f"stages.{s}.outputs_identity 必须是对象")
    locks = data.get("locks")
    if not isinstance(locks, dict):
        bad("locks 缺失或不是对象")
    for k in LOCKS:
        v = locks.get(k, _MISSING)
        if v is _MISSING:
            bad(f"locks.{k} 缺失")
        if isinstance(v, bool):
            continue
        if isinstance(v, dict):
            missing = [f for f in _LOCK_RECORD_REQUIRED if f not in v]
            if missing:
                bad(f"locks.{k} 审批记录缺字段 {missing}")
            # 只查存在不查类型 = 伪造记录能过校验（Codex R3 F10）
            if not isinstance(v["artifact"], str) or not v["artifact"]:
                bad(f"locks.{k}.artifact 必须是非空字符串，实际 {v['artifact']!r}")
            if not isinstance(v["sha256"], str) or not _SHA256_HEX.match(v["sha256"]):
                bad(f"locks.{k}.sha256 必须是 64 位小写十六进制，实际 {v['sha256']!r}")
            if not isinstance(v["approved_at"], str) or not v["approved_at"]:
                bad(f"locks.{k}.approved_at 必须是时间字符串，实际 {v['approved_at']!r}")
            continue
        bad(f"locks.{k} 类型非法（须为布尔或审批记录对象），实际 {type(v).__name__}")
    return data


def load_state(out_dir: Path, project: str) -> dict:
    p = state_path(out_dir)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"[state] {p} 不是合法 JSON：{exc}——不自动修补，请人工核对或删除该文件后重跑"
            ) from exc
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"[state] {p} 不是合法 UTF-8：{exc}——不自动修补，请人工核对或删除该文件后重跑"
            ) from exc
        return _validate_state(data, p)
    return {
        "project": project,
        "stages": {s: {"status": "pending", "outputs": [], "verified": False} for s in STAGES},
        "locks": {k: False for k in LOCKS},
    }


def save_state(out_dir: Path, state: dict) -> None:
    # 写前也校验：非法 state 不得原子覆盖健康文件（load 校验挡不住内存里被改坏的 dict）
    _atomic_write_json(state_path(out_dir), _validate_state(state, state_path(out_dir)))


def _invalidate_downstream(st: dict, stage: str, reason: str) -> None:
    """stage 的产物基础变了（重跑 / 重新审批 / 撤销审批）→ 下游阶段与锁全部失效。"""
    for ds in DOWNSTREAM[stage]:
        if st["stages"][ds]["status"] != "pending":
            st["stages"][ds] = {"status": "pending", "outputs": [], "verified": False,
                                "note": reason}
    # 锁批的是"产出该产物的阶段"的旧产物 → 该阶段本身或其下游重跑时锁作废
    for lk, producer in LOCK_STAGE.items():
        if (producer == stage or producer in DOWNSTREAM[stage]) and st["locks"].get(lk):
            st["locks"][lk] = False


def mark_stage(out_dir: Path, project: str, stage: str, status: str, outputs: list[str], verified: bool, note: str = "") -> dict:
    st = load_state(out_dir, project)
    entry: dict = {"status": status, "outputs": outputs, "verified": verified, "note": note}
    if status == "done":
        # 完成时记录产物指纹：qa-export 选源时靠它证明"文件就是本轮产出的那份"。
        # outputs 里的路径先按调用方原样解释（绝对或 cwd 相对），裸文件名再回退 out_dir
        identity = {}
        for o in outputs:
            p = Path(o)
            if not p.exists() and not p.is_absolute():
                p = Path(out_dir) / p
            if p.exists():
                identity[p.name] = media_fingerprint(p)
        entry["outputs_identity"] = identity
    st["stages"][stage] = entry
    if status == "done":
        # 上游重跑 = 下游既有结果基于旧产物 → 全部失效回 pending（L4）
        _invalidate_downstream(st, stage, f"上游 {stage} 重跑，本阶段结果已失效，需重跑")
    save_state(out_dir, st)
    return st


def require_stage_output(out_dir: Path, project: str, stage: str, artifact: str) -> Path:
    """消费上游阶段产物前的门控：阶段本轮 done + verified + 文件就是当时那份。

    只看"文件存在"不够——上游重跑/重新审批会把产出阶段置回 pending，但旧文件
    还在盘上（Codex R3 P1-1：fine-cut 消费失效 rough_cut，qa-export 三重门反而
    认可基于旧剪点的新 fine）。legacy state 无 outputs_identity → fail-closed。
    """
    st = load_state(out_dir, project)
    entry = st["stages"].get(stage) or {}
    p = Path(out_dir) / artifact
    if entry.get("status") != "done":
        raise RuntimeError(
            f"[lineage] {artifact} 的产出阶段 {stage} 未完成本轮"
            f"（status={entry.get('status')!r}）——上游重跑/重新审批后须先重跑 {stage}，"
            f"盘上旧文件不可直接消费"
        )
    if entry.get("verified") is not True:
        raise RuntimeError(
            f"[lineage] 阶段 {stage} 的产物未经校验（verified != true）——"
            f"重跑 {stage} 使其通过校验后再消费 {artifact}"
        )
    ident = entry.get("outputs_identity")
    if not isinstance(ident, dict) or artifact not in ident:
        raise RuntimeError(
            f"[lineage] 阶段 {stage} 无 {artifact} 的产物指纹（legacy state 或产出时文件缺失）——"
            f"无法证明盘上文件由本轮产出，重跑 {stage} 补记指纹"
        )
    if not p.exists():
        raise RuntimeError(f"[lineage] {artifact} 不存在——先重跑 {stage}")
    if media_fingerprint(p) != ident[artifact]:
        raise RuntimeError(
            f"[lineage] {artifact} 与阶段 {stage} 完成时的指纹不符——"
            f"文件在阶段完成后被替换/改动过，重跑 {stage} 重新产出"
        )
    return p


def _source_media_surface(out_dir: Path, source_timeline) -> dict | None:
    """cut_lock 锁面并入源媒体内容身份：同路径换源（重拍覆盖）= 审批基础变了。

    路径按消费方（cut.py `Path(edl["source_timeline"])`）同一语义解释——绝对或
    cwd 相对，不额外猜 out_dir 相对，否则锁面和真正被剪的文件可能不是同一个。
    源缺失（外置盘未挂等）→ 锁定"缺失"这一事实本身；之后源出现或再消失都会破锁。
    """
    if not source_timeline:
        return None
    p = Path(source_timeline)
    if not p.exists():
        return {"missing": True}
    return media_fingerprint(p)


_UNPARSEABLE = object()  # 字节可 hash、内容解析失败的哨兵：先按指纹判"被改"，再谈内容


def _artifact_digest(out_dir: Path, lock: str) -> tuple[str, str, object]:
    """(artifact 相对路径, sha256, 校验用的那份内容)。cut_lock 只对人审决策面取指纹。

    内容与指纹出自同一次读盘——消费方用返回的内容而不是二次读文件，
    消掉"校验后、使用前"的 TOCTOU 替换窗口。
    整文件锁（transcript/srt）指纹取自原始字节：解析失败不在此处炸，content 置
    _UNPARSEABLE——require_lock 先按指纹报"人审后被改"（改成非法 JSON 也是改），
    set_lock 拒绝给解析不了的产物上锁。cut_lock 例外：决策面指纹必须先解析
    JSON，解析不了 = 无法核对审批面，直接报改坏。
    """
    artifact = LOCK_ARTIFACTS[lock]
    p = Path(out_dir) / artifact
    if not p.exists():
        raise RuntimeError(f"[lock] '{lock}' 无物可批：{artifact} 不存在，先产出产物再上锁")
    raw = p.read_bytes()
    if lock == "cut_lock":
        # rough_cut 成功后会回填 out_start（cut.py 机器派生字段）重写 edl.json，
        # 整文件 hash 会被这次回填误破——只对人审决策面（剪什么/留什么/剪的是哪份源）取指纹
        try:
            edl = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"[lock] {artifact} 不是合法 JSON（{exc}）——人审后被改坏/半写，"
                f"重新产出并重新人审：talkcut lock {lock} --out <dir>"
            ) from exc
        surface = {
            "keep_ranges": [{k: v for k, v in kr.items() if k != "out_start"}
                            for kr in edl.get("keep_ranges", [])],
            "cuts": edl.get("cuts", []),
            "splices": edl.get("splices", []),
            "broll_cover": edl.get("broll_cover", []),
            "human_locked": edl.get("human_locked"),
            "source_timeline": edl.get("source_timeline"),
            "source_media": _source_media_surface(out_dir, edl.get("source_timeline")),
        }
        payload = json.dumps(surface, sort_keys=True, ensure_ascii=False).encode("utf-8")
        content: object = edl
    elif artifact.endswith(".json"):
        payload = raw
        try:
            content = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            content = _UNPARSEABLE
    else:
        payload = raw
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = _UNPARSEABLE
    return artifact, hashlib.sha256(payload).hexdigest(), content


def require_lock(out_dir: Path, project: str, lock: str):
    """门控：锁未上 / 旧版布尔锁 / 记录被手改 / 产物在人审后被改 → 拒绝进入下一阶段。

    通过后返回校验过的那份产物内容（JSON → dict，SRT → str）——消费方直接用返回值，
    不再二次读盘。
    """
    st = load_state(out_dir, project)
    val = st.get("locks", {}).get(lock, False)
    if val is True:
        raise RuntimeError(
            f"[lock] '{lock}' 是旧版布尔锁，无法证明人审批的是哪份产物——fail-closed 拒绝。"
            f" 重新人审并重新上锁：talkcut lock {lock} --out <dir>"
        )
    if not val:
        raise RuntimeError(
            f"[lock] '{lock}' 未上锁——人审通过后才能进下一阶段。"
            f" 上锁：talkcut lock {lock} --out <dir>"
        )
    artifact, digest, content = _artifact_digest(out_dir, lock)
    if val.get("artifact") != artifact:
        raise RuntimeError(
            f"[lock] '{lock}' 审批记录 artifact={val.get('artifact')!r} 与锁定产物 {artifact} 不符——"
            f"记录被手改过，重新人审并重新上锁：talkcut lock {lock} --out <dir>"
        )
    if digest != val.get("sha256"):
        raise RuntimeError(
            f"[lock] '{lock}' 对应产物 {artifact} 在人审后被改过（hash 不符）——"
            f"重新人审并重新上锁：talkcut lock {lock} --out <dir>"
        )
    if content is _UNPARSEABLE:
        # 指纹居然对得上但内容解析不了 = 上锁时就是坏产物（不该发生，set_lock 已拒）
        raise RuntimeError(
            f"[lock] '{lock}' 锁定产物 {artifact} 内容无法解析——审批记录不可信，"
            f"重新产出并重新人审：talkcut lock {lock} --out <dir>"
        )
    if lock == "transcript_lock":
        # R4 round-2 P1-2：消费时同样过 gap 门控——R4 前上的旧锁（内容未变、hash 对
        # 得上）不能绕过"未解决 retranscribe 不得进下游"
        _require_gaps_resolved(content)
    return content


# schemas/transcript.schema.json 的 decision enum 全集：audit_gaps（transcribe.py）
# 自动写前两种；人审可把真静音条目改为 true_silence_keep/true_silence_trim（含证据）。
# 出现枚举外的值 = 手改/损坏。
_GAP_DECISIONS = {"retranscribe", "gemini_vision", "true_silence_keep", "true_silence_trim"}


def _require_gaps_resolved(transcript) -> None:
    """R4 Q8 + round-2 P1-3 + round-3：transcript_lock 门控——还有 `retranscribe`
    （疑似说话电平被漏转）未解决就不许锁/不许消费。其余三种 decision 不挡锁：
    `gemini_vision`（真静音待画面）属剪点决策面，paper_edit 已列候选；
    `true_silence_keep`/`true_silence_trim` 是人审后的终态。

    严格面：顶层非对象、gap_audit 缺失/null/非列表、条目非对象、decision 非法
    一律拒——这些都是"文件可疑损坏/被手改"，静默放行 = 假绿。round-3 收口：
    schema 从基线起就 required gap_audit，"旧产物缺字段"的兼容豁免不成立，取消。
    retranscribe 只能由非空白字符串 resolution 解锁（True/对象/空白串都不算人审说明）。"""
    if not isinstance(transcript, dict):
        raise RuntimeError(
            "[lock] transcript.json 顶层不是对象——文件可疑损坏，先修复产物再人审上锁"
        )
    gap_audit = transcript.get("gap_audit")
    if gap_audit is None:
        raise RuntimeError(
            "[lock] transcript.json 缺 gap_audit（或为 null）——schema 要求必填"
            "（字幕空白≠没内容的审计记录），先跑 gap 审计补齐该字段再人审上锁"
        )
    if not isinstance(gap_audit, list):
        raise RuntimeError(
            "[lock] transcript.json 的 gap_audit 不是列表——文件可疑损坏，"
            "先修复产物再人审上锁"
        )
    pending = []
    for i, g in enumerate(gap_audit):
        if not isinstance(g, dict):
            raise RuntimeError(
                f"[lock] gap_audit[{i}] 不是对象——文件可疑损坏，先修复产物再人审上锁"
            )
        decision = g.get("decision")
        if decision not in _GAP_DECISIONS:
            raise RuntimeError(
                f"[lock] gap_audit[{i}] 的 decision={decision!r} 非法"
                f"（只认 {sorted(_GAP_DECISIONS)}）——文件可疑损坏/被手改，"
                f"先修复产物再人审上锁"
            )
        if decision != "retranscribe":
            continue
        res = g.get("resolution")
        if not (isinstance(res, str) and res.strip()):
            pending.append(g)
    if pending:
        spans = "、".join(f"{g.get('start')}–{g.get('end')}s" for g in pending)
        raise RuntimeError(
            f"[lock] transcript_lock 拒绝：gap_audit 还有 {len(pending)} 段 retranscribe"
            f"（疑似说话电平被漏转）未解决：{spans}。两条出路：重转这些段；"
            f"或人工听审确认后在对应条目加 \"resolution\": \"<确认说明>\"（非空文字）再上锁"
        )


def set_lock(out_dir: Path, project: str, lock: str, value: bool = True) -> None:
    """上锁 = 写审批记录（产物路径 + 内容 hash + 时间）；解锁 = False。

    重新审批"已修改的产物"或撤销既有审批时，基于旧审批的下游阶段与锁全部失效
    （与 mark_stage 同一 DAG）；产物未变的重复上锁 = no-op，不误伤下游。
    """
    st = load_state(out_dir, project)
    prev = st["locks"].get(lock, False)
    if not value:
        if prev:
            _invalidate_downstream(st, LOCK_STAGE[lock],
                                   f"{lock} 审批被撤销，本阶段结果失去人审依据，需重审重跑")
        st["locks"][lock] = False
    else:
        artifact, digest, content = _artifact_digest(out_dir, lock)
        if content is _UNPARSEABLE:
            raise RuntimeError(
                f"[lock] {artifact} 内容无法解析（非法 JSON/编码）——不能对坏产物上锁，"
                f"先修复产物再人审"
            )
        if lock == "transcript_lock":
            _require_gaps_resolved(content)
        unchanged = (isinstance(prev, dict)
                     and prev.get("sha256") == digest and prev.get("artifact") == artifact)
        if prev and not unchanged:
            # 曾有审批（记录或 legacy 布尔）但产物已变/无法证明未变 → 旧审批产出的下游作废
            _invalidate_downstream(st, LOCK_STAGE[lock],
                                   f"{lock} 重新审批了已修改的产物，基于旧审批的结果已失效，需重跑")
        st["locks"][lock] = {
            "artifact": artifact,
            "sha256": digest,
            "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "approved_by": "human",
        }
    save_state(out_dir, st)

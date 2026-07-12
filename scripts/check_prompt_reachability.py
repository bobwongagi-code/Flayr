#!/usr/bin/env python3
"""Prompt 可达性 gate —— 防"孤儿/子集漂移"结构约束（孤儿病第三次复发后立，2026-06-19）。

病根：canonical doc（observation-guide / structure_library / …）该喂某一步，却因装配/抽取
环节被砍或漂移，使生效 prompt 与 doc 不一致；下游在残缺事实/规则上判断 = garbage-in。

本 gate 治两种失败模式，per-ITEM 而非 per-DOC（关键：per-doc 会因"镜头语言在场"放过"小窗缺席"）：
  - total-orphan：整份 doc 不到目标步（structure_library 曾不到阶段2、品被砍）。
  - subset-drift：doc 的部分必备项漂移缺席（observation-guide 内联副本缺 小窗/对齐/视角/焦点）。

强制性：main() 违规即 exit 1，须接进冻结/跑前路径 block，不靠人记得跑。
登记约束（堵自身盲区）：payload.py/prompt.py 里 read_*( references/*.md 或根 *.md ) 装载的每份 doc
  必须在 REGISTRY 或 BULK_OR_INTENTIONAL 里登记；新增 canonical doc 不登记 → coverage 检查 fail。
  （否则校验对新 doc 是盲的——正是它该抓的那类失效。）

waived：已知、已决定本轮不修的漂移项，显式记原因 → 降级为告警不 block（写进 gate 日志）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.payload import (  # noqa: E402
    build_llm_comparison_payload,
    build_video_fact_payload,
)


def _payload_text(payload: dict) -> str:
    """把一个 chat payload 的所有文本（system + user 各段）拼成可达面。"""
    out = []
    for msg in payload.get("messages", []):
        c = msg.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(seg.get("text", "") for seg in c if seg.get("type") == "text")
    return " ".join(out)


def stage1_text() -> str:
    """阶段1 事实抽取 payload 的可达面（最小合成 analysis，足够触发静态文本+装载）。"""
    analysis = {
        "product": {"name": "占位", "category": "占位品类"},
        "videos": {"creator": {"work_dir": "/nonexistent", "path": "/nonexistent"}},
    }
    return _payload_text(build_video_fact_payload("m", "creator", analysis, []))


def stage2_text() -> str:
    """阶段2 对比判断 payload 的可达面。"""
    return _payload_text(
        build_llm_comparison_payload("m", "## 产品信息\n占位", {}, {"product": {"name": "占位"}})
    )


# (doc → 目标步 → 必备项 → marker 同义词集)。bulk doc 用单一特征标记做 total-orphan 检查；
# per-item doc 逐观察/结构项做 subset-drift 检查。
REGISTRY: list[dict] = [
    {
        "doc": "references/observation-guide.md",
        "stage": "stage1",
        "items": {
            "段切(信息变化拐点/四轨)": ["变化点", "拐点", "转场"],
            "视觉主体": ["视觉主体", "主体"],
            "焦点位置": ["焦点"],
            "镜头语言/取景完整性": ["镜头语言", "歪斜", "只见局部", "完成"],
            "遮挡与UI危险区": ["遮挡", "UI", "购物车", "危险区"],
            "动作类型": ["动作类型", "静态展示", "前后对比"],
            "可读字幕": ["字幕"],
            "与口播对齐(同步/错位)": ["与口播", "声画", "对位", "错位"],
            "强调-字幕样式变化": ["高亮", "字号", "变色"],
            "强调-画中画/小窗": ["画中画", "小窗", "截图"],
            "强调-表情变化": ["表情"],
            "强调-特殊音效": ["音效"],
            "BGM在场/类型(事实)": ["BGM"],
            "拍摄视角/互动方式": ["视角", "第一人称", "纪实"],
        },
    },
    {
        "doc": "structure_library_full.md",
        "stage": "stage2",
        "items": {
            "模块类型(判断视图)": ["场景还原型", "价格锚定型"],
            "适配条件": ["适配品类", "适配购买动机"],
        },
    },
    {
        "doc": "references/commercial-judgement-framework.md",
        "stage": "stage2",
        "items": {"商业评判框架(整份)": ["核心标尺"]},
    },
    {
        "doc": "references/market-knowledge-my.md",
        "stage": "stage2",
        "items": {"目标市场知识(整份)": ["东南亚"]},
    },
    {
        "doc": "QA-RULES.md",
        "stage": "stage2",
        "items": {"输出自检契约(整份)": ["自检"]},
    },
]

# 已决定本轮不修、显式 waive 的漂移项（doc::item → 原因）。waive 的降级为告警、不 block。
WAIVED: dict[str, str] = {}

# 登记约束白名单：被代码装载但不做 item 级可达检查的 doc（翻译层等独立用途 / 仅人工参考）。
BULK_OR_INTENTIONAL: set[str] = {
    "references/commerce-translation-guidelines.md",  # 翻译步专用，非分析链
    "references/analysis-output-schema.json",          # 输出契约，字段经阶段2 指令转述
    "references/brand_propositions.json",              # 冻结品牌命题结构化数据，阶段2 运行时注入，不做 prompt 文档 item 级检查
    # 下列已在 REGISTRY 做 item 级检查，列此仅为登记可见：
    "QA-RULES.md",
    "structure_library_full.md",
    "references/observation-guide.md",
    "references/commercial-judgement-framework.md",
    "references/market-knowledge-my.md",
}


def run_item_checks() -> list[tuple[str, str, str]]:
    """逐项检查，返回违规 (doc, item, 'blocker'|'waived')。"""
    texts = {"stage1": stage1_text(), "stage2": stage2_text()}
    violations: list[tuple[str, str, str]] = []
    for entry in REGISTRY:
        text = texts.get(entry["stage"], "")
        for item, markers in entry["items"].items():
            if not any(m in text for m in markers):
                key = f"{entry['doc']}::{item}"
                violations.append((entry["doc"], item, "waived" if key in WAIVED else "blocker"))
    return violations


def run_coverage_check() -> list[str]:
    """登记约束：扫 payload.py/prompt.py 里装载的 doc，凡未登记的 canonical doc → 盲区违规。"""
    registered = {e["doc"] for e in REGISTRY} | BULK_OR_INTENTIONAL
    loaded: set[str] = set()
    for src in ("flayr_core/llm/payload.py", "flayr_core/prompt.py", "flayr_core/translation.py"):
        p = ROOT / "scripts" / src
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r'ROOT\s*/\s*(?:"([^"]+)"\s*/\s*)?"([^"]+\.(?:md|json))"', text):
            parts = [g for g in m.groups() if g]
            loaded.add("/".join(parts))
    return sorted(d for d in loaded if d not in registered)


def main() -> int:
    item_violations = run_item_checks()
    unregistered = run_coverage_check()

    print("═══ Prompt 可达性 gate ═══")
    blockers = [v for v in item_violations if v[2] == "blocker"]
    waived = [v for v in item_violations if v[2] == "waived"]

    if blockers:
        print(f"\n❌ BLOCKER 漂移（{len(blockers)} 项，必须冻前修或显式 waive）：")
        for doc, item, _ in blockers:
            print(f"   {doc} :: {item}")
    if waived:
        print(f"\n🟡 已 waive（{len(waived)} 项，记进 gate 日志）：")
        for doc, item, _ in waived:
            print(f"   {doc} :: {item} —— {WAIVED.get(f'{doc}::{item}', '')}")
    if unregistered:
        print(f"\n❌ 登记盲区（{len(unregistered)} 份 doc 被装载却未登记 REGISTRY/白名单）：")
        for d in unregistered:
            print(f"   {d}")
    if not blockers and not unregistered:
        print("\n✅ 通过：所有 canonical doc 必备项可达目标步，无登记盲区。")
        return 0
    print("\n→ gate 未过：修复 blocker / 登记新 doc 后再冻结或跑。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

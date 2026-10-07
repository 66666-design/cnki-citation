# -*- coding: utf-8 -*-
# cnki-citation — 知网(CNKI)引文格式批量导出 CLI：GB/T 7714 / EndNote / 知网研学
# Copyright (C) 2026 66666-design
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 检索功能见姊妹仓 cnki-search（https://github.com/66666-design/cnki-search）。
"""
用法:
  python cnki_citation.py cite 大语言模型                       # 搜索并导出全部引文
  python cnki_citation.py cite 大语言模型 --pages 2 --top 5 --format all
  python cnki_citation.py ids <exportId1> <exportId2>           # 按导出 ID 直接取引文

引文格式(--format): gbt=仅 GB/T 7714-2025（默认）; all=另附 EndNote+知网研学
字段(--field)/排序(--sort): 与 cnki-search 相同，cite 先检索再导出
退出码: 0 成功; 2 验证码未通过; 3 无结果; 1 其他错误
"""
import argparse
import json
import re
import sys
from pathlib import Path

from cnki_session import CNKI, FIELD_LABEL, SORT_CODES, CaptchaError


def clean_citation(text: str) -> str:
    """去掉弹窗文本里的 HTML 标签、多余空白与前导编号 [n]。"""
    t = re.sub(r"<[^>]+>", "", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^\[\d+\]\s*", "", t)


def multi_line(text: str) -> str:
    """EndNote/研学字段：<br> 转多行并缩进。"""
    t = re.sub(r"<br\s*/?>", "\n", text or "")
    return "\n    ".join(ln.strip() for ln in t.splitlines() if ln.strip())


def main():
    ap = argparse.ArgumentParser(description="知网引文格式批量导出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_c = sub.add_parser("cite", help="搜索并导出引文")
    p_c.add_argument("keyword")
    p_c.add_argument("--pages", type=int, default=1)
    p_c.add_argument("--top", type=int, default=0)
    p_c.add_argument("--field", choices=list(FIELD_LABEL), default="SU")
    p_c.add_argument("--sort", choices=list(SORT_CODES), default="time")
    p_c.add_argument("-o", "--out", default="", help="引文输出文件，默认 <关键词>-引文.txt")
    p_c.add_argument("--format", choices=["gbt", "all"], default="gbt")
    p_c.add_argument("--json", default="")

    p_i = sub.add_parser("ids", help="按导出加密 ID 直接取引文")
    p_i.add_argument("ids", nargs="+")
    p_i.add_argument("-o", "--out", default="")
    p_i.add_argument("--format", choices=["gbt", "all"], default="gbt")

    args = ap.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cnki = CNKI(state_file=Path(__file__).parent / "_cnki_state.json")
    try:
        if args.cmd == "ids":
            exports = cnki.export(args.ids)
            lines = []
            for i, (eid, ex) in enumerate(zip(args.ids, exports), 1):
                gbt = clean_citation(ex.get("GBTREFER", "")) or f"[失败: {ex.get('error')}]"
                lines.append(f"[{i}] {gbt}")
                if args.format == "all":
                    lines.append(f"    EndNote: {multi_line(ex.get('ENDNOTE',''))}")
                    lines.append(f"    研学:    {multi_line(ex.get('ELEARNING',''))}")
            text = "\n".join(lines) + "\n"
            print(text)
            out = Path(args.out) if args.out else None
            if out:
                out.write_text(text, encoding="utf-8")
            cnki.save_state()
            return 0

        rows = cnki.search(args.keyword, args.pages, args.field, args.sort)
        if not rows:
            print("搜索结果为空（0 条）。", file=sys.stderr)
            return 3
        if args.top > 0:
            rows = rows[:args.top]
        exports = cnki.export([r["exportId"] for r in rows])
        cnki.save_state()

        ok = 0
        lines = []
        for i, (row, ex) in enumerate(zip(rows, exports), 1):
            gbt = clean_citation(ex.get("GBTREFER", ""))
            if not gbt:
                gbt = f"[导出失败: {ex.get('error')}]"
            else:
                ok += 1
            lines.append(f"[{i}] {gbt}")
            if args.format == "all":
                lines.append(f"    EndNote: {multi_line(ex.get('ENDNOTE',''))}")
                lines.append(f"    研学:    {multi_line(ex.get('ELEARNING',''))}")
            lines.append(f"    {row['title']} | {row['journal']} | {row['date']} | 被引 {row['cited']}")
        out_path = Path(args.out) if args.out else Path(f"{args.keyword}-引文.txt")
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if args.json:
            Path(args.json).write_text(
                json.dumps([{**r, **ex} for r, ex in zip(rows, exports)],
                           ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"完成: {ok}/{len(rows)} 条引文已写入 {out_path}")
        return 0
    except CaptchaError as e:
        print(f"验证失败: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

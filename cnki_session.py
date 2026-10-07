# -*- coding: utf-8 -*-
# cnki-session — 知网(CNKI)会话层：搜索/导出接口 + WAF(AJ-Captcha)自动过验
# Copyright (C) 2026 66666-design
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 被 cnki-search 与 cnki-citation 共用。详见各自 README 与
# https://www.gnu.org/licenses/agpl-3.0.html 。
"""
cnki_session.py — 知网会话层（两仓库共用）

含: 带自动过验的 CNKI 会话、搜索(16字段/5排序/专业检索)、引文导出、
    WAF(AJ-Captcha blockPuzzle) 纯 Python 求解器。
依赖: pip install requests beautifulsoup4 numpy pillow pycryptodome
"""
import base64
import io
import json
import re
import time
import uuid
from pathlib import Path

import numpy as np
import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from PIL import Image

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SEARCH_PAGE = "https://kns.cnki.net/kns8s/search"
EXPERT_PAGE = "https://kns.cnki.net/kns8s/AdvSearch?type=expert"
GRID_URL = "https://kns.cnki.net/kns8s/brief/grid"
EXPORT_URL = "https://kns.cnki.net/dm8/API/GetExport"
VERIFY_HOME = "https://kns.cnki.net/verify/home"
CAPTCHA_GET = "https://kns.cnki.net/verify-api/get"
CAPTCHA_CHECK = "https://kns.cnki.net/verify-api/web/check"

# 检索字段（2026-10-08 从 kns8s 前端下拉实抓 data-val）
FIELD_LABEL = {
    "SU": "主题", "TKA": "篇关摘", "KY": "关键词", "TI": "篇名", "FT": "全文",
    "AU": "作者", "FI": "第一作者", "RP": "通讯作者", "AF": "作者单位",
    "FU": "基金", "AB": "摘要", "CO": "小标题", "RF": "参考文献",
    "CLC": "分类号", "LY": "文献来源", "DOI": "DOI",
}
# 排序方式（结果页排序条 data-sort 实抓；仅限 800 万条记录以内有效）
SORT_CODES = {
    "relevance": "FFD",   # 相关度
    "time": "PT",         # 发表时间（知网默认）
    "cited": "CF",        # 被引
    "download": "DFR",    # 下载
    "overall": "ZH",      # 综合
}


class CaptchaError(Exception):
    pass


# ---------------------------------------------------------------- WAF 求解器

def _aes_b64(plain: str, key: str) -> str:
    cipher = AES.new(key.encode(), AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(plain.encode(), 16))).decode()


def _js_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(v)


def _find_gap(bg_png: bytes, piece_png: bytes):
    """FFT SSD 模板匹配定位缺口。返回 (x, y, 置信统计)。"""
    bg = np.asarray(Image.open(io.BytesIO(bg_png)).convert("RGB"), dtype=np.float64)
    piece_img = Image.open(io.BytesIO(piece_png)).convert("RGBA")
    bbox = piece_img.split()[-1].getbbox() or (0, 0, *piece_img.size)
    piece_img = piece_img.crop(bbox)  # 块图是全高 PNG，裁到拼图块本身
    piece = np.asarray(piece_img, dtype=np.float64)
    mask = (piece[:, :, 3] > 128).astype(np.float64)
    ph, pw = mask.shape
    bh, bw = bg.shape[:2]

    def xcorr(img2d, kern2d):
        fh, fw = bh + ph, bw + pw
        f = np.fft.rfft2(img2d, (fh, fw))
        k = np.fft.rfft2(kern2d, (fh, fw))
        full = np.fft.irfft2(f * k, (fh, fw))
        return full[ph - 1:bh, pw - 1:bw]

    ssd = np.zeros((bh - ph + 1, bw - pw + 1))
    for c in range(3):
        ssd += xcorr(bg[:, :, c] ** 2, mask[::-1, ::-1])
        ssd -= 2 * xcorr(bg[:, :, c], (mask[:, :, None] * piece[:, :, :3])[::-1, ::-1, c])
    ssd += float((mask[:, :, None] * piece[:, :, :3] ** 2).sum())
    flat = ssd.ravel()
    i = int(np.argmin(flat))
    y, x = np.unravel_index(i, ssd.shape)
    order = np.partition(flat, min(3, flat.size - 1))
    return int(x), int(y), {"bg": (bh, bw), "best": float(flat[i]),
                            "second": float(order[1]), "median": float(np.median(flat))}


def _trigger_challenge(s: requests.Session, referer: str, max_tries: int = 12) -> dict:
    """打一次被门控的请求，等 WAF 发 -403；challenge 类型非滑块就重试。"""
    for _ in range(max_tries):
        r = s.post(GRID_URL, data={"boolSearch": "true", "pageNum": "1"},
                   headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"},
                   timeout=25)
        try:
            j = r.json()
        except Exception:
            time.sleep(1.0)
            continue
        if j.get("code") != -403:
            raise RuntimeError(f"取挑战失败: {str(j)[:120]}")
        m = re.search(r"captchaType=(\w+)&ident=(\w+)&captchaId=([\w-]+)&returnUrl=([^&\s\"]+)",
                      j.get("message", ""))
        if m and m.group(1) == "blockPuzzle":
            return {"ident": m.group(2), "captchaId": m.group(3), "returnUrl": m.group(4)}
        time.sleep(0.8)
    raise CaptchaError("未拿到滑块挑战（连续 clickWord 或无挑战）")


def _solve(s: requests.Session, referer: str, log=print, max_attempts: int = 6) -> bool:
    """过一次 WAF 验证。成功后 session 带放行 cookie。"""
    ch = _trigger_challenge(s, referer)
    s.get(VERIFY_HOME, params={"captchaType": "blockPuzzle", "ident": ch["ident"],
                               "captchaId": ch["captchaId"], "returnUrl": ch["returnUrl"]},
          timeout=25)
    for attempt in range(max_attempts):
        body = {"captchaType": "blockPuzzle", "clientUid": str(uuid.uuid4()),
                "ident": ch["ident"], "captchaId": ch["captchaId"],
                "ts": int(time.time() * 1000)}
        d = (s.post(CAPTCHA_GET, json=body, timeout=25).json() or {}).get("data") or {}
        if not d.get("originalImageBase64"):
            time.sleep(1)
            continue
        gx, gy, st = _find_gap(base64.b64decode(d["originalImageBase64"]),
                               base64.b64decode(d["jigsawImageBase64"]))
        bh, bw = st["bg"]
        x_val = 310.0 * gx / bw
        plain = '{"x":%s,"y":5}' % _js_num(x_val)
        point_json = (_aes_b64(plain, d["secretKey"]) if d.get("secretKey")
                      else d["token"] + "---" + plain)
        r = s.post(CAPTCHA_CHECK, json={
            "captchaType": "blockPuzzle", "pointJson": point_json,
            "token": d["token"], "ident": ch["ident"], "returnUrl": ch["returnUrl"]},
            timeout=25).json()
        if str(r.get("code")) == "0":
            ret = (r.get("data") or {}).get("returnUrl") or ch["returnUrl"]
            sep = "&" if "?" in ret else "?"
            s.get(f"{ret}{sep}captchaId={ch['captchaId']}", timeout=30, allow_redirects=True)
            log(f"  滑块通过（第 {attempt + 1} 次尝试, x={gx}）")
            return True
        time.sleep(0.8)  # 组件失败后 refresh 拿新图
    return False


class CNKI:
    """带 WAF 自动过验的知网会话。"""

    def __init__(self, state_file: Path = None, log=print):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        self.log = log
        self.state_file = Path(state_file) if state_file else None
        if self.state_file and self.state_file.exists():
            try:
                for k, v in json.loads(self.state_file.read_text(encoding="utf-8")).items():
                    self.s.cookies.set(k, v, domain=".cnki.net")
            except Exception:
                pass

    def save_state(self):
        if self.state_file:
            self.state_file.write_text(
                json.dumps({c.name: c.value for c in self.s.cookies}, ensure_ascii=False),
                encoding="utf-8")

    def _post_checked(self, url: str, **kw) -> requests.Response:
        """POST；-403 时自动解滑块重放（最多 3 轮）。其余响应原样返回，由调用方解释。"""
        for round_ in range(3):
            r = self.s.post(url, timeout=30, **kw)
            try:
                j = r.json()
            except Exception:
                return r
            if j.get("code") == -403:
                self.log("  触发 WAF 滑块，自动求解 ...")
                if not _solve(self.s, kw.get("headers", {}).get("Referer", SEARCH_PAGE),
                              log=self.log):
                    raise CaptchaError("滑块求解失败")
                continue
            return r
        raise CaptchaError("WAF 重试耗尽")

    # ------------------------------------------------------------- 搜索

    # 翻页时的固定规格（2026-10-07 从 SPA 实抓）
    PAGE2_PRODUCTS = "CJFQ,CAPJ,ZHYX,CJTL,CDFD,CMFD,WBFD,CPFD,IPFD,CCND,SCSF,SCHF,SCSD,SNAD,CCJD,CJFN,CCVD"

    def _search_common(self, qj: dict, aside: str, pages: int,
                       sort: str, search_from_url: str = SEARCH_PAGE) -> list:
        """搜索共用流程：首页 boolSearch=true，翻页带 turnpage 令牌。"""
        sort_code = SORT_CODES.get(sort, "PT")
        page1_sort = "" if sort == "time" else sort_code
        self.s.get(search_from_url, timeout=25)  # 预热拿 cookie
        rows = []
        turnpage = ""

        def fetch_page(pg: int):
            if pg == 1:
                form = {
                    "boolSearch": "true",
                    "QueryJson": json.dumps(qj, ensure_ascii=False, separators=(",", ":")),
                    "pageNum": "1", "pageSize": "20",
                    "sortField": page1_sort,
                    "sortType": "desc" if page1_sort else "",
                    "dstyle": "listmode",
                    "productStr": "", "aside": aside,
                    "searchFrom": "资源范围：总库",
                    "subject": "", "language": "", "uniplatform": "",
                    "CurPage": "1",
                }
            else:
                qj2 = dict(qj)
                qj2["Products"] = self.PAGE2_PRODUCTS
                qj2["SearchFrom"] = 4
                form = {
                    "boolSearch": "false",
                    "QueryJson": json.dumps(qj2, ensure_ascii=False, separators=(",", ":")),
                    "pageNum": str(pg), "pageSize": "20",
                    "sortField": page1_sort or "PT", "sortType": "desc",
                    "dstyle": "listmode",
                    "boolSortSearch": "false",
                    "productStr": "", "aside": "",
                    "searchFrom": "资源范围：总库",
                    "subject": "", "turnpage": turnpage,
                    "language": "", "uniplatform": "",
                }
            return self._post_checked(GRID_URL, data=form, headers={
                "Referer": search_from_url, "X-Requested-With": "XMLHttpRequest"})

        # 第 1 页带自愈：空结果可能是 WAF 软封（200+暂无数据），主动过验后重试
        page_rows = []
        for attempt in range(3):
            r = fetch_page(1)
            page_rows = self._parse_rows(r.text)
            if page_rows or "暂无数据" not in r.text:
                break
            self.log("  第 1 页空结果（疑似 WAF 软封），尝试过验后重试 ...")
            try:
                _solve(self.s, search_from_url, log=self.log)
            except (CaptchaError, RuntimeError):
                pass
            time.sleep(1)
        self.log(f"  第 1 页: {len(page_rows)} 条")
        if not page_rows:
            return []
        rows.extend(page_rows)
        m = re.search(r'id="hidTurnPage"[^>]*value="([^"]*)"', r.text)
        turnpage = m.group(1) if m else ""

        for pg in range(2, pages + 1):
            r = fetch_page(pg)
            page_rows = self._parse_rows(r.text)
            self.log(f"  第 {pg} 页: {len(page_rows)} 条")
            if not page_rows:
                break
            rows.extend(page_rows)
            time.sleep(0.6)
        for i, row in enumerate(rows, 1):
            row["n"] = i
        return rows

    def search(self, kw: str, pages: int = 1, field: str = "SU", sort: str = "time") -> list:
        label = FIELD_LABEL.get(field, "主题")
        qj = {
            "Platform": "", "Resource": "CROSSDB", "Classid": "WD0FTY92", "Products": "",
            "QNode": {"QGroup": [{"Key": "Subject", "Title": "", "Logic": 0, "Items": [
                {"Field": field, "Value": kw, "Operator": "TOPRANK", "Logic": 0,
                 "Vector": "", "Title": label}],
                "ChildItems": []}]},
            "ExScope": 1, "SimpTrad": "0", "SearchType": 2, "Rlang": "CHINESE",
            "KuaKuCode": "YSTT4HG0,LSTPFY1C,EMRPGLPA,JUP3MUPD,MPMFIG1A,WQ0UVIAA,"
                         "BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R",
            "Expands": {}, "View": "changeDBCh", "SearchFrom": 1,
        }
        return self._search_common(qj, f"({label}：{kw})", pages, sort)

    def expert_search(self, expr: str, pages: int = 1, sort: str = "time") -> list:
        """专业检索：知网检索表达式透传，如 TI='知识图谱' AND AU='刘峤'。"""
        qj = {
            "Platform": "", "Resource": "CROSSDB", "Classid": "WD0FTY92", "Products": "",
            "QNode": {"QGroup": [
                {"Key": "Subject", "Title": "", "Logic": 0, "Items": [
                    {"Key": "Expert", "Title": "", "Logic": 0, "Field": "EXPERT",
                     "Operator": 0, "Value": expr, "Value2": "", "options": {}}],
                 "ChildItems": []},
                {"Key": "ControlGroup", "Title": "", "Logic": 0, "Items": [],
                 "ChildItems": []}]},
            "ExScope": "1", "SimpTrad": "0", "SearchType": 4, "Rlang": "CHINESE",
            "KuaKuCode": "YSTT4HG0,LSTPFY1C,EMRPGLPA,JUP3MUPD,MPMFIG1A,WQ0UVIAA,"
                         "BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R",
            "Expands": {}, "View": "changeDBCh", "SearchFrom": 1,
        }
        return self._search_common(qj, f"({expr})", pages, sort,
                                   search_from_url=EXPERT_PAGE)

    @staticmethod
    def _parse_rows(html: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one(".result-table-list")
        if not table:
            return []
        out = []
        for tr in table.select("tbody tr"):
            a = tr.select_one("td.name a.fz14")
            if not a:
                continue
            cb = tr.select_one("input.cbItem")
            cell = lambda sel: (tr.select_one(sel).get_text(strip=True)
                                if tr.select_one(sel) else "")
            out.append({
                "title": a.get_text(strip=True),
                "href": a.get("href", ""),
                "exportId": (cb.get("value", "") if cb else ""),
                "authors": "; ".join(x.get_text(strip=True) for x in tr.select("td.author a")),
                "journal": cell("td.source"),
                "date": cell("td.date"),
                "cited": cell("td.quote"),
                "db": cell("td.data"),
            })
        return out

    # ------------------------------------------------------------- 引文导出

    def export(self, export_ids: list) -> list:
        out = []
        for eid in export_ids:
            if not eid:
                out.append({"error": "empty exportId"})
                continue
            try:
                r = self._post_checked(EXPORT_URL, data={
                    "filename": eid, "displaymode": "GBTREFER,elearning,EndNote",
                    "uniplatform": "NZKPT"},
                    headers={"Referer": SEARCH_PAGE, "X-Requested-With": "XMLHttpRequest"})
                j = r.json()
            except CaptchaError:
                out.append({"error": "captcha exhausted"})
                continue
            except Exception as e:
                out.append({"error": str(e)[:120]})
                continue
            item = {}
            if str(j.get("code")) == "1" or j.get("success"):
                for d in (j.get("data") or []):
                    val = d.get("value") or [""]
                    item[str(d.get("mode", "")).upper()] = val[0] if isinstance(val, list) else val
            else:
                item = {"error": str(j.get("message", j))[:150]}
            out.append(item)
            time.sleep(0.35)
        return out


def clean_citation(text: str) -> str:
    """去掉弹窗文本里的 HTML 标签、多余空白与前导编号 [n]。"""
    t = re.sub(r"<[^>]+>", "", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^\[\d+\]\s*", "", t)


def multi_line(text: str) -> str:
    """EndNote/研学字段：<br> 转多行并缩进。"""
    t = re.sub(r"<br\s*/?>", "\n", text or "")
    return "\n    ".join(ln.strip() for ln in t.splitlines() if ln.strip())



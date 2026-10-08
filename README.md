# cnki-citation — 知网引文格式批量导出（GB/T 7714 / EndNote / 知网研学）

> CNKI citation export CLI: batch GB/T 7714-2025 / EndNote / e-learning
> citations. Pure Python, built-in WAF solver, no browser needed.

> 论文检索（16 字段 / 5 排序 / 专业检索表达式）见姊妹仓
> **[cnki-search](https://github.com/66666-design/cnki-search)**。

## 安装

```bash
pip install requests beautifulsoup4 numpy pillow pycryptodome
# 同目录需要会话层：
#   git clone https://github.com/66666-design/cnki-session
```

`cnki_citation.py` 与 `cnki_session.py` 放同一目录即可运行。

## 用法

```bash
# 搜索并导出全部引文（GB/T 7714），写入 大语言模型-引文.txt
python cnki_citation.py cite 大语言模型

# 搜 2 页、只取前 5 条、附 EndNote+研学格式、另存结构化 JSON
python cnki_citation.py cite 大语言模型 --pages 2 --top 5 --format all --json out.json

# 按作者搜、按被引降序再导出（字段/排序与 cnki-search 相同）
python cnki_citation.py cite 知识图谱 --field AU --sort cited

# 已有导出加密 ID 时直接取引文
python cnki_citation.py ids <exportId1> <exportId2>

# 页码范围：从第 2 页起取 1 页再导出引文
python cnki_citation.py cite 大语言模型 --start-page 2 --pages 1
```

退出码：0 成功；2 验证码未通过；3 无结果；1 其他错误。

## 引文格式（`--format`）

知网 `GetExport` 接口当前只开放三种（2026-10-08 逐值实测，Refworks /
NoteExpress 等均返回"暂无数据"）：

| mode | 名称 | 说明 |
|---|---|---|
| `GBTREFER` | **GB/T 7714-2025 格式引文** | 国标参考文献格式，中文学术界标准 |
| `ELEARNING` | **知网研学（原 E-Study）** | 知网自家结构化引文（题名/作者/单位逐行） |
| `ENDNOTE` | **EndNote** | `%A/%T/%J` 标签格式，文献管理软件通用 |

`gbt`=仅输出 GB/T 7714（默认）；`all`=三种全附。GB/T 文本已去掉弹窗里的
HTML 标签与前导编号 `[n]`。注意：接口要求三个 mode 一起传（单独传一个会被
静默拒绝），工具已处理。

## 工作原理

- 引文：`POST https://kns.cnki.net/dm8/API/GetExport`，
  `filename=<导出加密ID>&displaymode=GBTREFER,elearning,EndNote&uniplatform=NZKPT`。
  该 ID 就是搜索结果行 `input.cbItem` 的 value，与网页「引用」弹窗同源。
- WAF 过验全自动（详见 [cnki-session](https://github.com/66666-design/cnki-session)
  README）。
- 会话缓存：`_cnki_state.json`（已 gitignore）。

## 隐私与合规

- `_cnki_state.json` 缓存知网会话 cookie，只存在于本机，请勿分享。
- 只读取公开可浏览的引文格式，不下载全文，不绕过付费权限。

## License

Copyright (C) 2026 66666-design · **AGPL-3.0-or-later**（同 [cnki-search](https://github.com/66666-design/cnki-search)、[cnki-session](https://github.com/66666-design/cnki-session)）。
任何人修改后分发或部署为网络服务，须以同协议提供完整源码。
详见 [LICENSE](LICENSE) 或 https://www.gnu.org/licenses/agpl-3.0.html 。

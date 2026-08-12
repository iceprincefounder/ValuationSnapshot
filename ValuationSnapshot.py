"""
从 stockanalysis.com 获取数据，直接读取列表中公司的 EV/EBIT 及其他关键指标。

设计原则：
    * 不做本地计算、不做兜底推算，所有数值直接来自网站返回的原始 JSON / HTML。
    * 如果网站没提供某个字段，就显示 N/A，绝不用其他公式凑出来。

数据源：
    美股 statistics 页：https://stockanalysis.com/stocks/{ticker}/statistics/
    港股 statistics 页：https://stockanalysis.com/quote/hkg/{code}/statistics/
    统计页里内嵌 JSON（形如 {id:"evEbit",title:"EV / EBIT",value:"29.04",hover:"29.04"}），
    直接正则抽取即可。hover 字段是未缩写的精确值，优先使用。

    收盘价则从对应市场的主页 HTML 顶部大号价格块抓：
        <div class="text-4xl font-bold ...">数值</div>
    港股页面下方还带一段 "Aug 7, 2026, 11:55 AM HKT" 作为价格时间戳。

    ETF 快照页：https://stockanalysis.com/etf/{ticker}/  (peRatio / aum 等)
    ETF 历史 P/E 曲线走"指数代理", 因 ETF 组合层面无公开时序:
        SPYM -> S&P 500        https://www.multpl.com/s-p-500-pe-ratio/table/by-month  (月频, 50+ 年)
        QQQM -> Nasdaq 100     https://siblisresearch.com/data/nasdaq-100-pe-ratio/    (季频, 免费部分 ~10 期)
        VUG  -> CRSP US LC Growth: 无公开历史 P/E 源, 曲线保持 N/A
    这些曲线严格上是"指数 P/E"而非"ETF 组合 P/E", 前端会在图表副标题里明确标注每个 ticker 的数据源。

依赖：仅标准库（urllib + re），无需第三方包。

------------------------------------------------------------------------------
外部凭证 / API Keys （维护须知）
------------------------------------------------------------------------------
本脚本会从环境变量读取以下第三方 API 凭证。**任何 key 都不允许硬编码进源码或
提交进仓库**——本仓库带 GitHub Pages 是公开的, key 一旦入库几分钟内会被自动扫描
盗刷。

    FMP_API_KEY   -- Financial Modeling Prep (financialmodelingprep.com)
                     用途: 抓取 DCF 公允价值 / WACC / 增长率等估值模型输出。
                     免费额度: 250 次/天, 仅支持个股, ETF 不支持 (返回 N/A)。
                     缺失时脚本不报错, 相关字段留空。

存放位置约定:
    - 本地开发: 用户环境变量 (PowerShell:
        [Environment]::SetEnvironmentVariable("FMP_API_KEY","xxx","User")
      )
    - GitHub Actions (周度快照 workflow): 已在仓库
        Settings -> Secrets and variables -> Actions
      里配置为 repository secret, workflow YAML 通过
        env:
          FMP_API_KEY: ${{ secrets.FMP_API_KEY }}
      注入到脚本进程。
    - 若 key 疑似泄漏, 立即到 FMP 后台 rotate 一次, 并同步更新 GitHub secret。

新增其它第三方数据源时, 请沿用同一套 "环境变量 + GitHub secret" 模式, 并在
本注释块中登记, 保持 key 治理的单一入口。
------------------------------------------------------------------------------
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field


# ---------- 股票列表 ----------

USStocks = [
    "AAPL",
    "MSFT",
    "GOOGL",   # Alphabet：stockanalysis 已提供公司整体 EV/EBIT，用 GOOGL 或 GOOG 都一样
    "AMZN",
    "META",
    "BRK.B",   # Berkshire Hathaway Class B (stockanalysis URL 路径为 /stocks/brk.b/)
    "KO",      # Coca-Cola
    "PG",      # Procter & Gamble
    "JNJ",     # Johnson & Johnson
    "UNH",     # UnitedHealth Group
]

# 港股用 4 位交易所代码。0700=腾讯控股，1810=小米集团-W
HKStocks = [
    "0700",    # Tencent
    "1810",    # Xiaomi
    "9988",    # Alibaba Group
    "9618",    # JD.com
    "9992",    # Pop Mart International
    "1211",    # BYD Company
    "9633",    # Nongfu Spring (农夫山泉)
    # "3750",  # CATL - Contemporary Amperex Technology (H-shares, listed May 2025)
               #        stockanalysis 目前只有 overview 页, statistics/financials/ratios 全部 404,
               #        暂无法抓取估值字段, 待数据源上线后启用。
]

# ETF 走独立的 /etf/{t}/ 主页, 主页 HTML 里内嵌一段 JSON 汇总了 ETF 层面的关键字段
# (peRatio / aum / nav / expenseRatio / dividendYield / sharesOut / dps / beta 等)。
# 我们把 peRatio 填入 PE 列, aum 填入 MarketCap 列; 其余对 ETF 语义上不适用的字段
# (EV/EBIT, EV/EBITDA, PEG, EBIT, Debt, Cash) 一律保留为 None -> 前端显示 "-"。
#
# Forward P/E ("PE Fwd" 列) 走各自的**发行商官方**基金 characteristics 数据源:
#   SPYM -> SSGA 官网 SPYM SSR HTML (weighted harmonic FY1, FactSet Estimates, 月频)
#   QQQM -> Invesco dng-api JSON      (weighted harmonic forward P/E, 日频)
#   VUG  -> Vanguard characteristics endpoint 未公开加权前瞻 P/E, 保持 None
#
# 历史 P/E 曲线走"指数代理": ETF 组合层面无公开时序, 因此改抓 ETF 所跟踪指数的历史 P/E。
#   SPYM -> S&P 500        (multpl.com, 月频, 50+ 年)
#   QQQM -> Nasdaq 100     (siblisresearch.com, 季频, 免费部分 ~10 期)
#   VUG  -> CRSP US LC Growth: 无公开源, 曲线保持 N/A
# EV/EBIT 时序对 ETF 一律不适用, 保持 N/A。
ETFs = [
    "VUG",     # Vanguard Growth ETF          (CRSP US LC Growth - no public PE history)
    "QQQM",    # Invesco NASDAQ-100 ETF       (Nasdaq 100 index proxy)
    "SPYM",    # SPDR Portfolio S&P 500 ETF   (S&P 500 index proxy)
]

# ETF -> 追踪指数的历史 P/E 数据源。None 表示无公开源。
# key = ETF ticker (upper), value = (label, url, parser_name)
# label 会显示在前端图表副标题里作为数据源署名; parser_name 用于 _fetch_etf 分发解析器。
ETF_PE_SOURCE: dict[str, tuple[str, str, str] | None] = {
    "SPYM": ("S&P 500 Index P/E (TTM) · multpl.com",
             "https://www.multpl.com/s-p-500-pe-ratio/table/by-month",
             "multpl_spx"),
    "QQQM": ("Nasdaq 100 Index P/E (TTM) · siblisresearch.com",
             "https://siblisresearch.com/data/nasdaq-100-pe-ratio/",
             "siblis_ndx"),
    "VUG":  None,   # CRSP US Large Cap Growth: no public historical PE source
}

# ETF -> 当前 Forward P/E 数据源 (发行商官方最新月度/日度公布值)。None 表示未开放。
# key = ETF ticker (upper), value = (label, url_or_cusip, parser_name)
# label 显示在表格 P/E (Fwd) 单元格的 tooltip 里, 明确"此前瞻 P/E 出自发行商"。
ETF_FWD_PE_SOURCE: dict[str, tuple[str, str, str] | None] = {
    "SPYM": ("SSGA · Price/Earnings Ratio FY1 (FactSet Estimates, weighted harmonic)",
             "https://www.ssga.com/us/en/individual/etfs/spdr-portfolio-sp-500-etf-spym",
             "ssga_html"),
    "QQQM": ("Invesco dng-api · forwardPriceToEarningsRatio (weighted harmonic)",
             "46138G649",   # QQQM CUSIP; API 通过 idType=cusip 查询
             "invesco_api"),
    "VUG":  None,   # Vanguard characteristics endpoint 未开放加权前瞻 P/E
}

# ETF -> Vanguard characteristic API 数据源。Vanguard 官方前端 (investor.vanguard.com)
# 唯一公布的组合层面口径, 只提供 TTM P/E + EPS growth + ROE (无 forward P/E)。
# 这里独立于 ETF_FWD_PE_SOURCE 是因为 SSGA / Invesco 提供 forward, Vanguard 只有 TTM,
# 分开管理可以避免语义混淆。key = ETF ticker (upper), value = (label, ticker_for_api)。
ETF_VANGUARD_SOURCE: dict[str, tuple[str, str] | None] = {
    "VUG":  ("Vanguard · Portfolio characteristics (Morningstar equity, monthly)",
             "vug"),
    # SPYM (SPDR S&P 500) 与 VOO (Vanguard S&P 500) 追踪同一指数 (S&P 500) 且均为
    # 全复制策略, 成分股 + 权重差异 < 0.05%, 组合层面加权 ROE 数值应基本一致。
    # SSGA 官方页面 (含 Fund + Index Characteristics 两个面板) 不发布 ROE 字段,
    # 这里用 VOO 的 Vanguard characteristic API 数据作为代理回填 SPYM 的 ROE 列;
    # 由回填逻辑保证仅在原字段为空时写入, 不会覆盖 SSGA 原生 EPS Growth (前瞻口径,
    # 与 Vanguard 的 TTM 口径不同)。VUG 追踪 CRSP US Large-Cap Growth, 与任何
    # S&P 500 ETF 都不同源, 保持独立。
    "SPYM": ("Vanguard · VOO Portfolio characteristics (S&P 500 proxy)",
             "voo"),
}

# ---------- 公司 logo 域名映射 ----------
# 每个域名对应的 favicon 已在脚本运行时抓取并缓存到 ``Pages/logos/<domain>.png``,
# 图标随 commit 一同上传到 GitHub Pages. 前端 <img> 直接引用相对路径:
#   src="../logos/<domain>.png"  (报告位于 Pages/<YYYY>/ 下, 用 ../ 跳出年份目录)
# 优点: 不依赖任何第三方 favicon 服务, 国内外读者都能秒开; 图标即使原始来源域名将来
# 变更, 历史快照依旧显示当时的 logo. 抓取源见 _LOGO_SOURCES (Google -> DDG).
LOGO_DOMAIN: dict[str, str] = {
    # US
    "AAPL":  "apple.com",
    "MSFT":  "microsoft.com",
    "GOOGL": "abc.xyz",
    "AMZN":  "amazon.com",
    "META":  "meta.com",
    "BRK.B": "berkshirehathaway.com",
    "KO":    "coca-colacompany.com",
    "PG":    "pg.com",
    "JNJ":   "jnj.com",
    "UNH":   "unitedhealthgroup.com",
    # HK
    "0700":  "tencent.com",
    "1810":  "mi.com",
    "9988":  "alibabagroup.com",
    "9618":  "jd.com",
    "9992":  "popmart.com",
    "1211":  "byd.com",
    "9633":  "nongfuspring.com",
    # ETFs
    "VUG":   "vanguard.com",
    "QQQM":  "invesco.com",
    "SPYM":  "ssga.com",
}


# ---------- 页面字段映射 ----------

# stockanalysis.com 页面里的字段 id -> 我们要的列
#
# 说明:
#   epsGrowth3To5Y / returnOnEquity 是 ETF 专用列 (个股不填):
#     - SPYM: SSGA HTML 提供 "Est. 3-5 Year EPS Growth", 无 ROE
#     - QQQM: Invesco API 提供 returnOnEquity, 无 EPS Growth
#     - VUG : 两者皆无
#   在 render_section_html 里 ETF section 只展示 [PE, PE Fwd, EPS Growth, ROE] 四列,
#   其它列 (MarketCap/EV/Debt/Cash/EBIT/EV·EBIT/EV·EBITDA/PEG) 对 ETF 语义上不成立, 直接不渲染。
FIELDS = {
    "marketcap":         "MarketCap",
    "enterpriseValue":   "EV",
    "debt":              "Debt",
    "totalcash":         "Cash&STI",
    "ebit":              "EBIT(TTM)",
    "evEbit":            "EV/EBIT",
    "evEbitda":          "EV/EBITDA",
    "pe":                "PE",
    "peForward":         "PE Fwd",
    "pegRatio":          "PEG",
    "epsGrowth3To5Y":    "EPS Growth (3-5Y Est)",
    "returnOnEquity":    "ROE",
}

# 独立于 FIELDS 的"收盘价"与"时间"列
CLOSE_COL = "Close"
CLOSE_DATE_COL = "AsOf"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


@dataclass
class Row:
    symbol: str
    data: dict           # key = 列名, value = 原始字符串或 None
    # 5Y = 20 个季度 TTM 点（?p=trailing）
    pe_history: list[tuple[str, float]] | None = None
    ev_ebit_history: list[tuple[str, float]] | None = None
    # 3Y = 最近 12 个季度 TTM 点（从 5Y 数组尾部切片）
    pe_history_3y: list[tuple[str, float]] | None = None
    ev_ebit_history_3y: list[tuple[str, float]] | None = None
    # 1Y = 约 52 个周频点（本地反算，周收盘价 / 当前 TTM EPS 及 EBIT）
    pe_history_1y: list[tuple[str, float]] | None = None
    ev_ebit_history_1y: list[tuple[str, float]] | None = None
    # 该 ticker 的 P/E 曲线数据源署名（个股为 "stockanalysis.com"; ETF 为指数代理源；
    # None 表示无历史序列 -> 前端不显示单独数据源标注）
    pe_history_source: str | None = None
    # 该 ticker 的 Forward P/E 数据源署名（仅 ETF 使用, 显示在表格 P/E (Fwd) 单元格 tooltip 里；
    # 个股由 stockanalysis statistics 页直接给出, 不额外标注, 保持 None）
    pe_forward_source: str | None = None
    # 以下三项是"表格单元格级"的数据源署名, 用于给对应列的 <td> 添加 title tooltip。
    # 与 pe_history_source (曲线数据源) / pe_forward_source (前瞻 PE 数据源) 平级独立:
    #   - pe_ttm_source        : P/E (TTM) 单元格的数据源 (仅 ETF 标注, 走 stockanalysis /etf/{t}/;
    #                            个股不标注, 口径统一走 stockanalysis statistics 页, 无需重复署名)
    #   - eps_growth_source    : EPS Growth (3-5Y Est) 单元格的数据源 (仅 ETF, 个股无此列)
    #   - roe_source           : ROE 单元格的数据源 (仅 ETF, 个股无此列)
    pe_ttm_source: str | None = None
    eps_growth_source: str | None = None
    roe_source: str | None = None
    # DCF Tab 使用的估值信息(仅个股, 快照生成时抓取; ETF 无组合层面 DCF 概念, 保持 None)。
    # 融合两个独立的公开数据源, 前端在 DCF Tab 里以并列双卡片形式展现, 且明确标注来源。
    # ------------------------------------------------------------------
    # 数据源 A: financialmodelingprep.com  (FMP `/stable/discounted-cash-flow`)
    #   免费 tier 只返回一个"内部黑盒 DCF" 数字, 没有分年 FCF / WACC / g 明细;
    #   FMP legacy `advanced_discounted_cash_flow` 自 2025-08-31 起对新用户 403,
    #   新用户无法拿到 5 年逐年 FCF, 因此这里退化为单值展示。
    # 数据源 B: stockanalysis.com  (`/stocks/{t}/forecast/` 页)
    #   免费拿到分析师共识价目标 (avg / low / high)、评级分布、今明两年 EPS/Revenue,
    #   分年 DCF / Fair Value 数值是付费墙 ("Upgrade"), 无法免费获取。
    # ------------------------------------------------------------------
    # 存整体 dict, 结构见 `_fetch_dcf_snapshot()` / `_fetch_sa_forecast()` 的返回值:
    #   {
    #     "fmp": {              # None 表示 FMP 分支缺失 (无 key / 网络失败 / ticker 不支持)
    #       "fair_value":   float | None,  # /stable/discounted-cash-flow 返回的 dcf
    #       "fair_value_l": float | None,  # /stable/levered-discounted-cash-flow (含资本结构)
    #       "price":        float | None,  # FMP 端点同批次返回的 Stock Price
    #       "asof":         str | None,    # FMP 返回的 date
    #       "source_url":   str,           # 用于卡片脚注可点击的说明链接
    #     } | None,
    #     "sa": {               # None 表示 stockanalysis forecast 页抓取/解析失败
    #       "target_avg":   float | None,  # 分析师共识目标价 (平均)
    #       "target_low":   float | None,
    #       "target_high":  float | None,
    #       "target_upside": float | None, # 百分比, 站点直接给出
    #       "consensus":    str | None,    # "Buy" / "Hold" / "Sell" 等
    #       "num_analysts": int | None,    # 参与调查的分析师数
    #       "rev_this":     float | None,  # 本财年 Revenue 预测
    #       "rev_next":     float | None,  # 下一财年 Revenue 预测
    #       "eps_this":     float | None,
    #       "eps_next":     float | None,
    #       "source_url":   str,           # forecast 页链接
    #     } | None,
    #     "currency": str | None,   # 报价币种 (USD/HKD)
    #   }
    dcf: dict | None = None


# ---------- URL 构造 ----------

def _url_home(ticker: str, market: str) -> str:
    """主页 URL（用于抓当前/最新收盘价）。"""
    if market == "US":
        return f"https://stockanalysis.com/stocks/{ticker.lower()}/"
    if market == "HK":
        return f"https://stockanalysis.com/quote/hkg/{ticker}/"
    if market == "ETF":
        return f"https://stockanalysis.com/etf/{ticker.lower()}/"
    raise ValueError(f"unknown market: {market}")


def _url_stats(ticker: str, market: str) -> str:
    """statistics 页 URL（用于抓财务指标）。"""
    if market == "US":
        return f"https://stockanalysis.com/stocks/{ticker.lower()}/statistics/"
    if market == "HK":
        return f"https://stockanalysis.com/quote/hkg/{ticker}/statistics/"
    raise ValueError(f"unknown market: {market}")


def _url_ratios(ticker: str, market: str, period: str = "trailing") -> str:
    """财务比率页 URL（含 TTM PE / EV-EBIT 历史数组）。

    period:
      - "trailing"  → 近 20 个季度 TTM（约 5 年，每季一点）
    """
    if market == "US":
        return f"https://stockanalysis.com/stocks/{ticker.lower()}/financials/ratios/?p={period}"
    if market == "HK":
        return f"https://stockanalysis.com/quote/hkg/{ticker}/financials/ratios/?p={period}"
    raise ValueError(f"unknown market: {market}")


def _url_income_quarterly(ticker: str, market: str) -> str:
    """季度损益表页 URL，含 `datekey`+`epsdil`+`opinc` 20 期季度数组。

    用于按“报告日”对齐每周股价，得到严谨的 TTM 序列（4 季滚动求和）。
    """
    if market == "US":
        return f"https://stockanalysis.com/stocks/{ticker.lower()}/financials/?p=quarterly"
    if market == "HK":
        return f"https://stockanalysis.com/quote/hkg/{ticker}/financials/?p=quarterly"
    raise ValueError(f"unknown market: {market}")


def _url_history_weekly(ticker: str, market: str) -> str:
    """stockanalysis 官方历史股价 API，返回近 1 年周频 K 线（约 52 个点）。

    - US : /api/symbol/s/{ticker}/history
    - HK : /api/symbol/q/hkg-{code}/history
    """
    if market == "US":
        return (
            f"https://api.stockanalysis.com/api/symbol/s/{ticker.lower()}/history"
            f"?type=stock&range=1Y&period=Weekly"
        )
    if market == "HK":
        return (
            f"https://api.stockanalysis.com/api/symbol/q/hkg-{ticker}/history"
            f"?type=stock&range=1Y&period=Weekly"
        )
    raise ValueError(f"unknown market: {market}")


# ---------- HTTP ----------

def _get(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"  !! GET {url} failed: {e}", file=sys.stderr)
        return None


# ---------- 公司 logo 本地缓存 ----------
# 图标存放位置: Pages/logos/<domain>.png
# 缓存策略  : 已存在的文件不重复抓取 (favicon 不常变动); 抓取失败静默跳过, 前端 <img>
#            的 onerror 自动隐藏, 不影响页面其他部分。
# 抓取源    : Google s2 (高清 64px, 覆盖率最广) -> DuckDuckGo ip3 (备用, 覆盖非主流域名)。
#            两者都在境外, GitHub Actions runner 均可访问; 本地开发环境如果被墙, 会
#            退回到 "有多少缓存就用多少" 的降级模式, 不阻塞报告生成。

_LOGO_SOURCES: tuple[str, ...] = (
    "https://www.google.com/s2/favicons?domain={d}&sz=64",
    "https://icons.duckduckgo.com/ip3/{d}.ico",
)


def _fetch_favicon_bytes(domain: str) -> bytes | None:
    """依次尝试各个 favicon 源, 返回首个成功的字节流 (期望 png/ico), 全部失败返回 None。

    仅返回体积在 [200 B, 200 KB] 内且看起来是图片的数据 (魔数校验), 过滤掉 Google
    的 "1x1 透明占位图" (~120 B) 或 HTML 报错页。
    """
    import urllib.request
    for tpl in _LOGO_SOURCES:
        url = tpl.format(d=domain)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
        except Exception as e:
            print(f"  .. favicon {domain} <- {url} failed: {e}", file=sys.stderr)
            continue
        if not data or len(data) < 200 or len(data) > 200 * 1024:
            # 太小 = Google 占位, 太大 = 不太可能是 favicon, 都跳过
            continue
        # 图片魔数: PNG(89 50 4E 47) / ICO(00 00 01 00) / GIF(47 49 46) / JPEG(FF D8 FF)
        head = data[:4]
        if not (
            head.startswith(b"\x89PNG")
            or head.startswith(b"\x00\x00\x01\x00")
            or head.startswith(b"GIF8")
            or head.startswith(b"\xff\xd8\xff")
        ):
            continue
        return data
    return None


def ensure_logo_cache(logos_dir: str) -> None:
    """遍历 LOGO_DOMAIN, 为每个域名确保 ``<logos_dir>/<domain>.png`` 存在。

    - 已存在: 跳过 (favicon 稳定, 不需重抓)。
    - 不存在: 联网抓取, 成功则落盘。失败不抛异常, 前端 <img> 的 onerror 会隐藏。
    每次运行只对新增/缺失的 ticker 做网络请求, 通常 20 个 ticker 首次运行后
    后续都是 0 次网络请求, 与"随 commit 上传"的意图匹配 (只在必要时更新)。
    """
    import os
    os.makedirs(logos_dir, exist_ok=True)
    fetched = 0
    for _sym, domain in LOGO_DOMAIN.items():
        if not domain:
            continue
        path = os.path.join(logos_dir, f"{domain}.png")
        if os.path.exists(path) and os.path.getsize(path) >= 200:
            continue
        print(f"Fetching favicon for {domain} ...", file=sys.stderr)
        data = _fetch_favicon_bytes(domain)
        if data is None:
            print(f"  !! favicon miss: {domain} (all sources failed)", file=sys.stderr)
            continue
        try:
            with open(path, "wb") as f:
                f.write(data)
            fetched += 1
        except OSError as e:
            print(f"  !! failed to write {path}: {e}", file=sys.stderr)
    if fetched:
        print(f"> logo cache: {fetched} new favicon(s) written to {logos_dir}", file=sys.stderr)




# ---------- 解析：statistics 页 ----------

# 匹配 {id:"xxx",title:"...",value:"...",hover:"..."} 结构。hover 是可选。
_PAT_FIELD = re.compile(
    r'\{id:"(?P<id>[^"]+)",title:"[^"]*",value:"(?P<value>[^"]*)"'
    r'(?:,hover:"(?P<hover>[^"]*)")?'
)


def parse_stats(html: str) -> dict[str, str]:
    """从 statistics 页面 HTML 中提取 id -> 值字典（优先用 hover 精确值）。"""
    result: dict[str, str] = {}
    for m in _PAT_FIELD.finditer(html):
        fid = m.group("id")
        hover = m.group("hover")
        value = m.group("value")
        result[fid] = hover if hover else value
    return result


# ---------- 解析：主页价格块 ----------

# 主页顶部：<div class="text-4xl font-bold ...">312.41</div>
_PAT_PRICE = re.compile(
    r'class="text-4xl font-bold[^"]*"[^>]*>([^<]+)<'
)

# 严格匹配 "Mon D, YYYY[, HH:MM AM/PM TZ]" 这种价格时间戳
# 例：Aug 6, 2026  /  Aug 7, 2026, 11:55 AM HKT
_PAT_ASOF = re.compile(
    r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s*20\d{2}'
    r'(?:,\s*\d{1,2}:\d{2}\s*(?:AM|PM)(?:\s+[A-Z]{2,4})?)?'
)


def parse_home(html: str) -> tuple[str | None, str | None]:
    """从主页 HTML 提取 (收盘价, 时间字符串)。

    价格取顶部 text-4xl 大号数字；时间戳只在价格块之后 800 字节内搜索，
    避免命中新闻列表 / Trends 模块里的其它日期。
    """
    m = _PAT_PRICE.search(html)
    price = m.group(1).strip() if m else None

    asof = None
    if m:
        # 只在价格块紧邻的下方区域查找日期，避免误匹配页面其它模块
        segment = html[m.end(): m.end() + 800]
        m2 = _PAT_ASOF.search(segment)
        if m2:
            asof = m2.group(0).strip()
    return price, asof


# ETF 主页内嵌 JSON 里同段 payload 就带了 peRatio / aum / expenseRatio 等指标。
# 形如：`...type:"etf",aum:"$229.61B",nav:"$213.03",expenseRatio:"0.03%",peRatio:"36.65",
#        sharesOut:"3.10B",dps:"$0.34",dividendYield:"0.38%",...`
# 值可能是 "..." 字符串、裸数字，也可能是 `void 0`（表示不适用，例如 SPYM 无 NAV）。
def _parse_etf_field(html: str, key: str) -> str | None:
    m = re.search(
        rf'(?<![A-Za-z]){re.escape(key)}:'
        r'(?:"(?P<s>[^"]*)"|(?P<v>void\s+0|null)|(?P<n>-?\d+(?:\.\d+)?))',
        html,
    )
    if not m:
        return None
    if m.group("v") is not None:
        return None  # void 0 / null
    if m.group("s") is not None:
        return m.group("s").strip() or None
    return m.group("n")


def parse_etf_snapshot(html: str) -> dict[str, str | None]:
    """从 ETF 主页 HTML 中抽取 stockanalysis 提供的一批 ETF 层面指标。

    返回值键是 stockanalysis 页面里出现的原始 key，未做归一化，
    进一步映射到 FIELDS 列名由调用方完成。
    """
    keys = (
        "peRatio", "aum", "nav", "expenseRatio", "sharesOut",
        "dps", "dividendYield", "payoutRatio", "beta", "ch1y",
    )
    return {k: _parse_etf_field(html, k) for k in keys}


# ---------- 解析：ETF 追踪指数的历史 P/E（第三方指数估值源） ----------
#
# ETF 组合层面并没有公开的历史 P/E 时序，因此我们退而求其次抓 ETF 追踪的**指数**的
# P/E 历史作为代理。所有源都严格标注在图表副标题里，用户能一眼看出这条曲线是
# "指数 P/E" 而非"ETF 组合 P/E"。


# multpl.com 的 by-month 表页：<table id="datatable"> 里每 <tr> 是 (Date, Value)。
# Value 单元格可能带 <abbr title="Estimate">†</abbr> 前缀（当月估算值），我们剥掉
# HTML 标签只保留末尾的数字。Date 形如 "Aug 6, 2026"。
_PAT_MULTPL_ROW = re.compile(
    r'<tr[^>]*>\s*<td>([^<]+)</td>\s*<td>(.*?)</td>\s*</tr>',
    re.DOTALL,
)


def parse_multpl_spx_pe(html: str, limit: int = 60) -> list[tuple[str, float]]:
    """解析 multpl.com S&P 500 P/E by-month 表，返回 [(date_iso, pe), ...] 旧->新序列。

    - `limit` 是最多返回的**最近**月份数（60 = 最近 5 年月度）。
    - 数据源：https://www.multpl.com/s-p-500-pe-ratio/table/by-month
    """
    # 只在 <table id="datatable"> ... </table> 之间匹配，防止误伤其它表
    m = re.search(r'<table[^>]*id="datatable"[^>]*>(.*?)</table>', html, re.DOTALL)
    if not m:
        return []
    body = m.group(1)

    out: list[tuple[str, float]] = []
    for row in _PAT_MULTPL_ROW.finditer(body):
        date_raw = row.group(1).strip()
        val_html = row.group(2)
        val_txt = re.sub(r'<[^>]+>', '', val_html)
        val_txt = re.sub(r'[†\s]+', ' ', val_txt).strip()
        try:
            pe = float(val_txt.split()[-1])
        except (ValueError, IndexError):
            continue
        # 归一化日期："Aug 6, 2026" -> "2026-08"
        try:
            d = dt.datetime.strptime(date_raw, "%b %d, %Y")
        except ValueError:
            continue
        # 月度频率：统一用月首 iso 日期作为 label
        iso = d.strftime("%Y-%m-%d")
        out.append((iso, pe))

    # multpl 页面本身是新->旧排列，截取最近 `limit` 项后反转为旧->新
    out = out[:limit]
    out.reverse()
    return out


# siblisresearch.com 的 Nasdaq 100 页表格结构：<table class="supsystic-table ...">
# 首行是表头 (Date / NASDAQ 100 Price / P/E (TTM) Ratio / EPS (TTM) / Forward P/E / EPS Fwd / CAPE)
# 免费版只暴露最近 ~10 个季末点，Date 形如 "6/30/2026"。
def parse_siblis_ndx_pe(html: str, limit: int = 60) -> list[tuple[str, float]]:
    """解析 siblisresearch.com Nasdaq-100 P/E 表，返回 [(date_iso, pe), ...] 旧->新序列。

    - 数据源（免费部分）：https://siblisresearch.com/data/nasdaq-100-pe-ratio/
    - 免费版只有约 10 个季末点，无法完整覆盖 5Y；`limit` 起截断作用（够 3Y 曲线）。
    """
    m = re.search(
        r'<table[^>]*class="[^"]*supsystic-table[^"]*"[^>]*>(.*?)</table>',
        html,
        re.DOTALL,
    )
    if not m:
        return []
    body = m.group(1)
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.DOTALL)
    if len(rows) < 2:
        return []

    out: list[tuple[str, float]] = []
    for row in rows[1:]:   # 跳过表头
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
        if len(cells) < 3:
            continue
        date_raw = re.sub(r'<[^>]+>|\s+', ' ', cells[0]).strip()
        pe_raw   = re.sub(r'<[^>]+>|\s+', ' ', cells[2]).strip()
        try:
            d = dt.datetime.strptime(date_raw, "%m/%d/%Y")
            pe = float(pe_raw.replace(",", ""))
        except ValueError:
            continue
        out.append((d.strftime("%Y-%m-%d"), pe))

    # 页面已按新->旧排列，取最近 `limit` 条并反转
    out = out[:limit]
    out.reverse()
    return out


# ---------- 抓取：ETF 发行商官方 Forward P/E（"P/E (Fwd)" 列的当前值） ----------
#
# ETF 组合层面的加权前瞻 P/E 由发行商每月/每日更新，是行业标准做法：
#   SSGA (SPYM): SSR HTML 里明文写 "Price/Earnings Ratio FY1 <td>21.58</td>"
#                (数据源: FactSet Estimates, weighted harmonic, 月度更新)
#   Invesco (QQQM): dng-api JSON 直接返回 forwardPriceToEarningsRatio
#                (数据源: 加权 harmonic, 每日更新)
# 两个源都保留原始数值精度, 由前端 _cell_html 统一 :.2f 格式化, 与其他 P/E 列一致。


# SSGA HTML 里"Price/Earnings Ratio FY1"表格行结构非常稳定, 用一行正则即可:
#   <th ... > Price/Earnings Ratio FY1 <...> </th> <td class="data">21.58</td>
# 注意 label 和 <td> 之间可能夹着 <span class="info">... tooltip ...</span>,
# 因此中间用 .*? 非贪婪吃掉。
_PAT_SSGA_FY1 = re.compile(
    r'Price/Earnings\s+Ratio\s+FY1.*?<td[^>]*class="data"[^>]*>\s*([\d.,]+)\s*</td>',
    re.DOTALL | re.IGNORECASE,
)


def fetch_ssga_forward_pe(url: str) -> float | None:
    """抓 SSGA ETF 页面, 提取 Price/Earnings Ratio FY1 (weighted harmonic FY1)。

    SSGA 每月月末更新, 数据源 FactSet Estimates。抓取失败或数值不合理时返回 None。
    """
    html_str = _get(url)
    if not html_str:
        return None
    m = _PAT_SSGA_FY1.search(html_str)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    # sanity: FY1 P/E 通常在 5 ~ 200 之间, 超出视为解析异常
    if v <= 0 or v > 500:
        return None
    return v


# SSGA HTML 里"Est. 3-5 Year EPS Growth"表格行, 与 FY1 P/E 完全同款结构:
#   <th ... > Est. 3-5 Year EPS Growth <...> </th> <td class="data">18.43%</td>
# 数值带 "%" 后缀, 抓取后剥掉再转 float.
_PAT_SSGA_EPS_GROWTH = re.compile(
    r'Est\.\s*3-5\s*Year\s+EPS\s+Growth.*?<td[^>]*class="data"[^>]*>\s*'
    r'([\d.,\-]+)\s*%?\s*</td>',
    re.DOTALL | re.IGNORECASE,
)


def fetch_ssga_eps_growth(url: str) -> float | None:
    """抓 SSGA ETF 页面, 提取 Est. 3-5 Year EPS Growth (加权平均, FactSet Estimates)。

    返回百分比数值 (例如 18.43 代表 18.43%)。抓取失败或数值不合理返回 None。
    与 fetch_ssga_forward_pe 分开两次请求: 虽然可以复用同一份 HTML,
    但 SSGA 站点 CDN 命中率高, 一次快照多 1 次请求 (~200ms) 影响可忽略,
    保持函数职责单一便于失败排查。
    """
    html_str = _get(url)
    if not html_str:
        return None
    m = _PAT_SSGA_EPS_GROWTH.search(html_str)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    # sanity: 3-5Y EPS growth 通常 -20% ~ +50%, 超出视为解析异常
    if v < -50 or v > 100:
        return None
    return v


def fetch_invesco_fund_characteristics(cusip: str) -> dict[str, float] | None:
    """通过 Invesco dng-api 拿 fundCharacteristics 全量字段。

    URL 模式:
      https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}
        ?expand=nav&idType=cusip&variationType=fundCharacteristics&productType=ETF

    返回 dict, 包含 (key 为 None 表示该项缺失或值不合理):
      - forward_pe : forwardPriceToEarningsRatio (加权 harmonic forward P/E)
      - roe        : returnOnEquity (百分比数值, 例如 34.79 表示 34.79%)
    合并成一次请求, 避免同一 CUSIP 反复调用 3+ 次。
    """
    api = (
        f"https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}"
        f"?expand=nav&idType=cusip&variationType=fundCharacteristics&productType=ETF"
    )
    req = urllib.request.Request(api, headers={
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://www.invesco.com/",
        "Origin": "https://www.invesco.com",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
    except Exception as e:
        print(f"  !! Invesco API {api} failed: {e}", file=sys.stderr)
        return None
    try:
        j = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  !! Invesco API parse failed: {e}", file=sys.stderr)
        return None

    out: dict[str, float] = {}

    fp = j.get("forwardPriceToEarningsRatio")
    if isinstance(fp, (int, float)) and 0 < fp <= 500:
        out["forward_pe"] = float(fp)

    roe = j.get("returnOnEquity")
    # Invesco 用百分比数字 (34.786... = 34.79%). sanity: -100 ~ +200%
    if isinstance(roe, (int, float)) and -100 < roe <= 200:
        out["roe"] = float(roe)

    return out


def fetch_invesco_forward_pe(cusip: str) -> float | None:
    """兼容薄壳: 老接口只关心 forward P/E, 内部委托给
    fetch_invesco_fund_characteristics。新代码请直接用后者拿 dict。
    """
    fc = fetch_invesco_fund_characteristics(cusip)
    if fc is None:
        return None
    return fc.get("forward_pe")


# ---------- 抓取：Vanguard fund characteristics（VUG 等 Vanguard ETF 官方组合层面指标） ----------
#
# Vanguard 前端 SPA (investor.vanguard.com) 的 characteristic 数据取自静态 JSON:
#   https://investor.vanguard.com/investment-products/etfs/profile/api/{ticker}/characteristic
# 返回结构 (以 VUG 为例, 2026-06-30 快照):
#   {
#     "equityCharacteristic": {
#       "asOfDate": "2026-06-30T00:00:00-04:00",
#       "shortName": "Growth ETF               ",
#       "benchmarkShortName": "Morningstar US Large Cap Growth Index",
#       "fund": {
#         "earningsGrowthRate": "32.4",        # % (裸数字字符串)
#         "medianMarketCap":     "$1.8 trillion",
#         "numberOfStocks":      "147",
#         "priceEarningsRatio":  "35.6x",       # TTM 加权 (Morningstar 口径)
#         "priceBookRatio":      "12.5x",
#         "returnOnEquity":      "36.1",        # %
#         "turnoverRate":        "12.3",        # %
#         "foreignHolding":      "0.2",         # %
#         "totalNetAssets":      "$378.8 billion",
#         ...
#       },
#       "benchmark": { ...同上 fields... }
#     },
#     "fundCharacteristic": {}, "fixedIncomeCharacteristic": {}, "moneyMarketCharacteristic": {}
#   }
# SSGA / Invesco 提供 forward P/E, Vanguard 只公布截至月末的 TTM P/E + EPS growth + ROE,
# 因此本函数只回填 eps_growth / roe (以补齐 ETF 专属列), forward P/E 保持 None。
def fetch_vanguard_fund_characteristics(ticker: str) -> dict[str, float] | None:
    """通过 Vanguard investor.vanguard.com 前端代理 API 拿指定 ETF 的组合层面
    characteristics (Portfolio composition -> Characteristics 面板同源)。

    URL 模式:
      https://investor.vanguard.com/investment-products/etfs/profile/api/{ticker}/characteristic

    返回 dict, key 缺失表示该项在 payload 里没提供或值非法:
      - eps_growth : earningsGrowthRate    (百分比数值, 例如 32.4 表示 32.4%)
      - roe        : returnOnEquity        (百分比数值, 例如 36.1 表示 36.1%)
    """
    api = (
        f"https://investor.vanguard.com/investment-products/etfs/profile/api/"
        f"{ticker.lower()}/characteristic"
    )
    req = urllib.request.Request(api, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://investor.vanguard.com/investment-products/etfs/profile/{ticker.lower()}",
        "Origin": "https://investor.vanguard.com",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
    except Exception as e:
        print(f"  !! Vanguard characteristic API {api} failed: {e}", file=sys.stderr)
        return None
    try:
        j = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  !! Vanguard characteristic API parse failed: {e}", file=sys.stderr)
        return None

    fund = ((j.get("equityCharacteristic") or {}).get("fund") or {})
    out: dict[str, float] = {}

    # Vanguard 用裸数字字符串 (无 %/x 后缀), 直接 float 转换.
    #   earningsGrowthRate "32.4" -> 32.4 (%)
    #   returnOnEquity     "36.1" -> 36.1 (%)
    def _to_pct(raw) -> float | None:
        if not isinstance(raw, str):
            return None
        try:
            v = float(raw.strip().rstrip("%"))
        except ValueError:
            return None
        # sanity: -100 ~ +200% (增长率/ROE 都不该超出这个量级)
        return v if -100 < v <= 200 else None

    eg = _to_pct(fund.get("earningsGrowthRate"))
    if eg is not None:
        out["eps_growth"] = eg

    roe = _to_pct(fund.get("returnOnEquity"))
    if roe is not None:
        out["roe"] = roe

    return out


# ---------- 解析：ratios 页（TTM 时间序列，如 PE / EV-EBIT） ----------

# 站点在 ratios 页面里以 datekey:[...] 和 <field>:[...] 的形式嵌入
# 20 个季度点的 TTM 时间序列（首个通常是 "TTM"，其余是季末日期）。
# 注意：某些季度分母极小时站点会返回 null（如 AMZN 疫情期 pe），
# 因此数组内除数字外还可能出现字面量 null。
_PAT_DATEKEY = re.compile(r'datekey:\[([^\]]+)\]')


def _parse_ttm_series(html: str, field: str) -> list[tuple[str, float]]:
    """通用：从 ratios 页面解析 `<field>:[...]` 与 `datekey:[...]` 对齐的 TTM 时间序列。

    - field 例："pe"、"evebit"；正则允许字面量 `null` 作为空值。
    - 返回 [(date_label, value), ...]，按时间从旧到新排序。
    """
    dk = _PAT_DATEKEY.search(html)
    arr = re.search(rf'(?<![A-Za-z0-9]){re.escape(field)}:\[([-\d\.,eEnul]+)\]', html)
    if not dk or not arr:
        return []
    dates = [s.strip().strip('"') for s in dk.group(1).split(",")]
    raw_vals = [s.strip() for s in arr.group(1).split(",")]
    values: list[float | None] = []
    for s in raw_vals:
        if s == "" or s.lower() == "null":
            values.append(None)
            continue
        try:
            values.append(float(s))
        except ValueError:
            values.append(None)
    n = min(len(dates), len(values))
    # 过滤 null 点；剩余按时间倒序（站点是新->旧）反转成旧->新
    pairs = [(dates[i], values[i]) for i in range(n) if values[i] is not None]
    return pairs[::-1]


def parse_pe_history(html: str) -> list[tuple[str, float]]:
    """TTM P/E 历史序列（旧->新）。"""
    return _parse_ttm_series(html, "pe")


def parse_ev_ebit_history(html: str) -> list[tuple[str, float]]:
    """TTM EV/EBIT 历史序列（旧->新）。"""
    return _parse_ttm_series(html, "evebit")


def _parse_quarterly_array(html: str, field: str) -> list[float | None]:
    """从季度损益表 HTML 中提取指定字段的数组（如 epsdil / opinc），保持站点原始順序（新->旧）。

    可能包含 null / 空值，统一转为 None。
    """
    m = re.search(rf'(?<![A-Za-z0-9]){re.escape(field)}:\[([-\d\.,eEnul]+)\]', html)
    if not m:
        return []
    out: list[float | None] = []
    for s in m.group(1).split(","):
        s = s.strip()
        if s == "" or s.lower() == "null":
            out.append(None); continue
        try:
            out.append(float(s))
        except ValueError:
            out.append(None)
    return out


def parse_quarterly_income(html: str) -> tuple[list[str], list[float | None], list[float | None]]:
    """解析季度损益表，返回三元组 (dates, epsdil, opinc)，均为新->旧顺序（与站点一致）。

    - dates : 季报报告期末日期字符串 (如 '2026-06-27')
    - epsdil: 当季稀释 EPS（4 个相加 = TTM EPS）
    - opinc : 当季 Operating Income = EBIT（4 个相加 = TTM EBIT）
    """
    if not html:
        return [], [], []
    dk = _PAT_DATEKEY.search(html)
    dates = [s.strip().strip('"') for s in dk.group(1).split(",")] if dk else []
    eps = _parse_quarterly_array(html, "epsdil")
    op  = _parse_quarterly_array(html, "opinc")
    return dates, eps, op


def _rolling_ttm(values_new_to_old: list[float | None], idx: int) -> float | None:
    """取 values_new_to_old 从 idx 开始向后（时间上向早）4 个季度的和作为 TTM 值。

    - values_new_to_old 遵循站点顺序：index 0 为最新季，index 越大越早。
    - idx 为“最新已发布季报”在数组中的下标。TTM 包含 idx 在内同 4 个季报。
    - 任何一个季为 None 则返回 None（保守）。
    """
    if idx < 0 or idx + 4 > len(values_new_to_old):
        return None
    total = 0.0
    for k in range(idx, idx + 4):
        v = values_new_to_old[k]
        if v is None:
            return None
        total += v
    return total


# ---------- 单只抓取 ----------

def _to_float(s: str | None) -> float | None:
    """把 statistics 页里带逗号/百分号的字符串转成 float，转不了返回 None。"""
    if s is None:
        return None
    t = s.replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _fetch_weekly_prices(ticker: str, market: str) -> list[tuple[str, float]]:
    """抓 stockanalysis 官方 history API 的 1Y 周频 K 线，返回 [(date, close), ...]（旧->新）。"""
    raw = _get(_url_history_weekly(ticker, market))
    if not raw:
        return []
    try:
        j = json.loads(raw)
    except Exception:
        return []
    rows = j.get("data") or []
    # API 返回按新->旧，需要反转成旧->新
    pairs: list[tuple[str, float]] = []
    for item in rows:
        t = item.get("t")
        c = item.get("c")
        if t and c is not None:
            try:
                pairs.append((t, float(c)))
            except (TypeError, ValueError):
                pass
    return pairs[::-1]


def _url_history_daily_recent(ticker: str, market: str) -> str:
    """近 5 个交易日日线 K 线 API，用于盘中回退取昨日收盘。"""
    if market == "US":
        return (
            f"https://api.stockanalysis.com/api/symbol/s/{ticker.lower()}/history"
            f"?type=stock&range=5D&period=Daily"
        )
    if market == "HK":
        return (
            f"https://api.stockanalysis.com/api/symbol/q/hkg-{ticker}/history"
            f"?type=stock&range=5D&period=Daily"
        )
    raise ValueError(f"unknown market: {market}")


def _fetch_last_daily_close(ticker: str, market: str) -> tuple[str, float] | None:
    """取最近一条已收盘的日线 (date_str, close)。用于盘中时段回退到"前一日收盘价"。

    stockanalysis 的日线 API 只包含已完成的交易日（当日盘中不会出现），
    因此在盘中调用时"最后一条"天然就是前一交易日的收盘。
    """
    raw = _get(_url_history_daily_recent(ticker, market))
    if not raw:
        return None
    try:
        j = json.loads(raw)
    except Exception:
        return None
    rows = j.get("data") or []
    # API 返回按新->旧；直接取第一条即最近的已收盘日
    for item in rows:
        t = item.get("t")
        c = item.get("c")
        if t and c is not None:
            try:
                return (str(t), float(c))
            except (TypeError, ValueError):
                continue
    return None


# 从 parse_home 返回的 asof 字符串中提取"日期部分"，例如：
#   "Aug 7, 2026, 11:55 AM HKT" -> "Aug 7, 2026"
#   "Aug 6, 2026"                -> "Aug 6, 2026"
_PAT_ASOF_DATE = re.compile(
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s*20\d{2}'
)
_MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1
)}


def _asof_to_iso(asof: str | None) -> str | None:
    """把 "Aug 7, 2026, 11:55 AM HKT" 里的日期抽出，转成 "2026-08-07"。失败返回 None。"""
    if not asof:
        return None
    m = _PAT_ASOF_DATE.search(asof)
    if not m:
        return None
    parts = m.group(0).replace(",", "").split()
    # ["Aug", "7", "2026"]
    if len(parts) < 3:
        return None
    mo = _MONTHS.get(parts[0])
    try:
        day = int(parts[1])
        year = int(parts[2])
    except ValueError:
        return None
    if not mo:
        return None
    return f"{year:04d}-{mo:02d}-{day:02d}"


_MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _iso_to_asof(iso_date: str | None) -> str | None:
    """把 "2026-08-06" 转成 "Aug 6, 2026"，与源站页面 AsOf 短日期风格一致。"""
    if not iso_date:
        return None
    try:
        y, m, d = iso_date.split("-")
        yi, mi, di = int(y), int(m), int(d)
        if not 1 <= mi <= 12:
            return None
        return f"{_MONTH_NAMES[mi - 1]} {di}, {yi}"
    except (ValueError, IndexError):
        return None


def _build_1y_history(
    weekly: list[tuple[str, float]],
    q_dates: list[str],
    q_eps: list[float | None],
    q_ebit: list[float | None],
    shares_out: float | None,
    debt: float | None,
    cash: float | None,
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """按周收盘价反算 1Y 的 P/E 与 EV/EBIT 序列，严格对齐到“该周所属季”的滚动 4 季 TTM。

    公式（q(t) = t 时刻最近一份已发布季报）：
        EPS_TTM(t)   = sum(epsdil[q(t)..q(t)-3])
        EBIT_TTM(t)  = sum(opinc[q(t)..q(t)-3])
        P/E_week     = weekly close ÷ EPS_TTM(t)
        EV_week      = weekly close × shares_out(today) + debt(today) - cash(today)
        EV/EBIT_week = EV_week ÷ EBIT_TTM(t)

    说明：
    - 周股价属于发布日 <= 周收盘日 的最近一份季报（可避免“未来函数”偏差）。
    - shares/debt/cash 数据源只供今日快照（statistics 页无历史季度值），已在图上注明。
    - q_dates / q_eps / q_ebit 均为新->旧顺序（与 stockanalysis 一致）。
    """
    pe_series: list[tuple[str, float]] = []
    ev_series: list[tuple[str, float]] = []

    # 预解析季报日为可比较的 datetime.date
    q_date_objs: list[dt.date | None] = []
    for s in q_dates:
        try:
            q_date_objs.append(dt.date.fromisoformat(s))
        except Exception:
            q_date_objs.append(None)

    for date_str, price in weekly:
        try:
            wd = dt.date.fromisoformat(date_str)
        except Exception:
            continue

        # 找发布日 <= wd 的最新季报（数组新->旧，正向扫描遇到的第一个就是最新）
        idx = -1
        for i, qd in enumerate(q_date_objs):
            if qd is not None and qd <= wd:
                idx = i; break
        if idx < 0:
            continue

        eps_ttm = _rolling_ttm(q_eps, idx)
        if eps_ttm not in (None, 0):
            pe_series.append((date_str, price / eps_ttm))

        ebit_ttm = _rolling_ttm(q_ebit, idx)
        if shares_out and ebit_ttm not in (None, 0):
            mcap = price * shares_out
            ev = mcap + (debt or 0.0) - (cash or 0.0)
            ev_series.append((date_str, ev / ebit_ttm))

    return pe_series, ev_series


# ---------- DCF / Fair Value 抓取 ----------
# DCF Tab 采用"自己算"的方式: 前端用两阶段 Gordon Growth 公式做敏感性网格,
#   V = Σ FCF₀·(1+g)^t / (1+WACC)^t + [FCF_N·(1+G) / (WACC-G)] / (1+WACC)^N
# 其中显式期年数 N 由 1Y/3Y/5Y 按钮切换, X 轴 WACC (5-10%), Y 轴 g (0-10%),
# 底部滑块控制永续增长 G (0-5%, 默认 2.5%)。每格显示 Upside% (相对当前股价)。
# 权益价值 = EV - 净债务, 每股 = 权益 / 股本; 股本 / 债务 / 现金 / 股价 均已在
# snapshot 里, 因此后端只需要额外抓一个基期 FCF₀ (TTM Free Cash Flow)。
#
# 数据源: stockanalysis.com  `/stocks/{t}/financials/cash-flow-statement/?p=trailing`
#   页面里嵌入了一个 JS 对象字面量, 内含所有科目的季度 TTM 序列, 字段名无引号,
#   格式类似:  fcf:[136683000000, 129174000000, ...]  (新→旧)
#   我们只取第 0 个元素作为最新 TTM FCF。ETF / 无 forecast 页的 ticker 返回 None,
#   前端对应卡片显示"数据不可用"。
#
# ETF 无 cash-flow 页, 上游直接跳过。FMP DCF endpoint 已不再使用 (免费 tier 无法
# 拿到分年 FCF/WACC/g 明细); FMP_API_KEY 环境变量仍在, 供未来其它场景复用。
def _fetch_sa_fcf(ticker: str, market: str) -> dict | None:
    """从 stockanalysis.com 的 cash-flow-statement TTM 页抓最新 Free Cash Flow。

    Returns
    -------
    dict | None
        None: 页面 404 / 抓取失败 / 解析不到 fcf 字段。
        dict: {"fcf_ttm": float(美元原值), "asof": str, "source_url": str}
    """
    if market == "ETF":
        return None
    if market == "HK":
        # 港股路径: /quote/hkg/{code}/financials/cash-flow-statement/?p=trailing
        url = f"https://stockanalysis.com/quote/hkg/{ticker}/financials/cash-flow-statement/?p=trailing"
    else:
        url = f"https://stockanalysis.com/stocks/{ticker.lower()}/financials/cash-flow-statement/?p=trailing"

    html = _get(url)
    if not html:
        print(f"  .. SA FCF {ticker}: page not accessible ({url})", file=sys.stderr)
        return None

    # 字段名无引号: fcf:[数值,数值,...] 或 fcf:[数值,null,数值,...]。
    # 数组顺序为"新→旧", 覆盖约 5 年。
    #
    # 关键: 页面里存在两组 fcf: 数组:
    #   (a) 主数据区: fcf:[9879902000,null,null,null,4581552000,null,...]
    #       -- 只有半年报的港股公司会有大量 null (Q1/Q3 未披露)
    #   (b) prior 缓存: prior:{fcf:[1292892000,958891000],...}
    #       -- stockanalysis 内部的历史快照, 数字被"聚合"过, 不是真正的 FCF
    # 老正则 [-0-9.eE, ] 字符集不含 'n'/'u'/'l', 遇到 null 就匹配失败,
    # re.search 会继续往后找到 (b), 导致数据被污染 (差近 8 倍)。
    # 修复: 用 [^\]]+ 贪婪到 ']', 然后按元素解析, null → skip。
    m = re.search(r'\bfcf\s*:\s*\[([^\]]+)\]', html)
    if not m:
        print(f"  !! SA FCF {ticker}: 'fcf' array not found in page", file=sys.stderr)
        return None
    raw_fcf = m.group(1)
    raw_parts = [p.strip() for p in raw_fcf.split(",")]
    if not raw_parts:
        print(f"  !! SA FCF {ticker}: 'fcf' array is empty", file=sys.stderr)
        return None

    # 同步抓 datekey (与 fcf 数组一一对应, 用于识别披露频率)
    md = re.search(r'\bdatekey\s*:\s*\[([^\]]+)\]', html)
    dates_all: list[str] = []
    if md:
        dates_all = re.findall(r'"([^"]+)"', md.group(1))

    # 逐元素解析: null / 空 / 坏值都跳过, 但保留对应的 datekey (用来判定频率)
    fcf_series: list[float] = []
    dates_kept: list[str]   = []
    for i, p in enumerate(raw_parts):
        if not p or p == "null":
            continue
        try:
            v = float(p)
        except ValueError:
            continue
        fcf_series.append(v)
        if i < len(dates_all):
            dates_kept.append(dates_all[i])

    if not fcf_series:
        print(f"  !! SA FCF {ticker}: cannot parse any numeric fcf values", file=sys.stderr)
        return None

    # 判定披露频率: 看相邻两个有效点日期差 (取中位数, 抗异常点)
    #   ~3 个月 → quarterly (美股/大部分港股)
    #   ~6 个月 → semiannual (只披露半年报的港股, 如 9992 泡泡玛特, 9633 农夫山泉)
    #   ~12 个月 → annual (仅年报)
    # 注: frequency 仅作 "整体披露密度" 的粗略提示; 前端计算 N 年 CAGR 时不依赖它,
    # 而是按 fcf_dates 中"最接近 N 年前"的那一期做匹配, 从而能正确处理 9992 这种
    # "早期季度报, 近期改半年报" 的混合披露公司。
    from datetime import datetime as _dt
    frequency = "quarterly"  # 默认
    if len(dates_kept) >= 2:
        gaps_days: list[int] = []
        for i in range(len(dates_kept) - 1):
            try:
                d0 = _dt.strptime(dates_kept[i],     "%Y-%m-%d")
                d1 = _dt.strptime(dates_kept[i + 1], "%Y-%m-%d")
                gaps_days.append(abs((d0 - d1).days))
            except (ValueError, TypeError):
                pass
        if gaps_days:
            gaps_days.sort()
            median_gap = gaps_days[len(gaps_days) // 2]
            if   median_gap >= 300: frequency = "annual"
            elif median_gap >= 130: frequency = "semiannual"
            else:                   frequency = "quarterly"

    # 保留窗口: 覆盖约 5 年即可 (太老的意义不大)
    # 用 fcf_dates 的首尾日期跨度判断, 保留跨度 <= ~5.5 年的部分
    if dates_kept:
        try:
            d_head = _dt.strptime(dates_kept[0], "%Y-%m-%d")
            keep_n = len(fcf_series)
            for i in range(1, len(dates_kept)):
                try:
                    di = _dt.strptime(dates_kept[i], "%Y-%m-%d")
                    if (d_head - di).days > 365 * 5 + 180:  # ~5.5 年
                        keep_n = i
                        break
                except ValueError:
                    pass
            fcf_series = fcf_series[:keep_n]
            dates_kept = dates_kept[:keep_n]
        except ValueError:
            fcf_series = fcf_series[:20]
            dates_kept = dates_kept[:20]
    else:
        fcf_series = fcf_series[:20]
    fcf_ttm = fcf_series[0]

    # 顺带抓 fiscalYear[0] 作为 "asof" 标签 (格式 "2026 Q3" 或年度 "2024")
    asof = None
    m2 = re.search(r'\bfiscalYear\s*:\s*\[([^\]]+)\]', html)
    if m2:
        vals = re.findall(r'"([^"]+)"', m2.group(1))
        if vals:
            asof = vals[0]
    # 页面可能还有 fiscalQuarter: 用它补齐 "2026 Q3" 之类
    mq = re.search(r'\bfiscalQuarter\s*:\s*\[([^\]]+)\]', html)
    if mq and asof:
        qs = re.findall(r'"([^"]+)"', mq.group(1))
        if qs:
            asof = f"{asof} {qs[0]}"

    # 报表原币 (functional/reporting currency): 页面里一定有一行
    #   "Financials in millions CNY. Fiscal year is ..."
    #   "Financials in millions HKD. Fiscal year is ..."
    # 用它作为 fcf_series 的实际计价币。
    # 港股常见分歧: 报价币是 HKD (statistics/quote 页), 但报表原币是 CNY (大陆背景公司,
    # 如 9992 泡泡玛特 / 9633 农夫山泉 / 0700 腾讯) 或 HKD (港资公司)。
    # 美股则一般都是 USD, 极少数 ADR (如 BABA) 也是 USD 报表。
    report_currency: str | None = None
    mc = re.search(r'Financials\s+in\s+\w+\s+([A-Z]{3})', html)
    if mc:
        report_currency = mc.group(1)

    print(f"  ok SA FCF {ticker}: TTM FCF={fcf_ttm/1e9:.2f}B {report_currency or '?'} "
          f"({asof or 'n/a'}) series_len={len(fcf_series)} freq={frequency}",
          file=sys.stderr)
    return {
        "fcf_ttm":         fcf_ttm,
        "fcf_series":      fcf_series,  # 新→旧; 跨度约 5 年内
        "fcf_dates":       dates_kept,  # 与 fcf_series 一一对应的 YYYY-MM-DD 日期字符串
        "frequency":       frequency,   # 'quarterly' | 'semiannual' | 'annual' (粗略披露密度)
        "asof":            asof,
        "source_url":      url,
        "report_currency": report_currency,  # 报表原币 (可能 None)
    }


def _fetch_fx_implied(ticker: str, market: str, report_ccy: str, quote_ccy: str) -> dict | None:
    """反算 stockanalysis 内部使用的隐含即期汇率 report_ccy → quote_ccy。

    背景
    ----
    港股常见: 报价币 = HKD (statistics/quote/close/market-cap 都是港币), 但报表原币
    可能 = CNY (大陆背景公司如 9992/9633/0700) 或 USD (国际保险如 1299)。
    如果直接把 CNY 计价的 FCF₀ 拿去和 HKD 计价的 Net Debt / Price 混算 DCF, 每股
    公允价会系统性错约 10-14% (2026 年即期 CNY/HKD ≈ 1.11-1.14)。

    反算方法
    --------
    stockanalysis 在 statistics 页展示 HKD 数值 (EBIT / EBITDA / Net Income / FCF /
    Total Debt / Enterprise Value 都是 HKD), 而 financials 子页 (income / cash-flow)
    则用报表原币。同一实体、同一时点、两种计价, 相除即得 stockanalysis 内部使用
    的即期汇率。

    实现: 收集多个 (HKD, 原币) 锚点对, 取比值中位数抗畸值:
      * EBIT (stats) ÷ operatingIncome[0] (income-statement)
      * EBITDA (stats) ÷ ebitda[0] (income-statement)
      * Net Income (stats) ÷ netIncome[0] (income-statement)
      * Free Cash Flow (stats) ÷ fcf[0] (cash-flow-statement)

    Returns
    -------
    dict | None
        None: 报价币 == 报表币 (无需换算) / 抓取失败 / 锚点不足。
        dict: {
          "fx":       float,   # 1 unit report_ccy = fx units quote_ccy
          "asof":     str,     # 反算所用锚点的期末日 (YYYY-MM-DD, 从 datekey 取)
          "source":   str,     # 反算方法说明 (含使用的锚点数)
          "anchors":  list,    # [(label, hkd_val, cny_val, ratio), ...] 供调试
          "report_currency": str,
          "quote_currency":  str,
        }
    """
    if report_ccy == quote_ccy or not report_ccy or not quote_ccy:
        return None
    if market != "HK":
        # 目前只有港股会遇到 quote != report; 美股几乎全是 USD 一致.
        return None

    # 抓 stats 页 (HKD 侧)
    url_st = f"https://stockanalysis.com/quote/hkg/{ticker}/statistics/"
    html_st = _get(url_st)
    if not html_st:
        return None

    def _stats_val(name: str) -> float | None:
        """从 stats 页挖 `>Name<...title="123,456,789"...>` 里的原始数字."""
        p = re.search(
            rf'>{re.escape(name)}<[^<]*</span>.*?title="([0-9,\.\-]+)"',
            html_st, re.S
        )
        if not p:
            return None
        try:
            return float(p.group(1).replace(",", ""))
        except ValueError:
            return None

    stats_ebit   = _stats_val("EBIT")
    stats_ni     = _stats_val("Net Income")
    stats_fcf    = _stats_val("Free Cash Flow")

    # 抓 income-statement 页 (报表原币侧)
    # 关键: stockanalysis 的 IS 页字段名和 stats 页显示名不完全一致:
    #   stats "EBIT"        <-> IS "opinc"     (Operating Income = EBIT)
    #   stats "Net Income"  <-> IS "netinccmn" (Net Income to Common)
    #   stats "EBITDA"      <-> IS 页无原始序列 (stats 页是 opinc + D&A 合成), 跳过
    url_is = f"https://stockanalysis.com/quote/hkg/{ticker}/financials/?p=trailing"
    html_is = _get(url_is)
    is_first: dict[str, float] = {}
    is_asof: str | None = None
    if html_is:
        for key in ("opinc", "netinccmn"):
            m = re.search(rf'\b{key}\s*:\s*\[([^\]]+)\]', html_is)
            if m:
                for p in m.group(1).split(","):
                    p = p.strip()
                    if not p or p == "null":
                        continue
                    try:
                        is_first[key] = float(p); break
                    except ValueError:
                        continue
        md = re.search(r'\bdatekey\s*:\s*\[([^\]]+)\]', html_is)
        if md:
            dks = re.findall(r'"([^"]+)"', md.group(1))
            if dks:
                is_asof = dks[0]

    # 抓 cash-flow 页 (报表原币, fcf 首值)
    url_cf = f"https://stockanalysis.com/quote/hkg/{ticker}/financials/cash-flow-statement/?p=trailing"
    html_cf = _get(url_cf)
    cf_fcf: float | None = None
    if html_cf:
        m = re.search(r'\bfcf\s*:\s*\[([^\]]+)\]', html_cf)
        if m:
            for p in m.group(1).split(","):
                p = p.strip()
                if p and p != "null":
                    try: cf_fcf = float(p); break
                    except ValueError: pass

    # 组装锚点对
    anchors: list[tuple[str, float, float, float]] = []
    def _add(label: str, hkd: float | None, orig: float | None) -> None:
        if hkd is None or orig is None or not orig:
            return
        # 排除量级明显不匹配的畸值 (例如 stats 页某字段被空表格污染)
        # 合理汇率区间: 0.05 ≤ HKD/orig ≤ 20 (远宽于 CNY/HKD~1.1 或 USD/HKD~7.8)
        ratio = hkd / orig
        if not (0.05 <= abs(ratio) <= 20):
            return
        anchors.append((label, hkd, orig, ratio))

    _add("EBIT",           stats_ebit, is_first.get("opinc"))
    _add("Net Income",     stats_ni,   is_first.get("netinccmn"))
    _add("Free Cash Flow", stats_fcf,  cf_fcf)

    if len(anchors) < 2:
        print(f"  !! FX {ticker}: 锚点不足 ({len(anchors)} < 2), 放弃反算", file=sys.stderr)
        return None

    ratios = sorted(a[3] for a in anchors)
    n = len(ratios)
    median = ratios[n // 2] if n % 2 == 1 else (ratios[n // 2 - 1] + ratios[n // 2]) / 2

    # 一致性检查: 所有锚点比值都应接近中位数 (相对偏差 ≤ 2%)
    outliers = [a for a in anchors if abs(a[3] - median) / abs(median) > 0.02]
    if outliers:
        outlier_desc = ", ".join(f"{a[0]}={a[3]:.4f}" for a in outliers)
        print(f"  !! FX {ticker}: 锚点分歧 (median={median:.4f}, outliers: {outlier_desc})",
              file=sys.stderr)
        # 保留但标注在 source 里, 便于用户判断是否可信

    print(f"  ok FX {ticker}: 1 {report_ccy} = {median:.4f} {quote_ccy} "
          f"(反算自 {n} 锚点, asof={is_asof or '?'})", file=sys.stderr)

    return {
        "fx":              median,
        "asof":            is_asof,
        "source":          f"stockanalysis 双页反算 ({n} 锚点: {'/'.join(a[0] for a in anchors)})",
        "anchors":         [(a[0], a[1], a[2], a[3]) for a in anchors],
        "report_currency": report_ccy,
        "quote_currency":  quote_ccy,
    }


def _fetch_dcf(ticker: str, market: str, currency: str | None = None) -> dict | None:
    """获取 DCF 敏感性图所需的基期数据。ETF 直接返回 None。

    前端还需要: shares_out / total_debt / cash / price / EBIT — 这些已经在 snapshot
    里注入 JS, 因此这里只补 FCF₀ 一个字段。

    港股币种问题
    ----------
    stockanalysis 港股页面存在"报价币 vs 报表币"差异 (如 9992/9633/0700 报表币是
    CNY, 但股价/市值/净债务是 HKD)。为了让前端 DCF 计算不混币, 这里同时抓取报表
    原币 (report_currency) 并反算隐含即期汇率 (fx_to_quote), 前端在算 fair value
    前会把 FCF₀ 乘以 fx_to_quote 换算成报价币。假设未来所有年份汇率恒定 = 今日即期
    (学术上主流做法; 汇率的不确定性远小于 g/WACC)。
    """
    if market == "ETF":
        return None
    fcf = _fetch_sa_fcf(ticker, market)
    if fcf is None:
        return None
    report_ccy = fcf.get("report_currency")
    quote_ccy  = currency  # 报价币 (US=USD, HK=HKD)

    # 若报表币 != 报价币 (常见于港股), 反算隐含汇率
    fx_to_quote: float = 1.0
    fx_asof: str | None    = None
    fx_source: str | None  = None
    fx_note:  str | None   = None
    if report_ccy and quote_ccy and report_ccy != quote_ccy:
        fx = _fetch_fx_implied(ticker, market, report_ccy, quote_ccy)
        if fx:
            fx_to_quote = fx["fx"]
            fx_asof     = fx["asof"]
            fx_source   = fx["source"]
        else:
            # 反算失败: 保守起见 fx=1 且明确标注 "未换算", 让前端 tooltip 可以警告用户
            fx_note = f"⚠ 报表币 {report_ccy} != 报价币 {quote_ccy}, 但汇率反算失败, 未做换算"
    elif report_ccy and quote_ccy and report_ccy == quote_ccy:
        fx_source = "报表币 = 报价币, 无需换算"

    return {
        "fcf_ttm":         fcf["fcf_ttm"],
        "fcf_series":      fcf.get("fcf_series") or [fcf["fcf_ttm"]],
        "fcf_dates":       fcf.get("fcf_dates") or [],
        "frequency":       fcf.get("frequency") or "quarterly",
        "asof":            fcf.get("asof"),
        "source_url":      fcf["source_url"],
        "currency":        quote_ccy,        # 报价币 (与 close/shares/debt 一致)
        "report_currency": report_ccy,       # 报表原币 (fcf_ttm 的实际计价)
        "fx_to_quote":     fx_to_quote,      # 换算系数: fcf_ttm(报表币) * fx_to_quote = fcf_ttm(报价币)
        "fx_asof":         fx_asof,          # 汇率反算所用锚点的期末日
        "fx_source":       fx_source,        # 汇率来源说明 (显示在 tooltip)
        "fx_note":         fx_note,          # 异常情况的用户可读提示 (可能 None)
    }


def _fetch_etf(ticker: str) -> Row:
    """ETF 分支: 请求 /etf/{t}/ 主页抽取 PE / AUM 等 ETF 层面指标, 并按 ETF 所追踪的
    指数抓取历史 P/E 时序作为"指数代理"曲线。

    stockanalysis 未提供 ETF 的 statistics / ratios / income 页 (全部 404), 且发行商
    不发布 ETF 组合的历史 P/E 序列, 因此历史曲线走第三方指数估值源 (见 ETF_PE_SOURCE)。
    每个 ticker 的数据源标签会被记录到 Row.pe_history_source, 前端在图表副标题里明确
    展示"这是指数 P/E 而非 ETF 组合 P/E"。EV/EBIT 时序对 ETF 一律 N/A。
    """
    row: dict[str, str | None] = {CLOSE_COL: None, CLOSE_DATE_COL: None}
    row.update({v: None for v in FIELDS.values()})

    home_html = _get(_url_home(ticker, "ETF"))
    if not home_html:
        return Row(ticker, row)

    row[CLOSE_COL], row[CLOSE_DATE_COL] = parse_home(home_html)

    # 盘中回退到前一交易日收盘 (日线 API 对 ETF 同样兼容 type=stock)
    asof_iso = _asof_to_iso(row.get(CLOSE_DATE_COL))
    last = _fetch_last_daily_close(ticker, "US")
    if last is not None:
        prev_date, prev_close = last
        if asof_iso and asof_iso > prev_date:
            prev_asof = _iso_to_asof(prev_date) or prev_date
            print(
                f"  .. {ticker}: intraday snapshot detected "
                f"(\"{row[CLOSE_DATE_COL]}\") -> falling back to previous close "
                f"{prev_close:g} @ {prev_asof}",
                file=sys.stderr,
            )
            row[CLOSE_COL] = f"{prev_close:g}"
            row[CLOSE_DATE_COL] = prev_asof

    etf = parse_etf_snapshot(home_html)

    # ETF -> 主表列 的字段映射:
    #   peRatio  -> PE          (ETF 层面加权 P/E, 由 stockanalysis 侧计算)
    #   aum      -> MarketCap   (基金总资产净值, 与股票的 MarketCap 单位一致, 便于并排比较)
    # 其余 EV/EBIT 家族字段对 ETF 语义上不成立, 保持 None (前端渲染为 "-")。
    pe_raw  = etf.get("peRatio")
    aum_raw = etf.get("aum")
    # ETF PE (TTM) 来自 stockanalysis 侧的组合层面加权 P/E; 每个 ETF 都从同一页面拉取,
    # 所以 label 用统一的通用文案 (若日后不同 ETF 换源, 再按 ticker 分支覆盖)。
    pe_ttm_source_label: str | None = None
    eps_growth_source_label: str | None = None
    roe_source_label: str | None = None
    if pe_raw is not None:
        row[FIELDS["pe"]] = pe_raw
        pe_ttm_source_label = "stockanalysis.com · ETF weighted P/E (TTM)"
    if aum_raw is not None:
        # aum 形如 "$229.61B" / "$97.11B" -> 去掉 $ 前缀, 与股票列 "3.90T" 风格保持一致;
        # _fmt_number 只处理裸数字, fullmatch 失败会原样返回, 因此这里保留 "229.61B"
        # 字符串, 前端表格会原样显示 "229.61B"。
        row[FIELDS["marketcap"]] = aum_raw.lstrip("$").strip() or aum_raw

    # -------- 发行商官方 ETF 组合层面指标 --------
    # 一次性从发行商拿三项 (可拿到哪几项取决于源):
    #   - Forward P/E     : 加权 harmonic FY1
    #   - EPS Growth 3-5Y : 加权平均 (FactSet Estimates, 仅 SSGA 发布)
    #   - ROE             : 加权平均 (仅 Invesco 发布)
    # SPYM (SSGA HTML)     -> forward_pe, eps_growth        ; roe 保持 None
    # QQQM (Invesco JSON)  -> forward_pe,           roe     ; eps_growth 保持 None
    # VUG  (无源)          -> 三项都 None
    # Row.pe_forward_source 记录 forward-PE 的数据源标签, 前端 P/E (Fwd) 单元格 tooltip 显示出处。
    fwd_pe_source_label: str | None = None
    fwd_src = ETF_FWD_PE_SOURCE.get(ticker.upper())
    if fwd_src is not None:
        fwd_label, fwd_arg, fwd_parser = fwd_src
        fwd_val: float | None = None
        eps_g_val: float | None = None
        roe_val: float | None = None

        if fwd_parser == "ssga_html":
            # SSGA HTML 同一个页面里两项都能抓, 分两次请求 (SSGA 有 CDN 缓存, 无所谓)
            fwd_val = fetch_ssga_forward_pe(fwd_arg)
            eps_g_val = fetch_ssga_eps_growth(fwd_arg)
        elif fwd_parser == "invesco_api":
            # Invesco 一次 API 调用同时拿 forward PE + ROE
            fc = fetch_invesco_fund_characteristics(fwd_arg)
            if fc is not None:
                fwd_val = fc.get("forward_pe")
                roe_val = fc.get("roe")

        if fwd_val is not None:
            # 与其他 P/E 列的字符串格式保持一致 (2 位小数, 供 _parse_num 再解析)
            row[FIELDS["peForward"]] = f"{fwd_val:.2f}"
            fwd_pe_source_label = fwd_label
        else:
            print(f"  .. {ticker}: forward P/E unavailable from {fwd_label}", file=sys.stderr)

        # 百分比字段: 存成裸数字字符串 (与 _cell_html 的 pct 分支约定一致),
        # 例如 "18.43" -> 显示 "18.43%"
        # label 也同步记录, 但为语义精确, 单独构造 (不复用 fwd_label 里的 PE 字段名):
        #   - SSGA:    EPS Growth 走 FactSet 3-5Y 前瞻口径
        #   - Invesco: ROE 走加权平均 (fundCharacteristics API 返回)
        if eps_g_val is not None:
            row[FIELDS["epsGrowth3To5Y"]] = f"{eps_g_val:.2f}"
            if fwd_parser == "ssga_html":
                eps_growth_source_label = (
                    "SSGA · Est. 3-5 Year EPS Growth (FactSet Estimates, weighted mean)"
                )
            else:
                eps_growth_source_label = fwd_label
        if roe_val is not None:
            row[FIELDS["returnOnEquity"]] = f"{roe_val:.2f}"
            if fwd_parser == "invesco_api":
                roe_source_label = (
                    "Invesco dng-api · returnOnEquity (weighted average)"
                )
            else:
                roe_source_label = fwd_label

    # -------- Vanguard 官方 characteristic (VUG 等 Vanguard ETF) --------
    # Vanguard 不发布 forward P/E, 但公布组合层面 TTM EPS growth + ROE (Morningstar 口径,
    # 见 fetch_vanguard_fund_characteristics doc). 这里只回填 EPS growth / ROE 两项,
    # 保留 fwd_src 分支已经填过的 forward_pe / eps_growth / roe (SSGA/Invesco 优先).
    vg_src = ETF_VANGUARD_SOURCE.get(ticker.upper())
    if vg_src is not None:
        vg_label, vg_arg = vg_src
        vc = fetch_vanguard_fund_characteristics(vg_arg)
        if vc is not None:
            # 仅当当前列为空时才回填 (不覆盖 SSGA/Invesco 已提供的更权威值)
            if row[FIELDS["epsGrowth3To5Y"]] is None and "eps_growth" in vc:
                row[FIELDS["epsGrowth3To5Y"]] = f"{vc['eps_growth']:.2f}"
                eps_growth_source_label = vg_label
            if row[FIELDS["returnOnEquity"]] is None and "roe" in vc:
                row[FIELDS["returnOnEquity"]] = f"{vc['roe']:.2f}"
                roe_source_label = vg_label
        else:
            print(f"  .. {ticker}: Vanguard characteristics unavailable ({vg_label})", file=sys.stderr)

    # -------- 指数代理: 历史 P/E 时序 --------
    # 5Y = 最近 60 个月度点 (SPX) / 或 ~10 个季末点 (NDX 免费版), 3Y = 5Y 尾部切片，
    # 1Y = 5Y 数组的更小尾部切片（月度 12 点 / 季度 4 点）。
    # VUG 追踪 CRSP US LC Growth, 无公开源 -> 保持 None, 前端不显示曲线。
    src = ETF_PE_SOURCE.get(ticker.upper())
    if src is not None:
        label, url, parser_name = src
        idx_html = _get(url)
        pe_hist: list[tuple[str, float]] = []
        if idx_html:
            if parser_name == "multpl_spx":
                pe_hist = parse_multpl_spx_pe(idx_html, limit=60)
            elif parser_name == "siblis_ndx":
                pe_hist = parse_siblis_ndx_pe(idx_html, limit=60)
        if pe_hist:
            # 无论 1Y/3Y/5Y, 数组最右端都应当反映"当前 TTM"快照（与股票分支一致）:
            #   - 中间历史点保留数据源的日期 + 数值 (月度 SPX 或 季度 NDX)
            #   - 最右端根据"数据源最新点距今天数"分两种做法, 避免日期显得过时:
            #     * 距今 <= STALE_DAYS: 数据源点已经足够新, 直接把它的日期改成今天 + 值改成 snap
            #       (SPYM: multpl 每天都会更新一个"今日"点, 差 ~1 天; QQQM 6/30 之后到 9/30 之前无更新时不适用)
            #     * 距今  > STALE_DAYS: 数据源点太老, 保留原点作为"历史点"不动,
            #       在末尾追加一个日期=今天 / 值=snap 的新点。曲线尾段会拉出一条直线,
            #       诚实反映"数据源空档期"(QQQM 常态: 6/30 -> today 追加一个点)。
            #   snap_pe 来自 stockanalysis ETF 主页 (etf.peRatio, ETF 组合当日加权 P/E),
            #   保证 1Y/3Y/5Y 三条曲线的最右点都 = 表格里 P/E 列的当前值。
            snap_pe = _to_float(pe_raw) if pe_raw is not None else None
            if snap_pe is not None:
                STALE_DAYS = 20  # 阈值: 数据源末点距今超过 20 天视为"陈旧"
                today_iso = dt.date.today().isoformat()
                last_date_str, _last_val = pe_hist[-1]
                try:
                    last_date = dt.date.fromisoformat(last_date_str)
                    gap_days = (dt.date.today() - last_date).days
                except ValueError:
                    gap_days = 0  # 解析失败按"新鲜"处理, 沿用原日期
                appended_snap = False
                if gap_days > STALE_DAYS:
                    # 追加"今日快照点", 中间的数据源末点保留原值不动
                    pe_hist.append((today_iso, snap_pe))
                    appended_snap = True
                else:
                    # 数据源末点够新, 直接把它替换成"今日快照点"
                    pe_hist[-1] = (today_iso, snap_pe)
            else:
                appended_snap = False

            r = Row(ticker, row)
            r.pe_history = pe_hist
            # 3Y / 1Y 尾部切片: 数据源频率 × 时间跨度 = 应有的季末/月末点数。
            # 若走了"追加分支"(appended_snap=True), 说明末点是"今日快照"而非季末/月末,
            # 应额外多取 1 点, 保证时间跨度真的覆盖过去 3Y / 1Y。
            # 例: QQQM 1Y = 4 个季末 (含最近 6/30) + 1 个 today 快照 = 5 个点。
            #     SPYM 走"覆盖分支", 12 个月度点里最后一个已经是 today, 无需+1。
            extra = 1 if appended_snap else 0
            if parser_name == "multpl_spx":
                take_3y = 36 + extra
                take_1y = 12 + extra
                r.pe_history_3y = pe_hist[-take_3y:] if len(pe_hist) > take_3y else list(pe_hist)
                r.pe_history_1y = pe_hist[-take_1y:] if len(pe_hist) > take_1y else list(pe_hist)
            else:
                take_3y = 12 + extra
                take_1y = 4  + extra
                r.pe_history_3y = pe_hist[-take_3y:] if len(pe_hist) > take_3y else list(pe_hist)
                r.pe_history_1y = pe_hist[-take_1y:] if len(pe_hist) > take_1y else list(pe_hist)
            r.pe_history_source = label
            r.pe_forward_source = fwd_pe_source_label
            r.pe_ttm_source = pe_ttm_source_label
            r.eps_growth_source = eps_growth_source_label
            r.roe_source = roe_source_label
            return r
        else:
            print(f"  .. {ticker}: index PE history unavailable ({url})", file=sys.stderr)

    tail = Row(ticker, row)
    tail.pe_forward_source = fwd_pe_source_label
    tail.pe_ttm_source = pe_ttm_source_label
    tail.eps_growth_source = eps_growth_source_label
    tail.roe_source = roe_source_label
    return tail


def fetch_ticker(ticker: str, market: str) -> Row:
    if market == "ETF":
        return _fetch_etf(ticker)

    row: dict[str, str | None] = {CLOSE_COL: None, CLOSE_DATE_COL: None}

    home_html = _get(_url_home(ticker, market))
    if home_html:
        row[CLOSE_COL], row[CLOSE_DATE_COL] = parse_home(home_html)

    # 若网页上的时间戳日期 > 数据源日线里最近一条已收盘日期，说明当天正在盘中交易，
    # 此时改用"最近一条已收盘日线"（= 前一交易日收盘价）。
    # stockanalysis 的日线 API 只包含已完成交易日，因此其"最新点"天然是最近的收盘。
    asof_iso = _asof_to_iso(row.get(CLOSE_DATE_COL))
    last = _fetch_last_daily_close(ticker, market)
    if last is not None:
        prev_date, prev_close = last
        # 只有当 web 页面日期显著地"新于"日线最后一条时才判定为盘中
        if asof_iso and asof_iso > prev_date:
            prev_asof = _iso_to_asof(prev_date) or prev_date
            print(
                f"  .. {ticker}: intraday snapshot detected "
                f"(\"{row[CLOSE_DATE_COL]}\") -> falling back to previous close "
                f"{prev_close:g} @ {prev_asof}",
                file=sys.stderr,
            )
            row[CLOSE_COL] = f"{prev_close:g}"
            row[CLOSE_DATE_COL] = prev_asof

    # 港股统一样式：若 AsOf 只有日期（没 HKT/时区标签），补上 "4:00 PM HKT"
    # 目的是让港股所有行的 AsOf 与美股 "..., 4:00 PM EDT" 风格一致。
    if market == "HK":
        cur_asof = row.get(CLOSE_DATE_COL)
        if cur_asof and "HKT" not in cur_asof and ":" not in cur_asof:
            row[CLOSE_DATE_COL] = f"{cur_asof}, 4:00 PM HKT"

    stats_html = _get(_url_stats(ticker, market))
    if not stats_html:
        row.update({v: None for v in FIELDS.values()})
        return Row(ticker, row)
    parsed = parse_stats(stats_html)
    row.update({col: parsed.get(fid) for fid, col in FIELDS.items()})

    # 额外抓 ratios 页拿到 TTM 历史序列：
    #   5Y → ?p=trailing 20 个季度点
    #   3Y → 从 5Y 数组尾部截取最近 12 个季度点
    ratios_html_q = _get(_url_ratios(ticker, market, "trailing"))
    pe_hist_q = parse_pe_history(ratios_html_q) if ratios_html_q else []
    ev_hist_q = parse_ev_ebit_history(ratios_html_q) if ratios_html_q else []

    # 源站的最新点 label 是字面量 "TTM"（非日期），前端 X 轴切片会显示为 "TTM"；
    # 这里统一改成今天日期（YYYY-MM-DD），让 X 轴按正常日期格式呈现（3Y/5Y 会切成 "YYYY-MM"）。
    today_iso = dt.date.today().isoformat()
    if pe_hist_q and pe_hist_q[-1][0] == "TTM":
        pe_hist_q[-1] = (today_iso, pe_hist_q[-1][1])
    if ev_hist_q and ev_hist_q[-1][0] == "TTM":
        ev_hist_q[-1] = (today_iso, ev_hist_q[-1][1])

    # 序列是旧->新，所以“最近 12 个季度”取末尾 12 个
    pe_hist_3y = pe_hist_q[-12:] if pe_hist_q else []
    ev_hist_3y = ev_hist_q[-12:] if ev_hist_q else []

    # 1Y 周频：股价按周变化；EPS_TTM / EBIT_TTM 用季度损益表 4 季滚动求和，
    # 每一周对齐到"该周之前最近一份已发布季报"（避免未来函数）。
    weekly = _fetch_weekly_prices(ticker, market)
    income_html = _get(_url_income_quarterly(ticker, market))
    q_dates, q_eps, q_ebit = parse_quarterly_income(income_html or "")
    pe_hist_1y, ev_hist_1y = _build_1y_history(
        weekly,
        q_dates=q_dates,
        q_eps=q_eps,
        q_ebit=q_ebit,
        shares_out=_to_float(parsed.get("sharesout")),
        debt=_to_float(parsed.get("debt")),
        cash=_to_float(parsed.get("totalcash")),
    )

    # 1Y 序列的"最新那一周"用 statistics 页当前 TTM 快照覆盖：
    #   - 前面所有周点仍按"周价 ÷ 该周所属季 TTM"严谨反算（历史准确）
    #   - 最右点直接对齐今天的 P/E (TTM) / EV/EBIT (TTM) 快照
    # 这样 1Y / 3Y / 5Y 三条曲线的最右点都指向同一个"当前 TTM"值。
    snap_pe = _to_float(parsed.get("pe"))
    if snap_pe is not None and pe_hist_1y:
        pe_hist_1y[-1] = (pe_hist_1y[-1][0], snap_pe)
    snap_ev_ebit = _to_float(parsed.get("evEbit"))
    if snap_ev_ebit is not None and ev_hist_1y:
        ev_hist_1y[-1] = (ev_hist_1y[-1][0], snap_ev_ebit)

    return Row(
        ticker, row,
        pe_history=pe_hist_q or None,
        ev_ebit_history=ev_hist_q or None,
        pe_history_3y=pe_hist_3y or None,
        ev_ebit_history_3y=ev_hist_3y or None,
        pe_history_1y=pe_hist_1y or None,
        ev_ebit_history_1y=ev_hist_1y or None,
    )


# ---------- 输出 ----------

def _fmt_number(s: str | None) -> str:
    """把 hover 里的裸数字（如 '4,497,194,773,800'）格式化成 T/B/M。"""
    if s is None:
        return "N/A"
    raw = s.replace(",", "").replace("$", "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return s
    try:
        v = float(raw)
    except ValueError:
        return s
    absv = abs(v)
    if absv >= 1e12:
        return f"{v / 1e12:,.2f}T"
    if absv >= 1e9:
        return f"{v / 1e9:,.2f}B"
    if absv >= 1e6:
        return f"{v / 1e6:,.2f}M"
    return f"{v:,.2f}"


def print_table(title: str, rows: list[Row]) -> None:
    cols = [CLOSE_COL, CLOSE_DATE_COL] + list(FIELDS.values())
    headers = ["Ticker"] + cols
    # 日期字段（AsOf）可能较长（如 "Aug 7, 2026, 11:55 AM HKT"），列宽给足
    widths = [10, 10, 30] + [12] * len(FIELDS)

    def line(cells):
        return "  ".join(str(c).rjust(w) for c, w in zip(cells, widths))

    print()
    print(f"== {title} ==")
    print(line(headers))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for r in rows:
        cells = [r.symbol]
        for c in cols:
            v = r.data.get(c)
            if c in ("MarketCap", "EV", "Debt", "Cash&STI", "EBIT(TTM)"):
                cells.append(_fmt_number(v))
            else:
                cells.append("N/A" if v is None else v)
        print(line(cells))


# ---------- HTML 输出 ----------

import html
import json
from datetime import datetime

_MONEY_COLS = {"MarketCap", "EV", "Debt", "Cash&STI", "EBIT(TTM)"}

# 表头显示名
_HTML_HEADERS = {
    "Ticker":     "Ticker",
    CLOSE_COL:    "Close",
    CLOSE_DATE_COL: "As&nbsp;Of",
    "MarketCap":  "Market Cap",
    "EV":         "EV",
    "Debt":       "Total Debt",
    "Cash&STI":   "Cash&nbsp;+&nbsp;STI",
    "EBIT(TTM)":  "EBIT&nbsp;(TTM)",
    "EV/EBIT":    "EV&nbsp;/&nbsp;EBIT",
    "EV/EBITDA":  "EV&nbsp;/&nbsp;EBITDA",
    "PE":         "P/E&nbsp;(TTM)",
    "PE Fwd":     "P/E&nbsp;(Fwd)",
    "PEG":        "PEG",
    "EPS Growth (3-5Y Est)": "EPS&nbsp;Growth&nbsp;(3-5Y&nbsp;Est)",
    "ROE":        "ROE",
}


def _parse_num(s: str | None) -> float | None:
    if s is None:
        return None
    raw = s.replace(",", "").replace("$", "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _rank_class(col: str, val: float | None) -> str:
    """
    根据经验阈值给关键估值指标染色：
        绿色 = 便宜/优秀   黄色 = 一般   红色 = 偏贵/差
    """
    if val is None:
        return ""
    if col == "EV/EBIT":
        if val <= 15: return "good"
        if val <= 25: return "mid"
        return "bad"
    if col == "EV/EBITDA":
        if val <= 12: return "good"
        if val <= 20: return "mid"
        return "bad"
    if col in ("PE", "PE Fwd"):
        if val <= 20: return "good"
        if val <= 30: return "mid"
        return "bad"
    if col == "PEG":
        if val <= 1: return "good"
        if val <= 2: return "mid"
        return "bad"
    return ""


def _cell_html(col: str, v: str | None, currency: str) -> str:
    if v is None or v == "":
        return '<td class="num na">—</td>'

    if col in _MONEY_COLS:
        num = _fmt_number(v)
        if num != "N/A" and re.match(r"^-?[\d,]+(?:\.\d+)?[TBM]?$", num):
            # 货币标签只在标题栏显示一次，这里不再重复
            return f'<td class="num money"><span class="val">{html.escape(num)}</span></td>'
        return f'<td class="num">{html.escape(num)}</td>'

    if col in ("EV/EBIT", "EV/EBITDA", "PE", "PE Fwd", "PEG"):
        num_val = _parse_num(v)
        cls = _rank_class(col, num_val)
        disp = f"{num_val:,.2f}" if num_val is not None else html.escape(v)
        return f'<td class="num metric {cls}">{disp}</td>'

    # ETF 专用: 百分比列 (值本身是形如 "18.43" 或 "34.78611755" 的裸数字, 单位是 %).
    # 不做好坏染色 —— 高 ROE / 高 EPS growth 的"好"阈值跨行业差异极大,
    # 不擅自染色, 只显示 2 位小数 + "%" 后缀.
    if col in ("EPS Growth (3-5Y Est)", "ROE"):
        num_val = _parse_num(v)
        if num_val is None:
            return f'<td class="num">{html.escape(v)}</td>'
        return f'<td class="num pct">{num_val:,.2f}%</td>'

    if col == CLOSE_COL:
        # 收盘价：粗体单独展示
        return f'<td class="num close">{html.escape(v)}</td>'

    if col == CLOSE_DATE_COL:
        return f'<td class="asof">{html.escape(v)}</td>'

    return f'<td>{html.escape(v)}</td>'


def render_section_html(title: str, rows: list[Row], currency: str) -> str:
    # ETF section 只展示对 ETF 语义上成立的指标列, 按“成长/质量优先, 估值靠后”顺序:
    #   - EPS Growth  (发行商: SSGA 3-5Y EPS Growth; Invesco/VUG 无源)
    #   - ROE         (发行商: Invesco returnOnEquity; SSGA/VUG 无源)
    #   - PE          (ETF 加权 P/E, 来自 stockanalysis.com/etf/{t}/)
    #   - PE Fwd      (发行商官方加权前瞻 P/E: SSGA / Invesco / VUG 无源)
    # 其余列 (MarketCap/EV/Debt/Cash/EBIT/EV·EBIT/EV·EBITDA/PEG) 对 ETF
    # 组合层面语义不成立, 直接不渲染, 表格更紧凑.
    if title == "ETFs":
        metric_cols = [FIELDS["epsGrowth3To5Y"], FIELDS["returnOnEquity"],
                       FIELDS["pe"], FIELDS["peForward"]]
    else:
        # 个股: 沿用原来的完整列集 (排除 ETF 专用列)
        metric_cols = [c for c in FIELDS.values()
                       if c not in (FIELDS["epsGrowth3To5Y"], FIELDS["returnOnEquity"])]

    cols = ["Ticker", CLOSE_COL, CLOSE_DATE_COL] + metric_cols
    header_html = "".join(
        f'<th>{_HTML_HEADERS.get(c, c)}</th>' for c in cols
    )

    body_rows: list[str] = []
    for r in rows:
        domain = LOGO_DOMAIN.get(r.symbol, "")
        # 图标从本地缓存加载: ``../logos/<domain>.png``.
        # 报告位于 ``Pages/<YYYY>/`` 下, 用 ``../logos/`` 跳出年份目录到 Pages/logos/.
        # 不再依赖 Google/DDG/cccyun 等第三方 favicon 服务, 国内外读者都能秒开;
        # 抓取失败或首次运行网络不通时 png 缺失, onerror 会静默隐藏 <img>, 不影响其他内容.
        logo_html = (
            f'<img class="tk-logo" alt="" loading="lazy" '
            f'src="../logos/{html.escape(domain)}.png" '
            f'onerror="this.style.display=\'none\';">'
            if domain else ""
        )
        tds = [
            f'<td class="ticker">'
            f'<button type="button" class="tk-badge" data-ticker="{html.escape(r.symbol)}" '
            f'data-market="{currency}">'
            f'{logo_html}'
            f'<span class="tk-sym">{html.escape(r.symbol)}</span>'
            f'</button>'
            f'</td>'
        ]
        for c in cols[1:]:
            tds.append(_cell_html(c, r.data.get(c), currency))
        body_rows.append(
            f'<tr data-ticker="{html.escape(r.symbol)}">' + "".join(tds) + "</tr>"
        )

    return f"""
    <section class="market">
      <header class="market-head">
        <h2>{html.escape(title)}</h2>
        <div class="head-meta">
          <span class="cur-badge cur-{currency.lower()}">{currency}</span>
          <span class="pill">{len(rows)} tickers</span>
        </div>
      </header>
      <div class="table-wrap">
        <table>
          <thead><tr>{header_html}</tr></thead>
          <tbody>
            {''.join(body_rows)}
          </tbody>
        </table>
      </div>
    </section>
    """


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Valuation Snapshot</title>
{favicon_link}
<!--
  vs-data: machine-readable data contract for future historical extractors.
  Schema is versioned via data-schema attribute; the presence & id are stable across
  script refactors. Consumers should look up `<script id="vs-data">` and json.loads()
  its text content. See README for field definitions.
-->
<script type="application/json" id="vs-data" data-schema="1">{vs_data_json}</script>
<style>
  :root {{
    --bg: #0b1020;
    --bg-2: #121a33;
    --panel: #151d38;
    --panel-2: #1b2547;
    --border: #263263;
    --text: #e6ebff;
    --muted: #9aa4c7;
    --accent: #6ea8ff;
    --good: #22c55e;
    --good-bg: rgba(34,197,94,.12);
    --mid:  #f59e0b;
    --mid-bg: rgba(245,158,11,.12);
    --bad:  #ef4444;
    --bad-bg: rgba(239,68,68,.12);
    --shadow: 0 8px 30px rgba(0,0,0,.35);
    --mono: ui-monospace,"SF Mono",Consolas,"JetBrains Mono",Menlo,monospace;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #f5f7fb;
      --bg-2: #eef2f9;
      --panel: #ffffff;
      --panel-2: #f8fafc;
      --border: #e5e9f2;
      --text: #0f172a;
      --muted: #64748b;
      --accent: #2563eb;
      --good-bg: rgba(34,197,94,.14);
      --mid-bg:  rgba(245,158,11,.14);
      --bad-bg:  rgba(239,68,68,.12);
      --shadow: 0 6px 24px rgba(15,23,42,.08);
    }}
  }}

  * {{ box-sizing: border-box; }}
  html, body {{ margin:0; padding:0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    color: var(--text);
    background:
      radial-gradient(1200px 600px at 90% -10%, rgba(110,168,255,.15), transparent 60%),
      radial-gradient(900px 500px at -10% 10%, rgba(34,197,94,.10), transparent 60%),
      linear-gradient(180deg, var(--bg), var(--bg-2));
    min-height: 100vh;
    padding: 40px 24px 80px;
  }}

  .wrap {{ max-width: 1280px; margin: 0 auto; }}

  header.top {{
    display:flex; align-items:flex-end; justify-content:space-between; gap:16px;
    margin-bottom: 28px;
  }}
  header.top h1 {{
    margin: 0; font-size: 30px; letter-spacing:-.02em;
    background: linear-gradient(90deg, var(--accent), #a78bfa);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  header.top .sub {{
    color: var(--muted); font-size: 13px; margin-top: 6px;
  }}
  header.top .sub a {{ color: var(--accent); text-decoration: none; }}
  header.top .sub a:hover {{ text-decoration: underline; }}

  .legend {{
    display:flex; flex-wrap:wrap; gap:8px; align-items:center;
    color: var(--muted); font-size: 12px;
  }}
  .legend .chip {{
    display:inline-flex; align-items:center; gap:6px;
    padding: 4px 10px; border-radius:999px; border:1px solid var(--border);
    background: var(--panel);
  }}
  .legend .dot {{ width:8px; height:8px; border-radius:50%; }}
  .dot.good {{ background: var(--good); }}
  .dot.mid  {{ background: var(--mid); }}
  .dot.bad  {{ background: var(--bad); }}

  section.market {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: var(--shadow);
    overflow: hidden;
    margin-bottom: 28px;
  }}
  .market-head {{
    display:flex; align-items:center; justify-content:space-between; gap:12px;
    padding: 16px 20px; border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, var(--panel-2), transparent);
  }}
  .market-head h2 {{
    margin:0; font-size:18px; letter-spacing:-.01em;
    display:flex; align-items:center; gap:10px;
  }}
  .pill {{
    font-size: 11px; color: var(--muted);
    padding: 4px 10px; border-radius: 999px;
    border: 1px solid var(--border); background: var(--panel-2);
  }}

  .table-wrap {{ overflow-x: auto; }}
  table {{
    width: 100%; border-collapse: separate; border-spacing: 0;
    font-size: 13.5px;
  }}
  th, td {{
    padding: 12px 14px; text-align: center; white-space: nowrap;
    border-bottom: 1px solid var(--border);
    /* 列分隔竖线: 每格左侧一根细线, 首列由下方 :first-child 规则清除避免与外框重合 */
    border-left: 1px solid var(--border);
  }}
  th:first-child, td:first-child {{ border-left: none; }}
  thead th {{
    position: sticky; top: 0;
    background: var(--panel-2);
    color: var(--muted); font-weight: 600; font-size: 12px;
    text-transform: uppercase; letter-spacing: .04em;
  }}
  tbody tr:hover td {{ background: var(--panel-2); }}
  tbody tr:last-child td {{ border-bottom: none; }}

  /* 所有列（包括标题 th 与数值单元格）统一居中；保留 tabular-nums 以保证数字列宽稳定 */
  td.num, td.close, td.metric, td.money .val {{ font-family: var(--mono); font-variant-numeric: tabular-nums; }}
  td.asof {{ color: var(--muted); font-size: 12.5px; }}
  td.close {{ font-weight: 600; }}
  td.na {{ color: var(--muted); }}
  /* 单元格右上角"信息圆圈"按钮 (ⓘ): 悬停 title 提示数据来源, 点击新标签页跳转到源网页。
     覆盖 Stocks (US/HK) 与 ETF 表格所有已有数据源的数值列。
     - 承载 td 需要 position: relative (由 .has-src 挂在 td 上开启)
     - 按钮做成 10px 圆形, 半透明, 不遮挡数字; hover 时变亮变实 */
  td.has-src {{ position: relative; }}
  a.src-info {{
    position: absolute; top: 4px; right: 4px;
    width: 12px; height: 12px; line-height: 12px;
    border-radius: 50%;
    background: var(--accent); color: #fff;
    font-size: 9px; font-weight: 700; font-style: italic;
    font-family: Georgia, "Times New Roman", serif;
    text-align: center; text-decoration: none;
    opacity: .35;
    transition: opacity .15s ease, transform .08s ease;
    cursor: pointer;
  }}
  a.src-info:hover {{
    opacity: 1;
    transform: scale(1.15);
  }}
  .tk-badge {{
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 8px;
    background: linear-gradient(180deg, rgba(110,168,255,.16), rgba(110,168,255,.06));
    border: 1px solid rgba(110,168,255,.35);
    color: var(--text); font-weight: 700; letter-spacing:.02em;
    font-family: var(--mono); font-size: 12.5px;
    cursor: pointer; user-select: none;
    transition: transform .08s ease, box-shadow .15s ease, border-color .15s ease;
  }}
  .tk-badge .tk-logo {{
    width: 16px; height: 16px; border-radius: 4px;
    background: #fff;              /* 透明/深底图标在暗色背景上不至于消失 */
    object-fit: contain;
    flex: 0 0 auto;
  }}
  .tk-badge .tk-sym {{ line-height: 1; }}
  .tk-badge:hover {{
    transform: translateY(-1px);
    border-color: var(--accent);
    box-shadow: 0 4px 14px rgba(110,168,255,.25);
  }}
  .tk-badge.selected {{
    background: linear-gradient(180deg, color-mix(in srgb, var(--sel) 32%, transparent), color-mix(in srgb, var(--sel) 10%, transparent));
    border-color: var(--sel);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--sel) 35%, transparent);
    color: var(--sel);
  }}

  /* -------- Right-side floating chart panel -------- */
  .chart-panel {{
    position: fixed;
    right: 24px; top: 90px;
    width: min(560px, 42vw);
    /* 高度上限 = 视口高度 - 顶部 90px - 底部留白 24px, 超出则内部滚动 */
    max-height: calc(100vh - 114px);
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: var(--shadow);
    z-index: 20;
    transform: translateX(calc(100% + 40px));
    opacity: 0;
    pointer-events: none;
    transition: transform .28s ease, opacity .2s ease;
    /* 允许纵向滚动, 圆角裁剪保持原样 */
    overflow-y: auto;
    overflow-x: hidden;
    overscroll-behavior: contain;
  }}
  /* 自定义滚动条, 与暗色主题一致 */
  .chart-panel::-webkit-scrollbar {{ width: 8px; }}
  .chart-panel::-webkit-scrollbar-track {{ background: transparent; }}
  .chart-panel::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
  .chart-panel::-webkit-scrollbar-thumb:hover {{ background: var(--muted); }}
  .chart-panel.open {{
    transform: translateX(0);
    opacity: 1;
    pointer-events: auto;
  }}
  .chart-head {{
    display:flex; justify-content:space-between; align-items:center;
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    /* 顶部信息 + Close 按钮吸顶, 滚动时始终可见 */
    position: sticky; top: 0; z-index: 3;
  }}
  .chart-title {{ font-size: 15px; font-weight: 700; letter-spacing:-.01em; }}
  .chart-sub   {{ font-size: 11.5px; color: var(--muted); margin-top: 2px; }}
  .chart-close {{
    width: 30px; height: 30px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--panel-2); color: var(--muted); cursor: pointer;
    font-size: 20px; line-height: 1; padding: 0;
  }}
  .chart-close:hover {{ color: var(--text); border-color: var(--accent); }}

  .chart-selected-row {{
    display: flex; align-items: center; gap: 10px;
    padding: 10px 18px 0;
    /* 空 chips 时不塌行, 保留 range-group 的位置 */
    min-height: 32px;
  }}
  .chart-selected {{
    display:flex; flex-wrap:wrap; gap:6px;
    flex: 1 1 auto; min-width: 0;   /* chips 占据剩余空间 */
  }}
  .sel-chip {{
    display: inline-flex; align-items: center; gap: 5px;
    padding: 2px 10px; border-radius: 999px;
    border: 1px solid var(--border); font-size: 11px; font-weight: 700;
    font-family: var(--mono); background: var(--panel-2);
  }}
  .sel-chip .tk-logo {{
    width: 14px; height: 14px; border-radius: 3px;
    background: #fff; object-fit: contain; flex: 0 0 auto;
  }}

  .chart-body {{ padding: 8px 6px 4px; min-height: 200px; }}
  .chart-empty {{
    color: var(--muted); font-size: 13px; text-align:center;
    padding: 60px 20px; line-height: 1.6;
  }}
  .chart-body svg .grid {{ stroke: var(--border); stroke-width: 1; stroke-dasharray: 2 4; }}
    .chart-body svg .axis {{ stroke: var(--border); stroke-width: 1; }}
    .chart-body svg .axis-lbl {{ fill: var(--muted); font-size: 10.5px; font-family: var(--mono); }}
    .chart-body svg .last-val  {{ font-size: 11px; font-family: var(--mono); font-weight: 700; }}
    .chart-body svg .pt-lbl    {{ font-size: 9.5px; font-family: var(--mono); font-weight: 600; paint-order: stroke; stroke: var(--panel); stroke-width: 3px; stroke-linejoin: round; }}
    .chart-body svg .avg-line  {{ stroke-dasharray: 6 4; stroke-width: 1.4; opacity: 0.65; fill: none; }}
    .chart-body svg .avg-lbl   {{ font-size: 10px; font-family: var(--mono); font-weight: 700; paint-order: stroke; stroke: var(--panel); stroke-width: 3px; stroke-linejoin: round; opacity: 0.9; }}
    .chart-body svg .avg-imp   {{ font-size: 10.5px; font-family: var(--mono); font-weight: 700; paint-order: stroke; stroke: var(--panel); stroke-width: 3px; stroke-linejoin: round; }}
    .chart-body svg .avg-cur   {{ font-size: 9.5px;  font-family: var(--mono); font-weight: 600; paint-order: stroke; stroke: var(--panel); stroke-width: 3px; stroke-linejoin: round; opacity: 0.75; }}

  .chart-legend {{
    display:flex; flex-direction:column; gap:6px;
    padding: 8px 18px 16px;
    border-top: 1px solid var(--border);
  }}
  .lg-item {{
    display:flex; flex-wrap: wrap; align-items:center; gap:10px;
    font-size: 12px;
  }}
  .lg-swatch {{ width: 12px; height: 3px; border-radius: 2px; }}
  .lg-tk {{
    display: inline-flex; align-items: center; gap: 5px;
    font-family: var(--mono); font-weight: 700; min-width: 60px;
  }}
  .lg-tk .tk-logo {{
    width: 14px; height: 14px; border-radius: 3px;
    background: #fff; object-fit: contain; flex: 0 0 auto;
  }}
  .lg-stat {{ color: var(--muted); font-family: var(--mono); }}
  .lg-stat em {{ color: var(--muted); font-style: normal; opacity: .65; margin-right: 3px; }}

  /* Tab 切换 (P/E ↔ EV/EBIT) */
  .chart-tabs {{
    display:flex; gap:4px; padding: 10px 18px 0;
  }}
  .chart-tab {{
    flex: 1;
    padding: 7px 10px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--panel-2);
    color: var(--muted);
    font-size: 12px; font-weight: 700; letter-spacing: .02em;
    font-family: var(--mono);
    cursor: pointer;
    transition: color .15s ease, border-color .15s ease, background .15s ease;
  }}
  .chart-tab:hover {{ color: var(--text); border-color: var(--accent); }}
  .chart-tab.active {{
    color: var(--accent);
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 20%, transparent) inset;
  }}
  /* 选中 ETF 时, EV/EBIT 与 DCF Tab 对组合层面无意义, 用 hidden 类彻底隐藏 */
  .chart-tab.hidden {{ display: none; }}

  /* Range 切换 (1Y / 3Y / 5Y): 已从 metric tab 行挪到下一行, 与 ticker 徽章共行, 右对齐 */
  .chart-toolbar {{
    display:flex; align-items:center; gap:10px; padding: 10px 18px 0;
  }}
  .chart-toolbar .chart-tabs {{ flex: 1; padding: 0; }}
  .range-group {{
    display:inline-flex; padding: 3px; gap: 2px;
    border: 1px solid var(--border); border-radius: 10px;
    background: var(--panel-2);
    margin-left: auto;   /* 兜底: 即使父容器不是 flex 也保证右对齐 */
    flex: 0 0 auto;
  }}
  .range-btn {{
    padding: 4px 10px;
    border: 0; border-radius: 7px;
    background: transparent; color: var(--muted);
    font-size: 11px; font-weight: 700; letter-spacing: .04em;
    font-family: var(--mono);
    cursor: pointer;
    transition: color .15s ease, background .15s ease;
  }}
  .range-btn:hover {{ color: var(--text); }}
  .range-btn.active {{
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 14%, transparent);
  }}

  /* 1Y 本地反算的公式提示条（仅当 range=1Y 时显示） */
  .chart-formula {{
    display: none;
    margin: 0 18px 14px;
    padding: 10px 12px;
    border: 1px dashed color-mix(in srgb, var(--mid) 55%, var(--border));
    background: color-mix(in srgb, var(--mid) 8%, transparent);
    border-radius: 10px;
    font-size: 11.5px; line-height: 1.55;
    color: var(--muted);
  }}
  .chart-formula.show {{ display: block; }}
  .chart-formula .cf-tag {{
    display: inline-block; margin-right: 8px;
    padding: 1px 7px; border-radius: 999px;
    font-family: var(--mono); font-size: 10.5px; font-weight: 700; letter-spacing: .04em;
    color: var(--mid); background: color-mix(in srgb, var(--mid) 18%, transparent);
    border: 1px solid color-mix(in srgb, var(--mid) 40%, transparent);
  }}
  .chart-formula code {{
    font-family: var(--mono); font-size: 11px;
    color: var(--text);
    padding: 1px 4px; border-radius: 4px;
    background: color-mix(in srgb, var(--panel-2) 70%, transparent);
  }}

  /* ---------- DCF Tab: 敏感性网格样式 ----------
     每个 ticker 一张卡, 卡内: 基础参数条 + 永续增长率 G 滑块 + WACC×g 网格。*/
  .dcf-grid {{
    display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    padding: 4px 18px 8px;
  }}
  .dcf-card {{
    border: 1px solid var(--border);
    background: color-mix(in srgb, var(--panel-2) 55%, transparent);
    border-radius: 12px;
    padding: 12px 14px;
    font-size: 12.5px;
  }}
  .dcf-card.dcf-empty {{ opacity: .75; }}
  .dcf-card-head {{
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 10px; margin-bottom: 8px;
    border-bottom: 1px dashed var(--border);
    padding-bottom: 6px;
  }}
  .dcf-title b {{ font-size: 14px; color: var(--text); }}
  .dcf-sub {{ color: var(--muted); font-family: var(--mono); font-size: 10.5px; margin-left: 6px; }}
  /* 卡头右侧: 汇率徽标 + Current 价格, 中间以间隔分开 */
  .dcf-head-right {{ display: flex; align-items: baseline; gap: 10px; }}
  .dcf-fx-badge {{
    font-family: var(--mono); font-size: 10px;
    padding: 2px 6px; border-radius: 3px;
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    color: var(--text);
    border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
    white-space: nowrap; cursor: help;
  }}
  .dcf-empty-msg {{ color: var(--muted); font-size: 11.5px; line-height: 1.55; }}
  .dcf-empty-msg code {{
    font-family: var(--mono); font-size: 10.5px;
    padding: 1px 4px; border-radius: 3px;
    background: color-mix(in srgb, var(--mid) 15%, transparent);
    color: var(--text);
  }}

  /* 卡顶基础参数条: FCF₀ · Shares · Net Debt · Price */
  .dcf-params {{
    display: flex; flex-wrap: wrap; gap: 4px 14px;
    margin-bottom: 8px;
    font-family: var(--mono); font-size: 11px; color: var(--muted);
  }}
  .dcf-params b {{ color: var(--text); margin-left: 3px; }}
  .dcf-params .src {{
    margin-left: auto;
    color: var(--muted); text-decoration: none;
    border-bottom: 1px dotted currentColor;
    font-size: 10.5px;
  }}
  .dcf-params .src:hover {{ color: var(--accent); border-color: var(--accent); }}

  /* 永续增长率 G 滑块 */
  .dcf-slider-row {{
    display: flex; align-items: center; gap: 10px;
    margin: 6px 0 10px;
    font-family: var(--mono); font-size: 11px; color: var(--muted);
  }}
  .dcf-slider-row .lbl {{ flex: 0 0 auto; }}
  .dcf-slider-row .lbl b {{ color: var(--text); }}
  .dcf-slider-row input[type=range] {{
    flex: 1 1 auto; accent-color: var(--accent);
    height: 4px; cursor: pointer;
  }}
  .dcf-slider-row .val {{
    flex: 0 0 auto; min-width: 44px; text-align: right;
    color: var(--accent); font-weight: 700;
  }}

  /* 网格表 (WACC × g) */
  .dcf-heatmap {{
    width: 100%; border-collapse: separate; border-spacing: 2px;
    font-family: var(--mono); font-size: 10.5px;
    table-layout: fixed;
  }}
  .dcf-heatmap thead th {{
    padding: 3px 0; color: var(--muted); font-weight: 700;
    letter-spacing: .04em;
  }}
  .dcf-heatmap th.corner {{
    color: var(--muted); font-size: 9.5px;
    text-align: right; padding-right: 6px;
    line-height: 1.15;
  }}
  .dcf-heatmap th.corner .arrow {{ opacity: .55; }}
  .dcf-heatmap tbody th {{
    text-align: right; padding: 0 6px 0 0;
    color: var(--muted); font-weight: 600;
    font-size: 10.5px;
  }}
  .dcf-heatmap td.hm {{
    padding: 3px 2px; text-align: center;
    font-variant-numeric: tabular-nums;
    border-radius: 3px;
    color: var(--text); font-weight: 600;
    cursor: help;
    white-space: nowrap;
    /* 背景色由 JS 内联 style 注入 (HSL 红→绿) */
  }}
  .dcf-heatmap td.hm.err {{
    color: var(--muted); background: transparent; font-weight: 400;
  }}
  /* 悬浮时更亮 */
  .dcf-heatmap td.hm:hover {{
    outline: 1.5px solid var(--accent);
    outline-offset: -1px;
  }}

  /* 插值行: 由 Actual g 精确值 (如 8.5%) 生成的独立行, 位于两档整数 g 之间 */
  .dcf-heatmap tbody tr.actual-row th {{
    color: var(--accent); font-weight: 700; font-style: italic;
    background: color-mix(in srgb, var(--accent) 10%, transparent);
  }}
  .dcf-heatmap tbody tr.actual-row td.hm {{
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 35%, transparent);
  }}
  /* 超界的 actual 行 (CAGR ≥ 10% 或 ≤ 0%): 用虚线上/下边框强调其位于网格外 */
  .dcf-heatmap tbody tr.actual-row.out-of-range th,
  .dcf-heatmap tbody tr.actual-row.out-of-range td {{
    background: color-mix(in srgb, var(--accent) 6%, transparent);
    border-top: 1.5px dashed color-mix(in srgb, var(--accent) 55%, transparent);
    border-bottom: 1.5px dashed color-mix(in srgb, var(--accent) 55%, transparent);
  }}
  .dcf-heatmap tbody tr.actual-row.out-of-range th {{
    font-size: 11px;   /* 箭头前缀 + 小数留出空间 */
  }}

  /* 卡底小结 */
  .dcf-footer {{
    margin-top: 8px; padding-top: 6px;
    border-top: 1px dashed var(--border);
    font-family: var(--mono); font-size: 10.5px;
    color: var(--muted);
    display: flex; flex-wrap: wrap; gap: 4px 12px;
  }}
  .dcf-footer b {{ color: var(--text); }}
  .dcf-footer .dcf-upside.pos {{ color: var(--good); font-weight: 700; }}
  .dcf-footer .dcf-upside.neg {{ color: var(--bad);  font-weight: 700; }}
  /* CAGR chip: 主行百分比 + 副行端点数值 (FCF₋ₙ → FCF₀), 供 CAGR 溯源 */
  .dcf-footer .dcf-cagr-chip {{
    display: inline-flex; flex-direction: column; line-height: 1.15;
    padding: 1px 2px; cursor: help;
  }}
  /* 档位标签 (1y/3y/5y): 默认不加粗, 仅当前档加粗以突出 */
  .dcf-footer .dcf-cagr-chip .dcf-cagr-label {{ font-weight: 400; color: var(--text); }}
  .dcf-footer .dcf-cagr-chip.current .dcf-cagr-label {{ font-weight: 700; }}
  .dcf-footer .dcf-cagr-endpoints {{
    font-size: 9px; color: var(--muted); opacity: 0.85;
    font-family: var(--mono);
  }}
  /* 当前 N 档: 端点数值也高亮 (与百分比同色, 但字重稍轻形成层次); 正/负分别绿/红 */
  .dcf-footer .dcf-cagr-endpoints.pos {{
    color: var(--good); opacity: 1; font-weight: 600;
  }}
  .dcf-footer .dcf-cagr-endpoints.neg {{
    color: var(--bad);  opacity: 1; font-weight: 600;
  }}

  /* range-group 在 DCF Tab 上语义变为"预测期年数", 显示不隐藏 */
  .range-group.hidden {{ display: none; }}

  @media (max-width: 640px) {{
    .dcf-grid {{ grid-template-columns: 1fr; }}
    .dcf-heatmap {{ font-size: 9.5px; }}
  }}

  @media (max-width: 900px) {{
    .chart-panel {{
      right: 12px; left: 12px; top: auto; bottom: 12px;
      width: auto; max-height: 65vh; overflow-y: auto;
      transform: translateY(calc(100% + 40px));
    }}
    .chart-panel.open {{ transform: translateY(0); }}
  }}

  .head-meta {{
    display:flex; align-items:center; gap:8px;
  }}
  .cur-badge {{
    display:inline-flex; align-items:center;
    padding: 4px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 700; letter-spacing: .06em;
    font-family: var(--mono);
  }}
  .cur-usd {{ background: rgba(34,197,94,.14);  color:#22c55e; border:1px solid rgba(34,197,94,.4); }}
  .cur-hkd {{ background: rgba(110,168,255,.14); color:#6ea8ff; border:1px solid rgba(110,168,255,.4); }}

  td.metric.good {{ background: var(--good-bg); color: var(--good); font-weight: 600; }}
  td.metric.mid  {{ background: var(--mid-bg);  color: var(--mid);  font-weight: 600; }}
  td.metric.bad  {{ background: var(--bad-bg);  color: var(--bad);  font-weight: 600; }}

  .title-row {{
    display:flex; align-items:center; gap:14px; flex-wrap:wrap;
  }}
  .gen-badge {{
    display:inline-flex; align-items:center; gap:6px;
    padding: 5px 12px; border-radius: 999px;
    background: var(--panel); border: 1px solid var(--border);
    color: var(--muted); font-family: var(--mono);
    font-size: 12px; letter-spacing: .02em;
    box-shadow: var(--shadow);
  }}
  .gen-badge svg {{ color: var(--accent); }}

  footer {{
    margin-top: 20px; color: var(--muted); font-size: 12px; text-align:center;
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="top">
      <div>
        <div class="title-row">
          <h1>Valuation Snapshot</h1>
          <span class="gen-badge" title="Report generation time">
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
            {generated_at}
          </span>
        </div>
        <div class="sub">
          Data source:
          <a href="https://stockanalysis.com/" target="_blank" rel="noreferrer">stockanalysis.com</a>
          · Values taken <b>as-is</b>, no local computation
        </div>
      </div>
      <div class="legend">
        <span class="chip"><span class="dot good"></span>cheap / strong</span>
        <span class="chip"><span class="dot mid"></span>fair</span>
        <span class="chip"><span class="dot bad"></span>rich / weak</span>
      </div>
    </header>

    {sections}

    <footer>
      Coloring uses rough rules of thumb (EV/EBIT ≤15 good ≤25 fair; PEG ≤1 good ≤2 fair; P/E ≤20 good ≤30 fair). Not investment advice.
    </footer>
  </div>

  <!-- 悬浮式图表面板：点击左侧 ticker 徽章后出现，支持 Tab 切换指标 -->
  <aside class="chart-panel" id="chartPanel" aria-hidden="true">
    <div class="chart-head">
      <div>
        <div class="chart-title" id="chartTitle">P/E (TTM) History</div>
        <div class="chart-sub"   id="chartSub">Click a ticker to load its history</div>
      </div>
      <button type="button" class="chart-close" id="chartClose" title="Close">×</button>
    </div>
    <div class="chart-toolbar">
      <div class="chart-tabs" role="tablist">
        <button type="button" class="chart-tab active" data-metric="pe"     role="tab" aria-selected="true">P/E (TTM)</button>
        <button type="button" class="chart-tab"        data-metric="evebit" role="tab" aria-selected="false">EV / EBIT (TTM)</button>
        <button type="button" class="chart-tab"        data-metric="dcf"    role="tab" aria-selected="false">DCF</button>
      </div>
    </div>
    <!-- 下一行: 左侧显示选中的 ticker 徽章, 右侧显示 1Y/3Y/5Y range 切换器 -->
    <div class="chart-selected-row">
      <div class="chart-selected" id="chartSelected"></div>
      <div class="range-group" id="rangeGroup" role="tablist" aria-label="time range">
        <button type="button" class="range-btn"        data-range="1y" role="tab" aria-selected="false">1Y</button>
        <button type="button" class="range-btn"        data-range="3y" role="tab" aria-selected="false">3Y</button>
        <button type="button" class="range-btn active" data-range="5y" role="tab" aria-selected="true">5Y</button>
      </div>
    </div>
    <div class="chart-body" id="chartBody">
      <div class="chart-empty">Click a ticker on the left to plot its history.<br/>Click again to remove; multiple tickers overlay for comparison.</div>
    </div>
    <div class="chart-legend" id="chartLegend"></div>
    <div class="chart-formula" id="chartFormula"></div>
  </aside>

  <script>
    // 每个 ticker 的 TTM 历史序列（旧->新），由 Python 端注入
    //   1Y = 周频（约 52 个点），本地反算（采用当前快照的 EPS/EBIT/股本/债务/现金）
    //   3Y = 季度 TTM（最近 12 个点），站点原始数据
    //   5Y = 季度 TTM（20 个点），站点原始数据
    const HISTORY_DATA = {{
      '1y': {{ pe: {pe_1y_json}, evebit: {ev_1y_json} }},
      '3y': {{ pe: {pe_3y_json}, evebit: {ev_3y_json} }},
      '5y': {{ pe: {pe_5y_json}, evebit: {ev_5y_json} }}
    }};
    // ticker -> P/E 历史曲线的数据源标签（例:"stockanalysis.com · quarterly TTM"、
    //   "S&P 500 Index P/E (TTM) · multpl.com"）。未收录的 ticker 表示无历史序列。
    //   render() 会按当前选中的 ticker 集合动态拼接到图表副标题，做到"每条曲线都有出处"。
    const PE_SOURCE = {pe_source_json};
    // ETF ticker 集合（用于动态隐藏对 ETF 无意义的 Tab：EV/EBIT、DCF）。
    // ETF 只保留 "P/E (TTM)" Tab，因为组合层面没有 EBIT / Debt / Cash 概念，
    // 也没有可对齐的 DCF 模型；只有指数代理的 P/E 时序有公开数据源。
    const ETF_SET = new Set({etf_set_json});
    // ticker -> Forward P/E (P/E Fwd 列) 的数据源标签（仅 ETF 有值, 例 SSGA/Invesco）。
    // 页面加载后会给对应 ticker 行的 "P/E (Fwd)" 单元格追加 title tooltip, 悬停可见出处。
    const FWD_SOURCE = {fwd_source_json};
    // ticker -> P/E (TTM) 单元格数据源标签. 个股走 stockanalysis Statistics,
    // ETF 走 stockanalysis /etf/{{t}}/ 主页. 与 FWD_SOURCE 采用相同的 tooltip 挂载方式.
    const PE_TTM_SOURCE = {pe_ttm_source_json};
    // ticker -> EPS Growth (3-5Y Est) 单元格数据源标签 (仅 ETF, 个股此列无数据).
    // SSGA: FactSet 3-5Y 前瞻; Vanguard (VUG): Morningstar TTM 增速回填.
    const EPS_GROWTH_SOURCE = {eps_growth_source_json};
    // ticker -> ROE 单元格数据源标签 (仅 ETF).
    // Invesco (QQQM): fundCharacteristics API; Vanguard (VUG/SPYM via VOO proxy): characteristic API.
    const ROE_SOURCE = {roe_source_json};
    // ticker -> 公司主页域名，用于 favicon 图标；缺失的 ticker 不显示 logo
    const LOGO_DOMAIN = {logo_map_json};
    // 生成一段 <img> 或空串（前端所有 ticker 出现处统一用它拼装图标）
    // 图标从本地缓存目录 ../logos/<domain>.png 加载（脚本运行时已抓取到 Pages/logos/,
    // 随 commit 一同上传）; 不依赖任何第三方 favicon 服务, 国内外都能秒开。
    // png 缺失时 onerror 静默隐藏 <img>, 不影响 ticker 文字部分。
    function logoImg(t) {{
      const d = LOGO_DOMAIN[t];
      if (!d) return '';
      return `<img class="tk-logo" alt="" loading="lazy" `
           + `src="../logos/${{d}}.png" `
           + `onerror="this.style.display='none';">`;
    }}

    // 每个 ticker 的当前快照（close/shares/debt/cash/ebit/pe/currency）
    // 用于在均值虚线右端反推"若估值回到均值时的隐含股价"
    const SNAPSHOT = {snapshot_json};
    // 每个 ticker 的 DCF 明细 (方案 C: 双数据源, 生成快照时抓取):
    //   {{ fmp: {{...FMP /stable/discounted-cash-flow...}} | null,
    //      sa:  {{...stockanalysis /forecast/ 分析师共识...}} | null,
    //      currency: 'USD' | 'HKD' | null }}
    // 结构详见 Python 端 Row.dcf 注释。ETF 与抓取全部失败的 ticker 保持 null,
    // 前端 DCF Tab 会分别渲染两个 panel (可各自 empty 显示缺失原因)。
    const DCF_DATA = {dcf_json};
    // 根据 metric 和均值反推该 ticker 的隐含股价；失败(数据缺失)返回 null
    function implyPrice(ticker, metric, avg) {{
      const s = SNAPSHOT[ticker];
      if (!s || !isFinite(avg)) return null;
      if (metric === 'pe') {{
        // implied = avg * EPS_TTM = avg / snap_pe * close
        if (!s.close || !s.pe) return null;
        return avg / s.pe * s.close;
      }}
      if (metric === 'evebit') {{
        // implied = (avg * EBIT_TTM - debt + cash) / shares
        if (!s.ebit || !s.shares) return null;
        const targetEv  = avg * s.ebit;
        const targetMc  = targetEv - (s.debt || 0) + (s.cash || 0);
        return targetMc / s.shares;
      }}
      return null;
    }}
    // 价格格式化：统一用 $ 前缀（USD / HKD 均使用同一符号）
    function fmtPrice(ticker, price) {{
      if (price == null || !isFinite(price)) return '';
      const abs = Math.abs(price);
      const digits = abs >= 100 ? 1 : 2;   // >=100 只留 1 位小数，避免过长
      return '$' + price.toFixed(digits);
    }}
    const METRIC_META = {{
      pe:     {{ title: 'P/E (TTM) History',       label: 'P/E (TTM)' }},
      evebit: {{ title: 'EV / EBIT (TTM) History', label: 'EV/EBIT (TTM)' }},
      dcf:    {{ title: 'DCF Valuation',            label: 'DCF' }}
    }};
    const RANGE_META = {{
        '1y': {{ label: 'Last 1Y', freq: 'weekly (computed locally)',     computed: true  }},
        '3y': {{ label: 'Last 3Y', freq: 'quarterly (fiscal period end)', computed: false }},
        '5y': {{ label: 'Last 5Y', freq: 'quarterly (fiscal period end)', computed: false }}
    }};
    // 1Y 本地反算公式（仅 UI 提示使用）
    const FORMULA_HTML = {{
      pe:     "P/E<sub>week</sub> = weekly close &divide; EPS<sub>TTM</sub>(q<sub>t</sub>) &nbsp;&middot;&nbsp; EPS<sub>TTM</sub>(q<sub>t</sub>) = sum of last 4 quarterly diluted EPS filed on or before that week",
      evebit: "EV<sub>week</sub> = weekly close &times; shares + debt &minus; cash &nbsp;&middot;&nbsp; EV/EBIT<sub>week</sub> = EV<sub>week</sub> &divide; EBIT<sub>TTM</sub>(q<sub>t</sub>) &nbsp;&middot;&nbsp; EBIT<sub>TTM</sub>(q<sub>t</sub>) = sum of last 4 quarterly Operating Income; shares / debt / cash use today's snapshot"
    }};

    (function() {{
      const PALETTE = [
        '#6ea8ff', '#22c55e', '#f59e0b', '#ef4444',
        '#a78bfa', '#f472b6', '#14b8a6', '#eab308'
      ];
      const panel     = document.getElementById('chartPanel');
      const body      = document.getElementById('chartBody');
      const legendE   = document.getElementById('chartLegend');
      const selectedE = document.getElementById('chartSelected');
      const closeBtn  = document.getElementById('chartClose');
      const titleE    = document.getElementById('chartTitle');
      const subE      = document.getElementById('chartSub');
      const formulaE  = document.getElementById('chartFormula');
      const tabs        = document.querySelectorAll('.chart-tab');
      const rangeBtns   = document.querySelectorAll('.range-btn');
      const rangeGroupE = document.getElementById('rangeGroup');

      // Range 按钮组语义随 Tab 变化:
      //   P/E / EV·EBIT: 历史区间 1Y/3Y/5Y (曲线时间跨度)
      //   DCF:           显式预测期年数 1/3/5 年 (N)
      // 因此 DCF Tab 下也需要显示 range 组, 不再隐藏。此函数目前作为占位保留,
      // 未来若需要按其他 Tab 语义再隐藏 range 组可以在此处扩展。
      function syncRangeGroupVisibility() {{
        if (!rangeGroupE) return;
        rangeGroupE.classList.remove('hidden');
      }}

      const selected = new Map();   // ticker -> color
      let currentMetric = 'pe';     // 'pe' | 'evebit'
      let currentRange  = '5y';     // '1y' | '3y' | '5y'

      function pickColor() {{
        const used = new Set(selected.values());
        for (const c of PALETTE) if (!used.has(c)) return c;
        return PALETTE[selected.size % PALETTE.length];
      }}

      function fmt(v) {{
        if (v == null || isNaN(v)) return 'N/A';
        return Number(v).toFixed(2);
      }}

      // ---------------- DCF Tab: 自建两阶段 DCF 敏感性网格 ----------------
      //   每个 ticker 一张卡:
      //     基础参数条: FCF₀ (TTM, 来自 stockanalysis) · Shares · Net Debt · Price
      //     滑块: 永续增长率 G ∈ [0%, 5%], 默认 2.5% (~ 名义通胀)
      //     网格: X 轴 WACC ∈ [5%, 10%] 步长 1% (6 列)
      //           Y 轴 g    ∈ [0%, 10%] 步长 1% (11 行, 从上往下: 10% -> 0%)
      //           每格显示 Upside% (每股公允价 vs 现价), 颜色 HSL 红→绿。
      //   预测期年数 N 由 1Y/3Y/5Y 按钮切换 (在 DCF Tab 下按钮语义变为"显式预测期长度")。
      //   数学:  V = Σ_{{t=1..N}} FCF₀·(1+g)^t / (1+WACC)^t
      //             + [FCF_N·(1+G) / (WACC-G)] / (1+WACC)^N
      //          Equity = V - NetDebt;  每股 = Equity / Shares
      // -----------------------------------------------------------------------
      const DCF_WACC_LIST = [0.05, 0.06, 0.07, 0.08, 0.09, 0.10];
      const DCF_G_LIST    = [0.10, 0.09, 0.08, 0.07, 0.06, 0.05, 0.04, 0.03, 0.02, 0.01, 0.00];
      const DCF_G_DEFAULT = 0.025;   // 永续增长率默认值 (名义通胀参考)
      const N_MAP = {{ '1y': 1, '3y': 3, '5y': 5 }};   // range 按钮在 DCF Tab 下的语义

      function fmtMoney(v, digits) {{
        if (v == null || !isFinite(v)) return 'N/A';
        const d = (digits == null) ? (Math.abs(v) >= 100 ? 1 : 2) : digits;
        return '$' + Number(v).toFixed(d);
      }}
      function fmtBig(v) {{
        if (v == null || !isFinite(v)) return 'N/A';
        const a = Math.abs(v);
        const sign = v < 0 ? '-' : '';
        if (a >= 1e12) return sign + (a/1e12).toFixed(2) + 'T';
        if (a >= 1e9)  return sign + (a/1e9).toFixed(2)  + 'B';
        if (a >= 1e6)  return sign + (a/1e6).toFixed(2)  + 'M';
        if (a >= 1e3)  return sign + (a/1e3).toFixed(2)  + 'K';
        return v.toFixed(2);
      }}
      function fmtPct(v, digits) {{
        if (v == null || !isFinite(v)) return 'N/A';
        return Number(v).toFixed(digits == null ? 2 : digits) + '%';
      }}
      function upsidePct(fv, price) {{
        if (fv == null || price == null || !price) return null;
        return (fv - price) / price * 100.0;
      }}
      function upsideSpan(up) {{
        if (up == null || !isFinite(up)) return `<span class="dcf-upside">N/A</span>`;
        const cls = up >= 0 ? 'pos' : 'neg';
        const sig = up >= 0 ? '+' : '';
        return `<span class="dcf-upside ${{cls}}">${{sig}}${{up.toFixed(1)}}%</span>`;
      }}

      // 两阶段 DCF: 返回 breakdown 对象 (供 tooltip 展示明细). terminal 无解时返回 null.
      //   fcf0: 基期自由现金流 (美元原值)
      //   g:    显式预测期年增速
      //   wacc: 折现率
      //   G:    永续增长率
      //   N:    显式预测期年数
      // 返回:
      //   {{ ev, pv, tvPV, fcfN, tvAtN }}
      //     ev    = pv + tvPV              (企业价值)
      //     pv    = Σ FCF_t / (1+wacc)^t   (显式期 PV, t=1..N)
      //     tvAtN = FCF_N·(1+G) / (wacc-G) (第 N 年时点的 Gordon 终值)
      //     tvPV  = tvAtN / (1+wacc)^N     (终值折回今天的 PV)
      //     fcfN  = FCF_0·(1+g)^N          (显式期末年 FCF)
      function dcfEV(fcf0, g, wacc, G, N) {{
        if (fcf0 == null || !isFinite(fcf0)) return null;
        // 显式期折现和
        let pv = 0;
        let fcfN = fcf0;
        for (let t = 1; t <= N; t++) {{
          fcfN = fcfN * (1 + g);   // FCF₀·(1+g)^t 迭代
          pv += fcfN / Math.pow(1 + wacc, t);
        }}
        // Gordon 终值 (WACC 必须严格 > G, 否则模型爆炸)
        if (wacc <= G) return null;
        const tvAtN = fcfN * (1 + G) / (wacc - G);
        const tvPV  = tvAtN / Math.pow(1 + wacc, N);
        return {{ ev: pv + tvPV, pv, tvPV, fcfN, tvAtN }};
      }}
      // 每股公允价 + 明细: 返回 {{ fv, ev, pv, tvPV, fcfN, tvAtN }}; 缺 shares/EV 时返回 null.
      // 保留 .fv 字段方便调用方直接读单一数值 (与旧接口语义对齐).
      function dcfPerShare(fcf0, g, wacc, G, N, shares, netDebt) {{
        const bd = dcfEV(fcf0, g, wacc, G, N);
        if (bd == null || shares == null || !shares) return null;
        const fv = (bd.ev - (netDebt || 0)) / shares;
        return {{ fv, ev: bd.ev, pv: bd.pv, tvPV: bd.tvPV, fcfN: bd.fcfN, tvAtN: bd.tvAtN }};
      }}

      // 上涨空间百分比 → 颜色 (与全站 strong/weak 图例对齐):
      //   Upside > 0 (低估, cheap/strong) → --good (#22c55e, 绿)
      //   Upside < 0 (高估, rich/weak)   → --bad  (#ef4444, 红)
      // Alpha 随 |up|/50 从 0.10 递增到 0.55, 越极端越深, 便于扫读。
      function upsideToColor(up) {{
        if (up == null || !isFinite(up)) return 'transparent';
        let x = up / 50;
        if (x >  1) x =  1;
        if (x < -1) x = -1;
        const alpha = (0.10 + 0.45 * Math.abs(x)).toFixed(3);
        // 与 CSS 里的 --good / --bad 保持同源 RGB
        return x >= 0
          ? `rgba(34, 197, 94, ${{alpha}})`   // good
          : `rgba(239, 68, 68, ${{alpha}})`;  // bad
      }}

      // 用过去 N 年 (N∈{{1,3,5}}) 的 TTM FCF 数据算历史 CAGR。
      //   fcfSeries: 新→旧 TTM 序列。
      //   fcfDates:  与 series 一一对应的 'YYYY-MM-DD' 字符串数组 (可为空: 退化为按下标查找)。
      //   freq:      'quarterly' | 'semiannual' | 'annual' (仅在 fcfDates 缺失时用作步长回退)。
      // 策略:
      //   有 fcfDates → 找"距离 series[0] 恰好 N*365.25 天"的那个下标 (取最接近的 index),
      //                 用它的实际跨度做 CAGR (years = 真实天数 / 365.25)。这样能正确处理
      //                 9992 这种"早期季度、近期半年"的混合披露模式。
      //   无 fcfDates → 回退到 stepPerYear 静态查找。
      //   端点 ≤ 0    → 返回 n/m (跨零点几何无意义)。
      //   数据不足    → 返回 null (前端显示 "N/A")。
      function fcfCagrPastN(fcfSeries, N, fcfDates, freq) {{
        if (!Array.isArray(fcfSeries) || fcfSeries.length < 2) return null;
        let startIdx, years;
        const hasDates = Array.isArray(fcfDates) && fcfDates.length === fcfSeries.length && fcfDates[0];
        if (hasDates) {{
          const t0 = Date.parse(fcfDates[0]);
          if (!isFinite(t0)) return null;
          const targetGapMs = N * 365.25 * 86400 * 1000;
          // 找 |gap - target| 最小的下标 (i>=1)
          let bestI = -1, bestDiff = Infinity;
          for (let i = 1; i < fcfDates.length; i++) {{
            const ti = Date.parse(fcfDates[i]);
            if (!isFinite(ti)) continue;
            const gap  = t0 - ti;
            const diff = Math.abs(gap - targetGapMs);
            if (diff < bestDiff) {{ bestDiff = diff; bestI = i; }}
          }}
          if (bestI < 0) return null;
          const gapDays  = (t0 - Date.parse(fcfDates[bestI])) / 86400000;
          const gapYears = gapDays / 365.25;
          // 精度判定: 找到的点必须"接近"目标 N 年 (差 ≤ 0.6 年); 否则视为数据不足
          // (例外: N=5 时容忍 4 年以上, 允许 4.75 年这种近似)
          const minYears = (N === 5) ? 4.0 : (N - 0.6);
          const maxYears = N + 0.6;
          if (gapYears < minYears || gapYears > maxYears) return null;
          startIdx = bestI;
          years    = gapYears;
        }} else {{
          const stepPerYear =
                (freq === 'annual')     ? 1
              : (freq === 'semiannual') ? 2
              : 4;
          const idealIdx = N * stepPerYear;
          if (idealIdx < fcfSeries.length) {{
            startIdx = idealIdx; years = N;
          }} else if (N === 5 && fcfSeries.length >= 3) {{
            startIdx = fcfSeries.length - 1;
            years    = startIdx / stepPerYear;
            if (years < 3) return null;
          }} else {{
            return null;
          }}
        }}
        const now  = fcfSeries[0];
        const then = fcfSeries[startIdx];
        if (now == null || then == null) return null;
        if (now <= 0 && then <= 0) return {{ nm: true, nmReason: 'both', now, then, years }};
        if (now <= 0)              return {{ nm: true, nmReason: 'now',  now, then, years }};
        if (then <= 0)             return {{ nm: true, nmReason: 'then', now, then, years }};
        const cagr = Math.pow(now / then, 1 / years) - 1;
        return {{ cagr, now, then, years, startIdx }};
      }}

      // 生成一张 DCF 卡的 HTML (静态骨架, 网格由 dcfRecomputeCard 首次填充)
      function renderDcfCard(ticker) {{
        const dcf   = DCF_DATA[ticker];
        const snap  = SNAPSHOT[ticker] || {{}};
        const price = snap.close;
        const cur   = (dcf && dcf.currency) || snap.currency || '';
        const isETF = ETF_SET.has(ticker);

        if (isETF) {{
          return `<div class="dcf-card dcf-empty" data-ticker="${{ticker}}">`
               +   `<div class="dcf-card-head"><div class="dcf-title"><b>${{ticker}}</b> <span class="dcf-sub">ETF</span></div></div>`
               +   `<div class="dcf-empty-msg">ETF 组合层面无 DCF 概念, 未做建模。</div>`
               + `</div>`;
        }}
        if (!dcf || dcf.fcf_ttm == null) {{
          return `<div class="dcf-card dcf-empty" data-ticker="${{ticker}}">`
               +   `<div class="dcf-card-head"><div class="dcf-title"><b>${{ticker}}</b> <span class="dcf-sub">${{cur||''}}</span></div></div>`
               +   `<div class="dcf-empty-msg">`
               +     `未抓到基期 <b>FCF (TTM)</b>. 可能是 stockanalysis 的 `
               +     `<code>/financials/cash-flow-statement/?p=trailing</code> 页面结构变化,`
               +     `或该 ticker 无 TTM 数据.`
               +   `</div>`
               + `</div>`;
        }}
        const fcf0    = dcf.fcf_ttm;
        const shares  = snap.shares;
        const netDebt = (snap.debt || 0) - (snap.cash || 0);
        const asof    = dcf.asof || '';
        const url     = dcf.source_url || '#';
        // 币种换算 (方案 A): 报表币 -> 报价币 (port stats/close/netDebt 已经是报价币)
        //   fx=1.0: 报表币==报价币 (US 大多数 / 港资港股) 或 老快照无此字段 (向后兼容)
        //   fx>1  : 常见如 CNY→HKD ≈ 1.11-1.14
        const reportCcy = dcf.report_currency || '';
        const fx        = (typeof dcf.fx_to_quote === 'number' && isFinite(dcf.fx_to_quote))
                          ? dcf.fx_to_quote : 1.0;
        const needFx    = reportCcy && reportCcy !== cur && Math.abs(fx - 1) > 1e-9;
        // 参数条: 若需换算, FCF₀ 显示 "98.80B CNY (≈ 110.00B HKD)"; 否则只显示原值
        const fcf0OrigTxt = needFx
          ? `${{fmtBig(fcf0)}} ${{reportCcy}} (≈ ${{fmtBig(fcf0 * fx)}} ${{cur}})`
          : `${{fmtBig(fcf0)}}${{cur ? ' ' + cur : ''}}`;

        // 汇率徽标 (仅在需要换算时显示, 靠右, Current 左侧)
        //   例如: "FX 1 CNY ≈ 1.1128 HKD"  hover 显示 asof / source
        const fxBadge = needFx
          ? `<span class="dcf-fx-badge" title="${{
                  ('FX asof ' + (dcf.fx_asof || 'n/a')
                   + '\\n' + (dcf.fx_source || '反算')
                   + '\\n假设未来所有年份汇率恒定 = 今日即期'
                  ).replace(/"/g, '&quot;')
             }}">FX 1 ${{reportCcy}} ≈ ${{fx.toFixed(4)}} ${{cur}}</span>`
          : '';

        // 卡骨架: 参数条 + 滑块 + 网格容器 + footer
        const gPct = (DCF_G_DEFAULT * 100).toFixed(1);
        return `<div class="dcf-card" data-ticker="${{ticker}}">`
             +   `<div class="dcf-card-head">`
             +     `<div class="dcf-title"><b>${{ticker}}</b> <span class="dcf-sub">${{cur||''}} · N=<span class="dcf-n">?</span>y</span></div>`
             +     `<div class="dcf-head-right">`
             +       fxBadge
             +       `<span class="dcf-sub">Current ${{fmtMoney(price)}}</span>`
             +     `</div>`
             +   `</div>`
             +   `<div class="dcf-params">`
             +     `<span>FCF₀ <b>${{fcf0OrigTxt}}</b></span>`
             +     `<span>Shares <b>${{fmtBig(shares)}}</b></span>`
             +     `<span>Net Debt <b>${{fmtBig(netDebt)}}</b> ${{cur||''}}</span>`
             +     `<a class="src" href="${{url}}" target="_blank" rel="noopener">source · ${{asof || 'TTM'}}</a>`
             +   `</div>`
             +   `<div class="dcf-slider-row">`
             +     `<span class="lbl"><b>Perpetual g (G)</b></span>`
             +     `<input type="range" class="dcf-g-slider" min="0" max="5" step="0.1" value="${{gPct}}" aria-label="Perpetual growth rate">`
             +     `<span class="val dcf-g-val">${{gPct}}%</span>`
             +   `</div>`
             +   `<div class="dcf-heatmap-wrap"><!-- filled by dcfRecomputeCard --></div>`
             +   `<div class="dcf-footer"><!-- filled by dcfRecomputeCard --></div>`
             + `</div>`;
      }}

      // 根据当前 range (=N) 和当前滑块 G 值, 重算一张卡的网格 + footer。
      // cardEl: .dcf-card DOM;  N: 显式期年数
      function dcfRecomputeCard(cardEl, N) {{
        const ticker = cardEl.getAttribute('data-ticker');
        const dcf    = DCF_DATA[ticker];
        if (!dcf || dcf.fcf_ttm == null) return;   // 空态卡不重算
        const snap    = SNAPSHOT[ticker] || {{}};
        const cur       = (dcf && dcf.currency) || snap.currency || '';
        const reportCcy = dcf.report_currency || '';
        const fx        = (typeof dcf.fx_to_quote === 'number' && isFinite(dcf.fx_to_quote))
                          ? dcf.fx_to_quote : 1.0;
        const needFx    = reportCcy && cur && reportCcy !== cur && Math.abs(fx - 1) > 1e-9;
        const fcf0Orig = dcf.fcf_ttm;               // 报表原币 FCF₀
        const fcf0    = fcf0Orig * fx;              // 换算后的报价币 FCF₀ (与 price/netDebt 同币)
        const shares  = snap.shares;
        const netDebt = (snap.debt || 0) - (snap.cash || 0);
        const price   = snap.close;

        // 币种说明块 (追加到每格 tooltip 末尾): 说明 FCF₀ 是否被换算过, 用了什么汇率
        // 学术假设明确写出, 让用户知道未来所有年份都用今日即期
        let currencyTip = '';
        if (needFx) {{
          const fxAsofTxt   = dcf.fx_asof   || 'n/a';
          const fxSourceTxt = dcf.fx_source || '反算';
          currencyTip = `\n────── Currency ──────`
                     + `\nReport ccy: ${{reportCcy}}   (FCF 原币)`
                     + `\nQuote ccy:  ${{cur}}   (股价 / 净债务)`
                     + `\nFX (spot):  1 ${{reportCcy}} ≈ ${{fx.toFixed(4)}} ${{cur}}`
                     + `\nFX asof:    ${{fxAsofTxt}}`
                     + `\nFX source:  ${{fxSourceTxt}}`
                     + `\n假设:       未来所有年份汇率恒定 = 今日即期`;
        }} else if (dcf.fx_note) {{
          // 报表币 != 报价币, 但反算失败, 明确警告用户 DCF 结果可能失真
          currencyTip = `\n────── Currency ──────\n${{dcf.fx_note}}`;
        }}
        // 读滑块
        const slider = cardEl.querySelector('.dcf-g-slider');
        const gPct   = slider ? parseFloat(slider.value) : (DCF_G_DEFAULT * 100);
        const G      = gPct / 100;
        // 更新滑块显示 + N 显示
        const valE = cardEl.querySelector('.dcf-g-val');
        if (valE) valE.textContent = gPct.toFixed(1) + '%';
        const nE = cardEl.querySelector('.dcf-n');
        if (nE) nE.textContent = String(N);

        // 计算 1/3/5 年历史 CAGR (用于 tooltip); 优先按 fcf_dates 精确匹配 N 年前那期
        const freq  = dcf.frequency || 'quarterly';
        const dts   = dcf.fcf_dates || [];
        const freqLabel = (freq === 'annual') ? '年度' : (freq === 'semiannual' ? '半年度' : '季度');
        const cagr1 = fcfCagrPastN(dcf.fcf_series, 1, dts, freq);
        const cagr3 = fcfCagrPastN(dcf.fcf_series, 3, dts, freq);
        const cagr5 = fcfCagrPastN(dcf.fcf_series, 5, dts, freq);
        // 当前 N 对应的历史 CAGR (决定是否插入 actual 行)
        const cagrCur = (N === 1) ? cagr1 : (N === 3 ? cagr3 : cagr5);
        // 参考列 tooltip - 显示三档 CAGR + 明确的计算方法说明
        // 单位换算: FCF 原值为美元 (港股为港币), 大数用 B (十亿) 缩写
        function bn(v) {{
          if (v == null || !isFinite(v)) return 'n/a';
          const abs = Math.abs(v);
          if (abs >= 1e9)  return (v/1e9).toFixed(2) + 'B';
          if (abs >= 1e6)  return (v/1e6).toFixed(2) + 'M';
          return v.toFixed(2);
        }}
        function cagrLabel(cg, N) {{
          if (cg == null) return `N/A (数据不足 ${{N}} 年)`;
          if (cg.nm) {{
            // 明确告诉用户是哪个端点为负 -> 触发 n/m
            if (cg.nmReason === 'now')  return `n/m (当前 TTM = ${{bn(cg.now)}} ≤ 0)`;
            if (cg.nmReason === 'then') return `n/m (${{N}}Y 前那期 = ${{bn(cg.then)}} ≤ 0)`;
            return `n/m (两端均 ≤ 0: now=${{bn(cg.now)}}, ${{N}}Y前=${{bn(cg.then)}})`;
          }}
          const pct = (cg.cagr >= 0 ? '+' : '') + (cg.cagr * 100).toFixed(1) + '%';
          return `${{pct}}  (${{bn(cg.then)}} → ${{bn(cg.now)}})`;
        }}
        // 采样口径: 优先按 fcf_dates 精确匹配, 否则按 freq 粗略步长
        const seriesLen = (dcf.fcf_series || []).length;
        const sampleDescr =
              (dts && dts.length === seriesLen && dts[0])
            ? `按 ${{freqLabel}} TTM 序列, 根据 datekey 匹配“最接近 N 年前”那期`
            : `按 ${{freqLabel}} TTM 序列, 升序下标定位`;
        function idxOf(cg) {{ return (cg && cg.startIdx != null) ? cg.startIdx : '?'; }}
        function yrsOf(cg) {{ return (cg && cg.years != null) ? cg.years.toFixed(2) : '?'; }}
        const actualTip =
            `Past FCF CAGR — 端点法\n`
          + `公式:  CAGR = (FCF₀ / FCF₋ₙ)^(1/N) − 1\n`
          + `采样:  ${{sampleDescr}}\n`
          + `        1Y ← series[${{idxOf(cagr1)}}]  (实际跨度 ${{yrsOf(cagr1)}} 年)\n`
          + `        3Y ← series[${{idxOf(cagr3)}}]  (实际跨度 ${{yrsOf(cagr3)}} 年)\n`
          + `        5Y ← series[${{idxOf(cagr5)}}]  (实际跨度 ${{yrsOf(cagr5)}} 年)\n`
          + `\n`
          + `  1y: ${{cagrLabel(cagr1, 1)}}\n`
          + `  3y: ${{cagrLabel(cagr3, 3)}}\n`
          + `  5y: ${{cagrLabel(cagr5, 5)}}\n`
          + `\n`
          + `n/m: 端点 ≤ 0 时 CAGR 几何无意义 (跨零点)\n`
          + `N/A: ${{freqLabel}} TTM 序列长度不足 N 年`;

        // 构造完整行列表: 常规整数档 + 可能的 actual 插值行
        // Actual g 处理策略:
        //   1) CAGR ∈ (0%, 10%) 相邻两档之间       → 插入 mid 位置
        //   2) CAGR 几乎等于某整数档 (差 <0.05pp)   → 不新增行, 标记该整数档 hit
        //   3) CAGR ≥ 10%  (超上限, 含正好 10%)     → 在顶部 g=10% 行之上插入 (Y 轴向上延伸)
        //   4) CAGR ≤ 0%   (超下限, 含 0% 与负数)   → 在底部 g=0% 行之下插入 (Y 轴向下延伸)
        //   5) n/m 或 N/A                            → 完全不插入, 不标 hit
        let actualG = null;                          // 需要作为独立行插入的 CAGR (含超界)
        let actualPos = null;                        // 'top' | 'bottom' | 'mid'
        let hitInteger = null;                       // 若 CAGR 恰等于某整数档, 记录该 g
        if (cagrCur && !cagrCur.nm && isFinite(cagrCur.cagr)) {{
          const c = cagrCur.cagr;
          // 先检查是否几乎等于某整数档 (容差 0.05pp)
          let snapped = null;
          for (const g of DCF_G_LIST) {{
            if (Math.abs(g - c) < 0.0005) {{ snapped = g; break; }}
          }}
          if (snapped != null) {{
            hitInteger = snapped;                    // 情况 2
          }} else if (c > 0 && c < 0.10) {{
            actualG = c; actualPos = 'mid';          // 情况 1
          }} else if (c >= 0.10) {{
            actualG = c; actualPos = 'top';          // 情况 3
          }} else {{
            actualG = c; actualPos = 'bottom';       // 情况 4 (c ≤ 0)
          }}
        }}
        // 按 g 从高到低排列 DCF_G_LIST (0.10 → 0.00), 按 actualPos 决定在何处插入独立行
        const rows = [];
        // top: 插在最顶
        if (actualG != null && actualPos === 'top') {{
          rows.push({{ g: actualG, isActual: true, outOfRange: 'above' }});
        }}
        for (let i = 0; i < DCF_G_LIST.length; i++) {{
          const g = DCF_G_LIST[i];
          rows.push({{
            g,
            isActual: false,
            isHit: hitInteger != null && Math.abs(g - hitInteger) < 1e-9,
          }});
          // mid: 若 actualG 落在当前 g 和下一个 g 之间 (g > actualG > nextG), 紧接着插入
          if (actualG != null && actualPos === 'mid' && i + 1 < DCF_G_LIST.length) {{
            const nextG = DCF_G_LIST[i + 1];
            if (g > actualG && actualG > nextG) {{
              rows.push({{ g: actualG, isActual: true }});
            }}
          }}
        }}
        // bottom: 插在最底
        if (actualG != null && actualPos === 'bottom') {{
          rows.push({{ g: actualG, isActual: true, outOfRange: 'below' }});
        }}

        // 生成网格 HTML
        // 表头: 空角 + WACC 数据列 (Actual g 参考列已并入 Y 轴刻度, 不再单独占列)
        let html = `<table class="dcf-heatmap">`
                 + `<thead><tr><th class="corner"><span class="arrow">g ↓ / WACC →</span></th>`;
        for (const w of DCF_WACC_LIST) {{
          html += `<th>${{(w*100).toFixed(0)}}%</th>`;
        }}
        html += `</tr></thead><tbody>`;

        // 中位格 (WACC=8% + g=中位 5%) 供 footer 用
        let midFV = null;
        for (const row of rows) {{
          const g = row.g;
          // 行头 g 值: 插值行显示一位小数 (带箭头), hit 行 (CAGR 正好命中整数档) 加 "= "
          // 前缀以强调 "此档即历史 CAGR", 避免与普通整数档视觉混淆 (例如 META 5Y CAGR≈3%
          // 会被 snap 到 3% 档, 若不加视觉标识, Y 轴上根本看不出历史 CAGR 在哪里)。
          let gHeadTxt;
          if (row.isActual) {{
            const num = (g*100).toFixed(1) + '%';
            if (row.outOfRange === 'above')      gHeadTxt = '↑ ' + num;
            else if (row.outOfRange === 'below') gHeadTxt = '↓ ' + num;
            else                                 gHeadTxt = num;
          }} else if (row.isHit) {{
            gHeadTxt = '= ' + (g*100).toFixed(0) + '%';
          }} else {{
            gHeadTxt = (g*100).toFixed(0) + '%';
          }}
          // isHit 行复用 actual-row 样式 (accent 色行头 + 边框), 保证与"插值 actual 行"
          // 在视觉上一致, 用户一眼即可锁定历史 CAGR 对应档.
          const trClsList = [];
          if (row.isActual || row.isHit) trClsList.push('actual-row');
          if (row.outOfRange)            trClsList.push('out-of-range');
          const trCls = trClsList.length ? ` class="${{trClsList.join(' ')}}"` : '';
          // 行头 g 值本身即承担了历史 CAGR 的可读性 (插值行/hit 行由 CSS 高亮), 无需额外参考列
          // 仅在 actual/hit 行的行头挂 CAGR 三档 tooltip
          const thTip = (row.isActual || row.isHit)
            ? ` title="${{actualTip.replace(/"/g,'&quot;')}}"` : '';
          html += `<tr${{trCls}}><th${{thTip}}>${{gHeadTxt}}</th>`;
          for (const w of DCF_WACC_LIST) {{
            const res = dcfPerShare(fcf0, g, w, G, N, shares, netDebt);
            if (res == null) {{
              html += `<td class="hm err" title="WACC ≤ G, terminal value undefined">—</td>`;
            }} else {{
              const fv = res.fv;
              const up = upsidePct(fv, price);
              const bg = upsideToColor(up);
              const upTxt = (up == null || !isFinite(up)) ? 'N/A'
                          : (up >= 0 ? '+' : '') + up.toFixed(0) + '%';
              const gLabel = row.isActual ? (g*100).toFixed(1) : (g*100).toFixed(0);
              // Actual 后缀: mid 简写 "actual past Ny", 超界额外说明; hit 行 (CAGR 命中整数档) 同样标注
              let actualSuffix = '';
              if (row.isActual) {{
                if      (row.outOfRange === 'above') actualSuffix = ` (actual past ${{N}}y, above grid)`;
                else if (row.outOfRange === 'below') actualSuffix = ` (actual past ${{N}}y, below grid)`;
                else                                  actualSuffix = ` (actual past ${{N}}y)`;
              }} else if (row.isHit) {{
                actualSuffix = ` (= actual past ${{N}}y)`;
              }}
              // Breakdown: 显式期 PV / 终值 (第 N 年时点 tvAtN + 折回今日 tvPV) / EV / 每股
              // 终值占比 = tvPV / ev, 有助于识别"公允价过度依赖遥远未来"的情形.
              const tvShare = (res.ev > 0) ? (res.tvPV / res.ev * 100) : null;
              const tvShareTxt = (tvShare == null || !isFinite(tvShare)) ? 'N/A' : tvShare.toFixed(0) + '%';
              const netDebtTxt = fmtBig(netDebt || 0);
              const sharesTxt  = fmtBig(shares);
              const tip = `WACC=${{(w*100).toFixed(0)}}%, g=${{gLabel}}%${{actualSuffix}}, G=${{gPct.toFixed(1)}}%, N=${{N}}y`
                        + `\n────── Cash Flow ──────`
                        + (needFx
                            ? `\nFCF₀ (raw)  ${{fmtBig(fcf0Orig)}} ${{reportCcy}}`
                            + `\nFCF₀ (${{cur}})  ${{fmtBig(fcf0)}}   = FCF₀ (raw) × ${{fx.toFixed(4)}}`
                            : `\nFCF₀        ${{fmtBig(fcf0)}}`)
                        + `\nFCF_N       ${{fmtBig(res.fcfN)}}   = FCF₀·(1+g)^N`
                        + `\n────── Present Value ──────`
                        + `\n显式期 PV   ${{fmtBig(res.pv)}}   (Σ FCF_t/(1+WACC)^t)`
                        + `\n终值 @N     ${{fmtBig(res.tvAtN)}}   = FCF_N·(1+G)/(WACC−G)`
                        + `\n终值 PV     ${{fmtBig(res.tvPV)}}   (占 EV ${{tvShareTxt}})`
                        + `\n────── Valuation ──────`
                        + `\nEV          ${{fmtBig(res.ev)}}   = 显式期 PV + 终值 PV`
                        + `\n− Net Debt  ${{netDebtTxt}}`
                        + `\n÷ Shares    ${{sharesTxt}}`
                        + `\nFair value: ${{fmtMoney(fv)}}`
                        + `\nCurrent:    ${{fmtMoney(price)}}`
                        + `\nUpside:     ${{upTxt}}`
                        + currencyTip
                        + (row.outOfRange ? `\n\n⚠ 此行 g 超出常规网格 [0%, 10%], 仅供参考` : '');
              html += `<td class="hm" style="background:${{bg}}" title="${{tip}}">${{upTxt}}</td>`;
              // 记录中位 (WACC=8%, g=5%) — 只从整数档取
              if (!row.isActual && w === 0.08 && Math.abs(g - 0.05) < 1e-9) midFV = fv;
            }}
          }}
          html += `</tr>`;
        }}
        html += `</tbody></table>`;

        const wrap = cardEl.querySelector('.dcf-heatmap-wrap');
        if (wrap) wrap.innerHTML = html;

        // Footer: 中位 (WACC=8%, g=5%) 的公允价与 upside + 历史 CAGR 摘要
        const footer = cardEl.querySelector('.dcf-footer');
        if (footer) {{
          const upMid = upsidePct(midFV, price);
          // 高亮当前 N 对应的档; 副行显示端点数值 (FCF₋ₙ → FCF₀), 便于验证 CAGR 来源
          // 颜色语义: cagr >= 0 → 绿 (pos), cagr < 0 → 红 (neg, 真·负增长, 两端点都>0);
          //          n/m 保持 muted 灰 (端点 ≤ 0, 几何无意义)
          function cagrChip(label, cg, N, isCur) {{
            // 主行: 百分比 (或 n/m / N/A) — 保持紧凑
            let head;
            let sign = 'nm';                        // 'pos' | 'neg' | 'nm' (na/nm 走 nm 分支保持灰色)
            if (cg == null) {{
              head = 'N/A';
            }} else if (cg.nm) {{
              head = 'n/m';
            }} else {{
              head = (cg.cagr >= 0 ? '+' : '') + (cg.cagr * 100).toFixed(1) + '%';
              sign = cg.cagr >= 0 ? 'pos' : 'neg';  // 真·负增长 → 红色
            }}
            // 副行: 端点数值 then → now (nm 时也显示, 让用户看到跨零具体位置)
            // 当前档 (isCur) 且 sign 有色时高亮同色, 让用户一眼锁定"当前 N"档且看到方向
            let sub = '';
            if (cg != null) {{
              const epCls = (isCur && sign !== 'nm')
                ? `dcf-cagr-endpoints ${{sign}}`
                : 'dcf-cagr-endpoints';
              sub = `<span class="${{epCls}}">${{bn(cg.then)}} → ${{bn(cg.now)}}</span>`;
            }}
            // 百分比着色: 当前档 pos/neg 走绿/红, 其他档保持 muted
            const cls = (isCur && sign !== 'nm') ? `dcf-upside ${{sign}}` : '';
            const style = isCur ? '' : 'color:var(--muted)';
            // 挂 title 兜底一层, hover 时看到完整算式
            const tip = (cg == null)
              ? `${{N}}y: 数据不足 ${{N*4}} 期`
              : (cg.nm
                  ? `${{N}}y: n/m — ${{cg.nmReason==='now' ? '当前 TTM ≤ 0' : cg.nmReason==='then' ? `${{N}}Y 前那期 ≤ 0` : '两端均 ≤ 0'}} (then=${{bn(cg.then)}}, now=${{bn(cg.now)}})`
                  : `${{N}}y CAGR = (${{bn(cg.now)}} / ${{bn(cg.then)}})^(1/${{N}}) − 1 = ${{(cg.cagr*100).toFixed(2)}}%`);
            const chipCls = isCur ? 'dcf-cagr-chip current' : 'dcf-cagr-chip';
            return `<span class="${{chipCls}}" style="${{style}}" title="${{tip.replace(/"/g,'&quot;')}}">`
                 +   `<span class="dcf-cagr-label">${{label}}</span> <span class="${{cls}}">${{head}}</span>`
                 +   sub
                 + `</span>`;
          }}
          footer.innerHTML =
              `<span>Mid (W=8%, g=5%, G=${{gPct.toFixed(1)}}%, N=${{N}}y): `
            +   `<b>${{fmtMoney(midFV)}}</b> · ${{upsideSpan(upMid)}}</span>`
            + `<span>Current: <b>${{fmtMoney(price)}}</b></span>`
            + `<span style="width:100%;height:0"></span>`
            + `<span style="color:var(--muted)">Actual FCF CAGR:</span>`
            + cagrChip('1y', cagr1, 1, N===1)
            + cagrChip('3y', cagr3, 3, N===3)
            + cagrChip('5y', cagr5, 5, N===5);
        }}
      }}

      // 触发所有已渲染 DCF 卡的重算 (滑块变动 / range 切换)
      function dcfRecomputeAll() {{
        const N = N_MAP[currentRange] || 5;
        document.querySelectorAll('.dcf-card[data-ticker]').forEach(el => {{
          dcfRecomputeCard(el, N);
        }});
      }}

      function renderDcfTab() {{
        titleE.textContent = METRIC_META.dcf.title;

        // 空选中集合: 与其他 Tab 行为一致——关闭悬浮面板, 让 Close 按钮生效。
        if (selected.size === 0) {{
          panel.classList.remove('open');
          panel.setAttribute('aria-hidden', 'true');
          body.innerHTML = '';
          legendE.innerHTML = '';
          selectedE.innerHTML = '';
          if (formulaE) {{ formulaE.classList.remove('show'); formulaE.innerHTML = ''; }}
          document.querySelectorAll('.tk-badge').forEach(b => {{
            b.classList.remove('selected');
            b.style.removeProperty('--sel');
          }});
          return;
        }}

        panel.classList.add('open');
        panel.setAttribute('aria-hidden', 'false');

        // 徽章选中态: 前置到任何内容渲染之前, 保证即使后续 DCF 网格出错也不影响高亮。
        document.querySelectorAll('.tk-badge').forEach(b => {{
          const t = b.dataset.ticker;
          b.classList.toggle('selected', selected.has(t));
          if (selected.has(t)) b.style.setProperty('--sel', selected.get(t));
          else b.style.removeProperty('--sel');
        }});

        // 副标题: 数据源 + 抓取时间 (取任一 ticker 的 asof)
        let asof = null;
        for (const t of selected.keys()) {{
          const d = DCF_DATA[t];
          if (d && d.asof) {{ asof = d.asof; break; }}
        }}
        const N = N_MAP[currentRange] || 5;
        subE.textContent =
          `Two-stage DCF sensitivity · WACC × g heatmap` +
          ` · N=${{N}} explicit years · FCF₀ from stockanalysis.com TTM` +
          (asof ? ` · ${{asof}}` : '');

        // 渲染每张卡骨架, 然后统一触发重算填充网格
        const cards = Array.from(selected.keys()).map(renderDcfCard).join('');
        body.innerHTML = `<div class="dcf-grid">${{cards}}</div>`;
        legendE.innerHTML = '';
        selectedE.innerHTML = Array.from(selected.entries()).map(([t, c]) =>
          `<span class="sel-chip" style="border-color:${{c}};color:${{c}}">${{logoImg(t)}}<span class="tk-sym">${{t}}</span></span>`
        ).join('');

        // 绑定每张卡的滑块 input 事件 → 只重算这张卡 (不重画其他)
        body.querySelectorAll('.dcf-card[data-ticker]').forEach(el => {{
          const slider = el.querySelector('.dcf-g-slider');
          if (slider) {{
            slider.addEventListener('input', () => {{
              dcfRecomputeCard(el, N_MAP[currentRange] || 5);
            }});
          }}
        }});

        // 首次填充所有网格
        dcfRecomputeAll();

        // 说明条: 精简版, 只保留核心公式 / 图表读法 / 数据来源与关键局限
        formulaE.classList.add('show');
        formulaE.innerHTML =
          `<span class="cf-tag">MODEL</span>` +
          `<b>两阶段 DCF</b>: 显式期 N 年内 FCF 按 g 复合增长, 之后按永续增长率 G 增长.` +
          `<br/><code>V = Σ FCF₀·(1+g)ᵗ/(1+WACC)ᵗ + [FCF_N·(1+G)/(WACC−G)]/(1+WACC)ᴺ</code>` +
          `<br/>每股公允价 = (V − Net Debt) / Shares. WACC ≤ G 时终值发散, 显示 —.` +
          `<br/><br/>` +
          `<span class="cf-tag">GRID</span>` +
          `<b>X 轴 WACC</b> 5%→10%, <b>Y 轴 g</b> 0%→10%, 每格显示 <b>Upside% = (FV − 现价)/现价</b> ` +
          `(绿=低估, 红=高估, 与主表 strong/weak 同源). 悬停看绝对公允价.` +
          `<br/>` +
          `<b>历史 CAGR</b> (过去 N 年 FCF 复合增速) 直接插入 Y 轴对应位置: ` +
          `∈(0%,10%) 插入两档之间; ≈整数档标 <b>hit</b>; 超 10% 置顶↑, ≤0% 置底↓ (虚线边框, 仅供参考); ` +
          `FCF 跨零点为 <b>n/m</b> 不插入. 悬停行头看 1Y/3Y/5Y 三档 + 计算方法.` +
          `<br/><br/>` +
          `<span class="cf-tag">METHOD · CAGR</span>` +
          `<b>端点法</b>: <code>CAGR = (FCF₀ / FCF₋ₙ)^(1/N) − 1</code>. ` +
          `采样自 stockanalysis 季度 TTM 序列, 只看首尾两点: ` +
          `<b>1Y</b> 取 4 季度前, <b>3Y</b> 取 12 季度前, <b>5Y</b> 取 19 季度前 (≈ 4.75 年, 近似当 5 年). ` +
          `<b>n/m</b> 触发条件: 任一端点 FCF ≤ 0 (跨零点 CAGR 无几何意义), tooltip 会指明是当前 TTM 还是 N 年前那期为负. ` +
          `<b>N/A</b>: 数据源仅返回少于 N·4 个季度. ` +
          `<i>局限</i>: 端点法对采样时点敏感, 单个季度极端值会明显放大/压低结果 — 仅作参考锚点, 不代表未来。` +
          `<br/><br/>` +
          `<span class="cf-tag">CONTROLS</span>` +
          `顶部 <b>1Y/3Y/5Y</b> = 显式预测期 N; 滑块 <b>G ∈ [0%, 5%]</b> = 永续增长率 (默认 2.5%, ~名义通胀).` +
          `<br/><br/>` +
          `<span class="cf-tag">DATA & LIMITS</span>` +
          `FCF₀ 来自 stockanalysis.com TTM 现金流表; 股本 / 债务 / 现金 / 现价来自本报告主表快照. ` +
          `本网格是<b>假设驱动的 what-if</b>, 不是历史序列 (那是 P/E Tab). ` +
          `FCF ≤ 0 时全表为负数不代表被高估; ETF 无 DCF, Tab 自动隐藏. 仅供参考, 不构成投资建议.`;
      }}

      function render() {{
        // 每次渲染前先同步 Tab 可见性——选中集合发生变化时（新增/移除 ETF）也需要立即反映。
        syncTabVisibility();

        const bucket  = HISTORY_DATA[currentRange] || {{}};
        const dataMap = bucket[currentMetric] || {{}};
        const meta    = METRIC_META[currentMetric];
        const rmeta   = RANGE_META[currentRange];
        titleE.textContent = meta.title;

        // DCF Tab: 自建两阶段 DCF 敏感性网格 (WACC × g 热图 + 永续增长率 G 滑块)。
        // range 按钮语义在此 Tab 下变为"显式预测期年数 N" (1Y=1, 3Y=3, 5Y=5)。
        // 每个选中的 ticker 一张卡: 参数条 + 滑块 + WACC×g 网格 + 底部 mid 小结。
        if (currentMetric === 'dcf') {{
          renderDcfTab();
          return;
        }}

        // 副标题：range · freq · 各 ticker 的数据源。曲线所需的数据源可能不同
        // （个股走 stockanalysis.com、ETF 走 multpl / siblis 指数代理），因此按当前
        // 选中且实际有序列的 ticker 汇总去重，让"每条曲线都有出处"这件事在 UI 上
        // 直接看得见。P/E metric 才展示数据源来源（EV/EBIT 数据源统一，无需分别标）。
        let subText = `${{rmeta.label}} · ${{rmeta.freq}}`;
        if (currentMetric === 'pe' && selected.size > 0) {{
          const seen = new Set();
          const parts = [];
          for (const t of selected.keys()) {{
            const src = PE_SOURCE[t];
            if (!src) continue;
            if (seen.has(src)) continue;
            seen.add(src);
            // 同一 source 被多个 ticker 共用时，用 "AAPL,MSFT: source" 前缀标出归属
            const owners = Array.from(selected.keys()).filter(x => PE_SOURCE[x] === src);
            parts.push(`${{owners.join(',')}}: ${{src}}`);
          }}
          if (parts.length) subText += ` · ${{parts.join(' | ')}}`;
        }} else if (currentMetric === 'evebit') {{
          subText += ` · source stockanalysis.com`;
        }}
        subE.textContent = subText;

        // 说明条：合并两类提示——1Y 本地反算公式 + ETF 指数代理 / EV/EBIT N/A 提醒
        const notes = [];
        // "本地反算"提示只对个股 1Y 成立（ETF 的 1Y 来自 multpl/siblis 指数源, 不是本地反算）。
        // 判定为个股：既不是指数代理（PE_SOURCE 值不含 'Index'）, 且 snapshot 里有 EBIT。
        const stockSelected = Array.from(selected.keys()).filter(t => {{
          const src = PE_SOURCE[t];
          const snap = SNAPSHOT[t];
          const isIndexProxy = src && src.indexOf('Index') >= 0;
          return !isIndexProxy && snap && snap.ebit;
        }});
        if (rmeta.computed && stockSelected.length > 0) {{
          notes.push(
            `<span class="cf-tag">COMPUTED</span>` +
            `<b>1Y 周频为本地反算</b>（仅个股: ${{stockSelected.join(', ')}}），非站点直接提供。` +
            `EPS<sub>TTM</sub> / EBIT<sub>TTM</sub> 采用<b>当周所属季的滚动 4 季 TTM</b>（历史值，无未来函数）；` +
            `股本 / 总债务 / 现金采用<b>今日 statistics 快照</b>（该页无历史序列）。` +
            `因此 <b>P/E 曲线不受资本结构变化影响</b>，而 <b>EV/EBIT 早期点</b>会因股本 / 债务 / 现金随时间变化而存在偏差，仅供参考。<br/>` +
            `<code>${{FORMULA_HTML[currentMetric]}}</code>`
          );
        }}
        // FX-CONVERTED 提示: 报表币 != 报价币 的港股 (如 9992/9633/0700, 报表 CNY / 报价 HKD),
        // 1Y 曲线的 EPS/EBIT 已按 fx_to_quote 换算至报价币, 避免分子(HKD)分母(CNY)错配。
        // 只有在 range=1Y 且有个股做过换算时才显示 (3Y/5Y 走站点成品比值, 无币种问题)。
        if (rmeta.computed && stockSelected.length > 0) {{
          const fxLines = [];
          for (const t of stockSelected) {{
            const d = (typeof DCF_DATA !== 'undefined') ? DCF_DATA[t] : null;
            if (!d) continue;
            const fx = (typeof d.fx_to_quote === 'number') ? d.fx_to_quote : 1.0;
            const rc = d.report_currency || '';
            const qc = d.currency        || '';
            if (fx && fx !== 1.0 && rc && qc && rc !== qc) {{
              const asof = d.fx_asof || 'n/a';
              fxLines.push(
                `<b>${{t}}</b>: 1 ${{rc}} ≈ ${{fx.toFixed(4)}} ${{qc}} ` +
                `<span style="color:var(--muted)">(asof ${{asof}})</span>`
              );
            }}
          }}
          if (fxLines.length) {{
            notes.push(
              `<span class="cf-tag">FX-CONVERTED</span>` +
              `以下港股<b>报表币 ≠ 报价币</b>，1Y 曲线的 EPS<sub>TTM</sub> / EBIT<sub>TTM</sub>` +
              `已按<b>今日即期汇率</b>换算至报价币（等价于假设过去一年汇率恒定），` +
              `以确保分子（股价 / EV）与分母（盈利）币种一致：<br/>` +
              fxLines.join('<br/>') +
              `<br/><span style="color:var(--muted)">` +
              `汇率来源：从 stockanalysis 同一时点 statistics(报价币) ÷ financials(报表原币) 反算隐含即期。` +
              `曲线最右端为站点官方 TTM 快照（比值本身已在报价币口径），未参与换算。` +
              `</span>`
            );
          }}
        }}
        // 选中的 ETF 用的是指数代理 P/E，明确告知用户
        const etfSelected = Array.from(selected.keys()).filter(t => PE_SOURCE[t] && PE_SOURCE[t].indexOf('Index') >= 0);
        if (currentMetric === 'pe' && etfSelected.length > 0) {{
          // 1Y 情形补一句频率说明：SPYM 是月度点、QQQM 是季度点，都不是"周频"
          const rangeNote = (currentRange === '1y')
            ? `1Y 视图下点位仍为数据源原生频率（月度 / 季度），非本地反算的周频。`
            : ``;
          notes.push(
            `<span class="cf-tag">INDEX PROXY</span>` +
            `<b>${{etfSelected.join(', ')}}</b> 展示的是所追踪指数的历史 P/E（TTM），` +
            `并非 ETF 组合本身的加权 P/E——ETF 组合层面没有公开的历史 P/E 时序，` +
            `因此退而求其次以指数估值作为代理。` +
            `曲线最右端已用 stockanalysis ETF 主页的当前 TTM 快照覆盖数值，` +
            `以保证 1Y / 3Y / 5Y 最新点与表格里的 P/E 完全一致。` +
            (rangeNote ? `<br/>${{rangeNote}}` : ``)
          );
        }}
        // 选中的 ETF 但当前 metric 是 EV/EBIT：ETF 语义上不适用
        const etfInEvEbit = currentMetric === 'evebit' && Array.from(selected.keys()).some(t => {{
          const src = PE_SOURCE[t];
          const snap = SNAPSHOT[t];
          // 判定为 ETF：要么有指数代理 label、要么 snapshot 里缺 EBIT（个股一般都有）
          return (src && src.indexOf('Index') >= 0) || (snap && !snap.ebit);
        }});
        if (etfInEvEbit) {{
          notes.push(
            `<span class="cf-tag">N/A FOR ETF</span>` +
            `EV/EBIT 时序对 ETF 不适用（组合层面无 EBIT / Debt / Cash 概念，指数源亦不提供）。`
          );
        }}
        if (notes.length) {{
          formulaE.classList.add('show');
          formulaE.innerHTML = notes.join('<br/><br/>');
        }} else {{
          formulaE.classList.remove('show');
          formulaE.innerHTML = '';
        }}

        // 更新徽章选中态
        document.querySelectorAll('.tk-badge').forEach(b => {{
          const t = b.dataset.ticker;
          b.classList.toggle('selected', selected.has(t));
          if (selected.has(t)) b.style.setProperty('--sel', selected.get(t));
          else b.style.removeProperty('--sel');
        }});

        if (selected.size === 0) {{
          panel.classList.remove('open');
          panel.setAttribute('aria-hidden', 'true');
          body.innerHTML = '<div class="chart-empty">Click a ticker on the left to plot its history.<br/>Click again to remove; multiple tickers overlay for comparison.</div>';
          legendE.innerHTML = '';
          selectedE.innerHTML = '';
          return;
        }}
        panel.classList.add('open');
        panel.setAttribute('aria-hidden', 'false');

        // 汇总 x 轴（所有 ticker 里最长的日期序列）与 y 极值
        let xAll = [];
        let yMin =  Infinity, yMax = -Infinity;
        const series = [];
        for (const [t, color] of selected.entries()) {{
          const hist = dataMap[t] || [];
          if (!hist.length) continue;
          if (hist.length > xAll.length) xAll = hist.map(p => p[0]);
          for (const [_, v] of hist) {{
            if (v < yMin) yMin = v;
            if (v > yMax) yMax = v;
          }}
          series.push({{ ticker: t, color, points: hist }});
        }}
        if (!series.length) {{
          body.innerHTML = `<div class="chart-empty">No ${{meta.label}} history available for the selected tickers.</div>`;
          legendE.innerHTML = '';
          selectedE.innerHTML = '';
          return;
        }}

        // 上下留 8% padding
        const pad = (yMax - yMin) * 0.08 || 1;
        yMin -= pad; yMax += pad;
        if (yMin < 0) yMin = 0;

        const W = body.clientWidth  || 640;
        const H = 360;
        const M = {{ l: 46, r: 60, t: 14, b: 32 }};
        const iw = W - M.l - M.r;
        const ih = H - M.t - M.b;

        const N = xAll.length;
        const xOf = (i) => M.l + (N === 1 ? iw/2 : iw * i / (N - 1));
        const yOf = (v) => M.t + ih - (v - yMin) / (yMax - yMin) * ih;

        // Y 轴刻度
        const ticks = 5;
        let yTicksSvg = '';
        for (let k = 0; k <= ticks; k++) {{
          const v = yMin + (yMax - yMin) * k / ticks;
          const y = yOf(v);
          yTicksSvg += `<line x1="${{M.l}}" x2="${{W - M.r}}" y1="${{y}}" y2="${{y}}" class="grid"/>`;
          yTicksSvg += `<text x="${{M.l - 8}}" y="${{y}}" class="axis-lbl" text-anchor="end" dominant-baseline="middle">${{v.toFixed(1)}}</text>`;
        }}

        // X 轴刻度：最多显示 6 个；1Y 周频用 MM-DD，3Y/5Y 用 YYYY-MM（点为报告期末）
        // 无论抽稀 step 是否命中，最右点（最新时间）都必须显示：
        //   * 与前一 tick 距离足够远 → 追加
        //   * 距离太近 → 替换掉上一个 tick，避免文字重叠
        const step = Math.max(1, Math.ceil(N / 6));
        const isWeekly = currentRange === '1y';
        const tickIdx = [];
        for (let i = 0; i < N; i += step) tickIdx.push(i);
        if (N > 0 && tickIdx[tickIdx.length - 1] !== N - 1) {{
          const minGap = Math.max(1, Math.floor(step / 2));
          if ((N - 1) - tickIdx[tickIdx.length - 1] >= minGap) {{
            tickIdx.push(N - 1);
          }} else {{
            tickIdx[tickIdx.length - 1] = N - 1;
          }}
        }}
        let xTicksSvg = '';
        for (const i of tickIdx) {{
          const raw = xAll[i] || '';
          const lbl = isWeekly
            ? raw.slice(5)                       // '2026-08-03' -> '08-03'
            : raw.slice(0, 7);                   // '2024-12-31' -> '2024-12'
          const x = xOf(i);
          xTicksSvg += `<text x="${{x}}" y="${{H - M.b + 16}}" class="axis-lbl" text-anchor="middle">${{lbl}}</text>`;
        }}

        // 每条折线
        let seriesSvg = '';
        let avgSvg    = ''; // 均值虚线单独一层，画在折线之上、点标签之下
        let labelsSvg = ''; // 数据点值标签单独一层，最后再画，保证不被折线/圆点盖住
        // 根据 series 中最长点数决定标签的抽稀步长（点太密时避免文字挤在一起）
        const maxPts = series.reduce((m, s) => Math.max(m, s.points.length), 0);
        const lblStep = maxPts <= 20 ? 1 : (maxPts <= 30 ? 2 : Math.ceil(maxPts / 18));
        for (let si = 0; si < series.length; si++) {{
          const s = series[si];
          // 让 series.points 与 xAll 右对齐（因为长度可能不同）
          const offset = N - s.points.length;
          const d = s.points.map((p, i) => {{
            const x = xOf(offset + i);
            const y = yOf(p[1]);
            return `${{i === 0 ? 'M' : 'L'}}${{x.toFixed(1)}},${{y.toFixed(1)}}`;
          }}).join(' ');
          seriesSvg += `<path d="${{d}}" fill="none" stroke="${{s.color}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
          // 端点 + 每点值标签
          const lastIdx = s.points.length - 1;
          // 多条线时给不同曲线一点垂直错位，减少标签重叠
          const yShift = (series.length > 1) ? (si % 2 === 0 ? -9 : 12) : -9;
          for (let i = 0; i < s.points.length; i++) {{
            const x = xOf(offset + i);
            const y = yOf(s.points[i][1]);
            seriesSvg += `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="2.5" fill="${{s.color}}"/>`;
            // 抽稀：始终标注最后一个点，其余按步长采样
            const show = (i === lastIdx) || (((lastIdx - i) % lblStep) === 0);
            if (show && i !== lastIdx) {{
              const v = s.points[i][1];
              const txt = (Math.abs(v) >= 100) ? v.toFixed(0) : v.toFixed(1);
              labelsSvg += `<text x="${{x.toFixed(1)}}" y="${{(y + yShift).toFixed(1)}}" class="pt-lbl" fill="${{s.color}}" text-anchor="middle">${{txt}}</text>`;
            }}
          }}
          // 最新值标注（右侧粗体大号）
          const last = s.points[lastIdx];
          const lx = xOf(N - 1);
          const ly = yOf(last[1]);
          seriesSvg += `<text x="${{(lx + 6).toFixed(1)}}" y="${{ly.toFixed(1)}}" class="last-val" fill="${{s.color}}" dominant-baseline="middle">${{last[1].toFixed(1)}}</text>`;

          // 均值虚线（横跨该 series 所覆盖的 x 区间）
          const sVals = s.points.map(p => p[1]);
          const sAvg  = sVals.reduce((a,b) => a + b, 0) / sVals.length;
          const ax1 = xOf(offset);
          const ax2 = xOf(N - 1);
          const ay  = yOf(sAvg);
          avgSvg += `<line x1="${{ax1.toFixed(1)}}" x2="${{ax2.toFixed(1)}}" y1="${{ay.toFixed(1)}}" y2="${{ay.toFixed(1)}}" class="avg-line" stroke="${{s.color}}"/>`;
          const avgTxt = (Math.abs(sAvg) >= 100) ? sAvg.toFixed(0) : sAvg.toFixed(1);
          avgSvg += `<text x="${{(ax1 + 4).toFixed(1)}}" y="${{(ay - 3).toFixed(1)}}" class="avg-lbl" fill="${{s.color}}" text-anchor="start">avg ${{avgTxt}}</text>`;

          // 均值虚线右端：按当前 metric + 该 series 的均值反推"如估值回到均值,当前股价应该是多少"
          //   P/E     : implied = sAvg * EPS_TTM      (EPS_TTM = close / snap_pe)
          //   EV/EBIT : implied = (sAvg * EBIT - debt + cash) / shares
          // 位置紧贴虚线右端(ax2 右侧), 与最新值粗体大字错开一行(向下偏移 12px)
          const impPrice = implyPrice(s.ticker, currentMetric, sAvg);
          const impTxt   = fmtPrice(s.ticker, impPrice);
          if (impTxt) {{
            // 第一行: 按均值反推的隐含股价
            avgSvg += `<text x="${{(ax2 + 6).toFixed(1)}}" y="${{(ay + 6).toFixed(1)}}" class="avg-imp" fill="${{s.color}}" dominant-baseline="middle">→ ${{impTxt}}</text>`;
            // 第二行: 当前实际收盘价 (来自 SNAPSHOT); 视觉更弱以示区分
            const snap = SNAPSHOT[s.ticker];
            const curTxt = (snap && snap.close != null) ? fmtPrice(s.ticker, snap.close) : '';
            if (curTxt) {{
              avgSvg += `<text x="${{(ax2 + 6).toFixed(1)}}" y="${{(ay + 18).toFixed(1)}}" class="avg-cur" fill="${{s.color}}" dominant-baseline="middle">${{curTxt}}</text>`;
            }}
          }}
        }}

        body.innerHTML = `
          <svg viewBox="0 0 ${{W}} ${{H}}" width="100%" height="${{H}}" preserveAspectRatio="none">
            ${{yTicksSvg}}
            ${{xTicksSvg}}
            <line x1="${{M.l}}" x2="${{M.l}}" y1="${{M.t}}" y2="${{H - M.b}}" class="axis"/>
            <line x1="${{M.l}}" x2="${{W - M.r}}" y1="${{H - M.b}}" y2="${{H - M.b}}" class="axis"/>
            ${{seriesSvg}}
            ${{labelsSvg}}
            ${{avgSvg}}
          </svg>
        `;

        // 图例（含最新/最小/最大/均值）
        legendE.innerHTML = series.map(s => {{
          const vals = s.points.map(p => p[1]);
          const mn = Math.min(...vals), mx = Math.max(...vals);
          const avg = vals.reduce((a,b)=>a+b,0) / vals.length;
          const last = vals[vals.length-1];
          return `
            <div class="lg-item">
              <span class="lg-swatch" style="background:${{s.color}}"></span>
              <span class="lg-tk">${{logoImg(s.ticker)}}<span class="tk-sym">${{s.ticker}}</span></span>
              <span class="lg-stat"><em>latest</em> ${{fmt(last)}}</span>
              <span class="lg-stat"><em>avg</em> ${{fmt(avg)}}</span>
              <span class="lg-stat"><em>min</em> ${{fmt(mn)}}</span>
              <span class="lg-stat"><em>max</em> ${{fmt(mx)}}</span>
            </div>`;
        }}).join('');

        selectedE.innerHTML = Array.from(selected.entries()).map(([t,c]) =>
          `<span class="sel-chip" style="border-color:${{c}};color:${{c}}">${{logoImg(t)}}<span class="tk-sym">${{t}}</span></span>`
        ).join('');
      }}

      function toggle(ticker) {{
        if (selected.has(ticker)) {{
          selected.delete(ticker);
        }} else {{
          // 即便当前 metric 无数据也允许选中，render 里会给出 "No data" 提示
          selected.set(ticker, pickColor());
        }}
        render();
      }}

      document.querySelectorAll('.tk-badge').forEach(b => {{
        b.addEventListener('click', () => toggle(b.dataset.ticker));
      }});
      tabs.forEach(t => {{
        t.addEventListener('click', () => {{
          const m = t.dataset.metric;
          if (m === currentMetric) return;
          // 若该 tab 已被隐藏（例如选中的是 ETF, 而点的是 EV/EBIT 或 DCF）, 忽略。
          if (t.classList.contains('hidden')) return;
          currentMetric = m;
          tabs.forEach(x => {{
            const on = x.dataset.metric === m;
            x.classList.toggle('active', on);
            x.setAttribute('aria-selected', on ? 'true' : 'false');
          }});
          // Metric 切换会影响 range 组是否可见 (DCF 隐藏它)
          syncRangeGroupVisibility();
          render();
        }});
      }});

      // 根据当前选中的 ticker 集合动态调整 Tab 可见性:
      //   - 只要选中的集合里包含任何 ETF, 则隐藏 EV/EBIT 与 DCF (两者对 ETF 均无意义);
      //   - 若当前 metric 恰好是被隐藏的, 自动回退到 P/E, 保持视图有内容。
      // 空选中集合视为"未开始", 全部 Tab 保持可见。
      function syncTabVisibility() {{
        const hasETF = Array.from(selected.keys()).some(t => ETF_SET.has(t));
        tabs.forEach(x => {{
          const m = x.dataset.metric;
          const hide = hasETF && (m === 'evebit' || m === 'dcf');
          x.classList.toggle('hidden', hide);
          if (hide) x.setAttribute('aria-hidden', 'true');
          else      x.removeAttribute('aria-hidden');
        }});
        if (hasETF && (currentMetric === 'evebit' || currentMetric === 'dcf')) {{
          currentMetric = 'pe';
          tabs.forEach(x => {{
            const on = x.dataset.metric === 'pe';
            x.classList.toggle('active', on);
            x.setAttribute('aria-selected', on ? 'true' : 'false');
          }});
        }}
        // currentMetric 可能被强制切回 pe, 需要同步 range 组可见性
        syncRangeGroupVisibility();
      }}
      rangeBtns.forEach(b => {{
        b.addEventListener('click', () => {{
          const r = b.dataset.range;
          if (r === currentRange) return;
          currentRange = r;
          rangeBtns.forEach(x => {{
            const on = x.dataset.range === r;
            x.classList.toggle('active', on);
            x.setAttribute('aria-selected', on ? 'true' : 'false');
          }});
          render();
        }});
      }});
      closeBtn.addEventListener('click', () => {{
        selected.clear();
        render();
      }});
      window.addEventListener('resize', () => {{ if (selected.size) render(); }});

      // ---- 给指定列的单元格追加"数据源信息按钮 (ⓘ)" ----
      // 按表头文本定位目标列, 再对每行 data-ticker 查 sourceMap / urlMap:
      //   sourceMap[sym] -> 悬停提示文本 (Source: ...)
      //   urlMap[sym]    -> 点击跳转 URL (新标签页打开)
      // 命中就在单元格右上角插入一个圆形 ⓘ <a> 按钮, 同时给 td 加 has-src 类
      // 以启用 position: relative。
      // 当前用于四列 ETF: EPS Growth (3-5Y Est) / ROE / P/E (TTM) / P/E (Fwd).
      // 个股所有数据列 (Close / AsOf / MarketCap ... PEG) 走下面的 annotateStockCols.
      function attachSrcInfo(cell, label, url) {{
        if (!cell || !label) return;
        // 避免同一单元格重复挂载
        if (cell.querySelector('a.src-info')) return;
        const a = document.createElement('a');
        a.className = 'src-info';
        a.textContent = 'i';
        a.setAttribute('title', 'Source: ' + label + ' (click to open)');
        a.setAttribute('aria-label', 'Data source: ' + label);
        if (url) {{
          a.href = url;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
        }} else {{
          a.href = 'javascript:void(0)';
          a.style.cursor = 'help';
        }}
        cell.classList.add('has-src');
        cell.insertBefore(a, cell.firstChild);
      }}

      function annotateColSource(headerText, sourceMap, urlMap) {{
        if (!sourceMap || Object.keys(sourceMap).length === 0) return;
        urlMap = urlMap || {{}};
        document.querySelectorAll('table').forEach(tbl => {{
          const ths = tbl.querySelectorAll('thead th');
          let colIdx = -1;
          ths.forEach((th, i) => {{
            // 表头里 &nbsp; -> textContent 里表现为空格, 统一压缩空白再对比
            const txt = (th.textContent || '').replace(/\\s+/g, ' ').trim();
            if (txt === headerText) colIdx = i;
          }});
          if (colIdx < 0) return;
          tbl.querySelectorAll('tbody tr[data-ticker]').forEach(tr => {{
            const sym = tr.getAttribute('data-ticker');
            const label = sourceMap[sym];
            if (!label) return;
            const cell = tr.children[colIdx];
            if (!cell) return;
            attachSrcInfo(cell, label, urlMap[sym]);
          }});
        }});
      }}

      // ---- 计算 ETF 数据源对应的跳转 URL ----
      // 由 label 文本关键字反推源网页 (发行商官方 / 指数代理), 无需在后端再维护一份 URL 表.
      // 匹配失败返回 null, 前端会退化为"仅悬停提示, 不可点击"。
      function etfSrcUrl(sym, label) {{
        if (!label) return null;
        const s = label.toLowerCase();
        if (s.includes('ssga')) {{
          // SSGA SPYM (S&P 500) 官方 ETF 页
          return 'https://www.ssga.com/us/en/individual/etfs/spdr-portfolio-sp-500-etf-spym';
        }}
        if (s.includes('invesco')) {{
          // Invesco QQQM (Nasdaq 100) 官方 ETF 页
          return 'https://www.invesco.com/us/financial-products/etfs/product-detail?audienceType=Investor&ticker=QQQM';
        }}
        if (s.includes('vanguard')) {{
          // Vanguard 官方页 (VUG 用 VUG 主页, SPYM/VOO proxy 走 VOO)
          if (sym === 'VUG') return 'https://investor.vanguard.com/investment-products/etfs/profile/vug';
          return 'https://investor.vanguard.com/investment-products/etfs/profile/voo';
        }}
        if (s.includes('multpl')) {{
          return 'https://www.multpl.com/s-p-500-pe-ratio/table/by-month';
        }}
        if (s.includes('siblis')) {{
          return 'https://siblisresearch.com/data/nasdaq-100-pe-ratio/';
        }}
        if (s.includes('stockanalysis')) {{
          // ETF 加权 P/E (TTM) 来自 stockanalysis.com/etf/{{t}}/
          return 'https://stockanalysis.com/etf/' + sym.toLowerCase() + '/';
        }}
        return null;
      }}

      // ---- 给个股 (US + HK) 数据列挂载数据源 ⓘ 按钮 ----
      // 个股数据统一走 stockanalysis.com, 按列语义走 statistics 页:
      //   MarketCap / EV / Debt / Cash&STI /
      //     EBIT(TTM) / EV/EBIT / EV/EBITDA /
      //     P/E(TTM) / P/E(Fwd) / PEG      -> statistics 页
      // 注: Close / As Of 不挂载 ⓘ (报价本身没有额外的 "数据来源" 值得跳转,
      //     且顶部大号价格块与表格里的收盘价语义一致, 挂 ⓘ 反而累赘).
      // HK 与 US 路径不同, 由 ticker 是否纯数字判断 (港股 4 位数字代码 e.g. 0700).
      function stockSrcStats(sym) {{        if (/^\\d+$/.test(sym)) return 'https://stockanalysis.com/quote/hkg/' + sym + '/statistics/';
        return 'https://stockanalysis.com/stocks/' + sym.toLowerCase() + '/statistics/';
      }}
      // 表头文本 -> (label, url 类型) ; url 类型: 'stats' (目前全部指向 statistics 页)
      const STOCK_COL_SPEC = [
        ['Market Cap',            'stockanalysis.com · Statistics',             'stats'],
        ['EV',                    'stockanalysis.com · Statistics',             'stats'],
        ['Total Debt',            'stockanalysis.com · Statistics',             'stats'],
        ['Cash + STI',            'stockanalysis.com · Statistics',             'stats'],
        ['EBIT (TTM)',            'stockanalysis.com · Statistics',             'stats'],
        ['EV / EBIT',             'stockanalysis.com · Statistics',             'stats'],
        ['EV / EBITDA',           'stockanalysis.com · Statistics',             'stats'],
        ['P/E (TTM)',             'stockanalysis.com · Statistics',             'stats'],
        ['P/E (Fwd)',             'stockanalysis.com · Statistics',             'stats'],
        ['PEG',                   'stockanalysis.com · Statistics',             'stats'],
      ];
      function annotateStockCols() {{
        document.querySelectorAll('table').forEach(tbl => {{
          const ths = tbl.querySelectorAll('thead th');
          // 建立"表头文本 -> 列索引"字典
          const headerIdx = {{}};
          ths.forEach((th, i) => {{
            const txt = (th.textContent || '').replace(/\\s+/g, ' ').trim();
            headerIdx[txt] = i;
          }});
          tbl.querySelectorAll('tbody tr[data-ticker]').forEach(tr => {{
            const sym = tr.getAttribute('data-ticker');
            if (ETF_SET.has(sym)) return;  // ETF 走各自的 SOURCE map, 不走通用规则
            STOCK_COL_SPEC.forEach(([header, label, kind]) => {{
              const idx = headerIdx[header];
              if (idx == null) return;
              const cell = tr.children[idx];
              if (!cell) return;
              // 若该单元格为空 (—), 跳过挂载, 避免"空数据"上出现指向 stats 页的信息按钮误导
              const txt = (cell.textContent || '').trim();
              if (!txt || txt === '—' || txt === '-' || txt === 'N/A') return;
              // 当前所有列都走 statistics; kind 字段保留以便未来扩展 (e.g. financials 页).
              const url = stockSrcStats(sym);
              attachSrcInfo(cell, label, url);
            }});
          }});
        }});
      }}

      // ---- ETF 已有 SOURCE map 走 annotateColSource + etfSrcUrl 反推 URL ----
      // 为每列构造 ticker -> url 表
      function buildUrlMap(sourceMap) {{
        const out = {{}};
        Object.keys(sourceMap).forEach(sym => {{
          const u = etfSrcUrl(sym, sourceMap[sym]);
          if (u) out[sym] = u;
        }});
        return out;
      }}
      annotateColSource('P/E (Fwd)',             FWD_SOURCE,        buildUrlMap(FWD_SOURCE));
      annotateColSource('P/E (TTM)',             PE_TTM_SOURCE,     buildUrlMap(PE_TTM_SOURCE));
      annotateColSource('EPS Growth (3-5Y Est)', EPS_GROWTH_SOURCE, buildUrlMap(EPS_GROWTH_SOURCE));
      annotateColSource('ROE',                   ROE_SOURCE,        buildUrlMap(ROE_SOURCE));
      // 个股表格所有数据列一次性挂载
      annotateStockCols();
    }})();
  </script>
</body>
</html>
"""


def _favicon_link_tag() -> str:
    """读取 icon.png, 编码为 data URI 作为 favicon.

    使用 data URI 而非相对路径, 让生成的 HTML 可独立分发 (无需附带 png).
    查找顺序: 脚本同目录 -> Pages/ 子目录 (兼容"资产只放 GitHub Pages 部署源"的布局).
    如果 png 都不存在或读取失败, 返回空串, 浏览器会退回到默认无图标行为.
    """
    import base64, os
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "icon.png"),
        os.path.join(here, "Pages", "icon.png"),
    ]
    for icon_path in candidates:
        try:
            with open(icon_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f'<link rel="icon" type="image/png" href="data:image/png;base64,{b64}" />'
        except OSError:
            continue
    return ""


def build_html_report(
    sections: list[tuple[str, list[Row], str]],
    generated_at: datetime | None = None,
) -> str:
    ts = generated_at or datetime.now()
    body = "\n".join(render_section_html(t, rows, cur) for t, rows, cur in sections)

    # 汇总所有 ticker 的历史序列（5Y/3Y=季度TTM, 1Y=周频本地反算），序列化成 JSON 注入前端
    pe_5y: dict[str, list[list]] = {}
    ev_5y: dict[str, list[list]] = {}
    pe_3y: dict[str, list[list]] = {}
    ev_3y: dict[str, list[list]] = {}
    pe_1y: dict[str, list[list]] = {}
    ev_1y: dict[str, list[list]] = {}
    # ticker -> P/E 历史数据源标签（前端图表副标题按选中 ticker 拼接显示）
    pe_source: dict[str, str] = {}
    # ticker -> Forward P/E 数据源标签（仅 ETF 有值, 表格 P/E (Fwd) 单元格 title tooltip）
    fwd_source: dict[str, str] = {}
    # ticker -> P/E (TTM) 单元格数据源标签 (仅 ETF, 走 stockanalysis /etf/{t}/; 个股不标注)
    pe_ttm_source: dict[str, str] = {}
    # ticker -> EPS Growth (3-5Y Est) 单元格数据源标签 (仅 ETF)
    eps_growth_source: dict[str, str] = {}
    # ticker -> ROE 单元格数据源标签 (仅 ETF)
    roe_source: dict[str, str] = {}
    for _title, rows, _cur in sections:
        for r in rows:
            if r.pe_history:
                pe_5y[r.symbol] = [[d, v] for d, v in r.pe_history]
            if r.ev_ebit_history:
                ev_5y[r.symbol] = [[d, v] for d, v in r.ev_ebit_history]
            if r.pe_history_3y:
                pe_3y[r.symbol] = [[d, v] for d, v in r.pe_history_3y]
            if r.ev_ebit_history_3y:
                ev_3y[r.symbol] = [[d, v] for d, v in r.ev_ebit_history_3y]
            if r.pe_history_1y:
                pe_1y[r.symbol] = [[d, v] for d, v in r.pe_history_1y]
            if r.ev_ebit_history_1y:
                ev_1y[r.symbol] = [[d, v] for d, v in r.ev_ebit_history_1y]
            # 个股历史均来自 stockanalysis.com/financials/ratios；ETF 则已在 _fetch_etf 里设好专用 label。
            if r.pe_history_source:
                pe_source[r.symbol] = r.pe_history_source
            elif r.pe_history:
                pe_source[r.symbol] = "stockanalysis.com · quarterly TTM"
            # Forward P/E 数据源 (仅 ETF, 个股不额外标注)
            if r.pe_forward_source:
                fwd_source[r.symbol] = r.pe_forward_source
            # ---- 单元格级数据源 tooltip (P/E TTM / EPS Growth / ROE) ----
            # 仅 ETF 需要标注单元格级数据源, 个股不加 tooltip (口径统一走 stockanalysis
            # statistics 页, 用户已知, 无需在每格重复署名, 避免视觉噪音)。三个 map 均只
            # 收录 Row 里显式设置了 source label 的 ticker, 而这些 label 只在 _fetch_etf
            # 内被写入, 因此天然只包含 ETF, 无需再判断 r.symbol 是否属于 ETFs 集合。
            if r.pe_ttm_source:
                pe_ttm_source[r.symbol] = r.pe_ttm_source
            if r.eps_growth_source:
                eps_growth_source[r.symbol] = r.eps_growth_source
            if r.roe_source:
                roe_source[r.symbol] = r.roe_source
    pe_5y_json = json.dumps(pe_5y, ensure_ascii=False)
    ev_5y_json = json.dumps(ev_5y, ensure_ascii=False)
    pe_3y_json = json.dumps(pe_3y, ensure_ascii=False)
    ev_3y_json = json.dumps(ev_3y, ensure_ascii=False)
    pe_1y_json = json.dumps(pe_1y, ensure_ascii=False)
    ev_1y_json = json.dumps(ev_1y, ensure_ascii=False)
    pe_source_json = json.dumps(pe_source, ensure_ascii=False)
    fwd_source_json = json.dumps(fwd_source, ensure_ascii=False)
    pe_ttm_source_json = json.dumps(pe_ttm_source, ensure_ascii=False)
    eps_growth_source_json = json.dumps(eps_growth_source, ensure_ascii=False)
    roe_source_json = json.dumps(roe_source, ensure_ascii=False)
    logo_map_json = json.dumps(LOGO_DOMAIN, ensure_ascii=False)
    # ETF 集合注入前端, 用于动态隐藏对 ETF 无意义的 Tab (EV/EBIT / DCF)。
    etf_set_json = json.dumps([t.upper() for t in ETFs], ensure_ascii=False)

    # 每个 ticker 的当前快照，供前端在均值虚线右端反推"若估值回到均值时的隐含股价"
    #   P/E 情形:    implied_price = avg_pe / snap_pe * close   (= avg_pe * EPS_TTM)
    #   EV/EBIT 情形: implied_price = (avg_ev_ebit * EBIT_TTM - debt + cash) / shares_out
    #     其中 shares_out = MarketCap / close （由 statistics 页 marketcap 反推）
    snapshot: dict[str, dict] = {}
    for _title, rows, cur in sections:
        for r in rows:
            close  = _to_float(r.data.get(CLOSE_COL))
            snap_pe = _to_float(r.data.get(FIELDS["pe"]))
            ebit   = _to_float(r.data.get(FIELDS["ebit"]))
            debt   = _to_float(r.data.get(FIELDS["debt"]))
            cash   = _to_float(r.data.get(FIELDS["totalcash"]))
            mcap   = _to_float(r.data.get(FIELDS["marketcap"]))
            shares = (mcap / close) if (mcap and close) else None
            snapshot[r.symbol] = {
                "close":    close,
                "pe":       snap_pe,
                "ebit":     ebit,
                "debt":     debt,
                "cash":     cash,
                "shares":   shares,
                "currency": cur,
            }
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)

    # DCF 数据 map: ticker -> Row.dcf dict (可能为 None; 前端 DCF Tab 会渲染缺失原因)。
    # ETF 与所有抓取失败 / 无 key 的 ticker 都会缺失, 前端负责给出可读提示。
    dcf_map: dict[str, dict] = {}
    for _title, rows, _cur in sections:
        for r in rows:
            if r.dcf:
                dcf_map[r.symbol] = r.dcf
    dcf_json = json.dumps(dcf_map, ensure_ascii=False)

    # -------- 稳定的数据契约块 (vs-data / schema v1) --------
    # 目的: 让未来任意时间点写的爬虫都能从 Pages/ 里 520 份历史 HTML 中稳定抽出结构化数据,
    # 不受脚本内部实现 (变量名/字段名/前端渲染方式) 演进影响。
    #
    # 契约要点:
    #   - 位置固定: <script type="application/json" id="vs-data" data-schema="N">
    #   - 版本演进: 字段增删只允许"追加"; 若必须删/改, 请升 data-schema 版本并保留旧解析路径
    #   - 字段周全: 涵盖当前表格所有列 + 历史曲线 + 数据源标签, 一份文件就是一份完整周度快照
    tickers_data: dict[str, dict] = {}
    for _title, rows, cur in sections:
        for r in rows:
            close   = _to_float(r.data.get(CLOSE_COL))
            snap_pe = _to_float(r.data.get(FIELDS["pe"]))
            fwd_pe  = _to_float(r.data.get(FIELDS["peForward"]))
            ev_ebit = _to_float(r.data.get(FIELDS["evEbit"]))
            ev_ebitda = _to_float(r.data.get(FIELDS["evEbitda"]))
            peg     = _to_float(r.data.get(FIELDS["pegRatio"]))
            ebit    = _to_float(r.data.get(FIELDS["ebit"]))
            debt    = _to_float(r.data.get(FIELDS["debt"]))
            cash    = _to_float(r.data.get(FIELDS["totalcash"]))
            mcap    = _to_float(r.data.get(FIELDS["marketcap"]))
            evv     = _to_float(r.data.get(FIELDS["enterpriseValue"]))
            eps_g   = _to_float(r.data.get(FIELDS["epsGrowth3To5Y"]))
            roe     = _to_float(r.data.get(FIELDS["returnOnEquity"]))
            shares  = (mcap / close) if (mcap and close) else None
            tickers_data[r.symbol] = {
                "section": _title,
                "currency": cur,
                "close": close,
                "close_date": r.data.get(CLOSE_DATE_COL),
                "pe": snap_pe,
                "pe_forward": fwd_pe,
                "ev_ebit": ev_ebit,
                "ev_ebitda": ev_ebitda,
                "peg": peg,
                "ebit_ttm": ebit,
                "debt": debt,
                "cash": cash,
                "market_cap": mcap,
                "enterprise_value": evv,
                "shares_out": shares,
                # ETF 专用字段 (个股为 None): 发行商官方加权口径.
                # eps_growth_3_5y 单位是百分比 (18.43 表示 18.43%);
                # roe 同理 (34.79 表示 34.79%).
                "eps_growth_3_5y": eps_g,
                "roe": roe,
                "pe_history_source": r.pe_history_source,
                "pe_forward_source": r.pe_forward_source,
                # 曲线保留 5Y 全量 (3Y/1Y 都是它的尾部切片, 冗余不必存);
                # 每点是 [date_str, value]
                "pe_history_5y": (
                    [[d, v] for d, v in r.pe_history] if r.pe_history else []
                ),
                "ev_ebit_history_5y": (
                    [[d, v] for d, v in r.ev_ebit_history] if r.ev_ebit_history else []
                ),
                # DCF 明细 (方案 C 双数据源): {fmp:{...}|null, sa:{...}|null, currency}.
                # 未抓到 (ETF / 两侧全失败) 时字段值为 None; 前端 DCF Tab 双 panel 渲染。
                "dcf": r.dcf,
            }
    vs_data = {
        "schema": 1,
        "date": ts.strftime("%Y-%m-%d"),
        "generated_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "tickers": tickers_data,
    }
    vs_data_json = json.dumps(vs_data, ensure_ascii=False, separators=(",", ":"))
    # 安全: 避免 JSON 里意外出现 "</script>" 类字符串导致 HTML 解析器提前关闭 <script>
    # (这里我们的字段都是数字/短标签, 实际不会命中, 但仍做一次防御性转义)
    vs_data_json = vs_data_json.replace("</", "<\\/")

    return _HTML_TEMPLATE.format(
        sections=body,
        generated_at=ts.strftime("%Y-%m-%d %H:%M:%S"),
        pe_5y_json=pe_5y_json,
        ev_5y_json=ev_5y_json,
        pe_3y_json=pe_3y_json,
        ev_3y_json=ev_3y_json,
        pe_1y_json=pe_1y_json,
        ev_1y_json=ev_1y_json,
        pe_source_json=pe_source_json,
        fwd_source_json=fwd_source_json,
        pe_ttm_source_json=pe_ttm_source_json,
        eps_growth_source_json=eps_growth_source_json,
        roe_source_json=roe_source_json,
        logo_map_json=logo_map_json,
        etf_set_json=etf_set_json,
        snapshot_json=snapshot_json,
        dcf_json=dcf_json,
        favicon_link=_favicon_link_tag(),
        vs_data_json=vs_data_json,
    )


# ---------- 主流程 ----------

def _fetch_list(tickers: list[str], market: str) -> list[Row]:
    rows: list[Row] = []
    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(0.5)  # 轻微节流
        print(f"Fetching {market} {ticker} ...", file=sys.stderr)
        rows.append(fetch_ticker(ticker, market))
    return rows


def _sync_pages(report_path: str) -> None:
    """HTML 已经直接写入 Pages/，这里只做"周边同步"：

      1. 复制 favicon ``icon.png`` 到 ``Pages/`` （每次覆盖）。
      2. 用正则重写 ``Pages/index.html`` 中三处对 report 的引用
         （meta refresh、canonical link、可见备用链接），指向最新那份。
      3. 确保存在空文件 ``Pages/.nojekyll`` （阻止 Pages 用 Jekyll 处理）。

    设计取舍：
      - Pages/index.html 若不存在则跳过重写（首次使用者应先手工放好模板）。
      - 历史 report 保留在 Pages/ 里不清理，旧链接依然可达。
      - CI 截图脚本 (.github/scripts/screenshot.py) 会自行扫 Pages/<YYYY>/ 下
        最新的归档 HTML 作为截图源, 无需在此额外产出 latest.html。
    """
    import os, re, shutil

    here = os.path.dirname(os.path.abspath(__file__))
    pages_dir = os.path.join(here, "Pages")
    # Pages 目录此时必然存在（主流程已 makedirs），保险起见再判一次
    if not os.path.isdir(pages_dir):
        return

    # report_ref: 供 index.html 引用的相对路径, 形如 "2026/ValuationSnapshot-20260810.html"
    # 兼容老布局: 如果 report_path 就在 pages_dir 根下, 结果退化为纯文件名 basename.
    report_ref = os.path.relpath(report_path, pages_dir).replace(os.sep, "/")

    # 1) 复制 favicon（每次覆盖，保持同步）
    # png 可能放在主目录（老布局），也可能已经躺在 Pages/ 里（新布局）；后者情形
    # 直接跳过复制即可（源和目标相同）。
    fav_src = os.path.join(here, "icon.png")
    fav_dst = os.path.join(pages_dir, "icon.png")
    if os.path.exists(fav_src) and os.path.abspath(fav_src) != os.path.abspath(fav_dst):
        shutil.copy2(fav_src, fav_dst)

    # 2) 重写 index.html 里的 3 处引用
    index_path = os.path.join(pages_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            txt = f.read()
        orig = txt
        # meta refresh: content="0; url=XXX.html"
        txt = re.sub(
            r'(<meta\s+http-equiv="refresh"[^>]*url=)[^"\s>]+',
            lambda m: m.group(1) + report_ref,
            txt,
        )
        # <link rel="canonical" href="XXX.html" />
        txt = re.sub(
            r'(<link\s+rel="canonical"\s+href=")[^"]+(")',
            lambda m: m.group(1) + report_ref + m.group(2),
            txt,
        )
        # <a href="[YYYY/]XXX.html">[YYYY/]XXX.html</a>  （既改 href 又改可见文本）
        # 兼容老布局 (裸文件名) 与新布局 (带年份前缀)。
        txt = re.sub(
            r'(<a\s+href=")(?:\d{4}/)?ValuationSnapshot-\d{8}\.html(">)(?:\d{4}/)?ValuationSnapshot-\d{8}\.html(</a>)',
            lambda m: m.group(1) + report_ref + m.group(2) + report_ref + m.group(3),
            txt,
        )
        if txt != orig:
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(txt)
            print(f"> Pages: index.html -> {report_ref}", file=sys.stderr)

    # 4) .nojekyll
    nojekyll = os.path.join(pages_dir, ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w", encoding="utf-8").close()


def main() -> int:
    # 先建 Pages 目录并抓取 favicon 缓存 (仅缺失的域名会联网抓; 已缓存的直接跳过)。
    # 抓取放在 fetch stocks 之前, 是因为 stockanalysis.com 请求耗时几十秒, 提前做 logo
    # 缓存不会显著拉长总时间, 而且失败也不阻塞后续流程。
    import os
    pages_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Pages")
    os.makedirs(pages_dir, exist_ok=True)
    ensure_logo_cache(os.path.join(pages_dir, "logos"))

    etf_rows = _fetch_list(ETFs, "ETF")
    us_rows = _fetch_list(USStocks, "US")
    hk_rows = _fetch_list(HKStocks, "HK")

    # DCF 抓取: 遍历所有个股 (跳过 ETF), 融合 FMP 单值 + stockanalysis 分析师共识。
    # 该调用不阻塞主流程——任一/两侧失败时 Row.dcf 仍为 None 或半空 dict, 前端会给出
    # 对应的缺失原因说明。放在收盘价 / 财务数据之后, 因为 DCF 数据完全独立且失败率较高
    # (小市值 / 港股 FMP 覆盖不全), 集中在一个阶段便于 CI 日志排查。
    print("\n> Fetching DCF (FMP single value + stockanalysis analyst consensus)...", file=sys.stderr)
    for r in us_rows:
        r.dcf = _fetch_dcf(r.symbol, "US", "USD")
    for r in hk_rows:
        r.dcf = _fetch_dcf(r.symbol, "HK", "HKD")

    # ------------------------------------------------------------------
    # 1Y 曲线的币种一致性修正 (报表币 != 报价币 的港股)
    # ------------------------------------------------------------------
    # 背景: _build_1y_history 里 P/E_week 与 EV/EBIT_week 的公式为
    #     P/E_week    = price(HKD) ÷ EPS_TTM(报表币)
    #     EV_week     = price(HKD) × shares + debt(HKD) − cash(HKD)
    #     EV/EBIT_wk  = EV(HKD)   ÷ EBIT_TTM(报表币)
    # 对于报表币 = CNY 的港股 (9992/9633/0700 等), 分子分母币种错配, 会让 1Y
    # 历史点被系统性抬高 ≈ fx_to_quote 倍 (2026 年 CNY/HKD ≈ 1.11-1.14)。
    # 修复: 把 EPS/EBIT_TTM 一次性乘 fx_to_quote 换算到报价币, 数学等价于
    # "假设过去一年汇率恒定 = 今日即期" — 与 DCF 用的同一 fx 前提保持一致。
    # 注: 序列最右端 (今日快照) 已在 fetch_snapshot 里用 statistics 页官方
    # PE/EV_EBIT 覆盖 (成品比值, 币种自动一致), 故只需修正历史点 [0..-2]。
    for r in hk_rows:
        if not r.dcf:
            continue
        fx = r.dcf.get("fx_to_quote")
        if not fx or fx == 1.0:
            continue
        # 分母 = EPS/EBIT × fx  =>  比值需要再除以 fx  (等价)
        if r.pe_history_1y and len(r.pe_history_1y) >= 2:
            r.pe_history_1y = (
                [(d, v / fx) for d, v in r.pe_history_1y[:-1]]
                + [r.pe_history_1y[-1]]
            )
        if r.ev_ebit_history_1y and len(r.ev_ebit_history_1y) >= 2:
            r.ev_ebit_history_1y = (
                [(d, v / fx) for d, v in r.ev_ebit_history_1y[:-1]]
                + [r.ev_ebit_history_1y[-1]]
            )
        print(
            f"  .. {r.symbol}: 1Y series FX-adjusted "
            f"(EPS/EBIT × {fx:.4f} => same currency as price)",
            file=sys.stderr,
        )

    now = datetime.now()
    html_doc = build_html_report(
        [
            ("ETFs", etf_rows, "USD"),
            ("US Stocks", us_rows, "USD"),
            ("HK Stocks", hk_rows, "HKD"),
        ],
        generated_at=now,
    )

    # 文件名安全的时间戳（本地时间），只保留到天，例：Pages/2026/ValuationSnapshot-20260807.html
    # （数据用的是前日收盘价，同一天多次运行会覆盖同一份文件）
    # 所有产物按"年份分目录"归档到 Pages/<YYYY>/ 下, 便于 10 年后按年裁剪 / 打包 / 走 LFS.
    # index.html 依然位于 Pages/ 根目录, meta refresh 指向 <YYYY>/ValuationSnapshot-YYYYMMDD.html
    year_dir = os.path.join(pages_dir, now.strftime("%Y"))
    os.makedirs(year_dir, exist_ok=True)
    out_path = os.path.join(year_dir, f"ValuationSnapshot-{now.strftime('%Y%m%d')}.html")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
        print(f"\n> HTML report written to `{out_path}`", file=sys.stderr)
    except OSError as e:
        print(f"  !! failed to write {out_path}: {e}", file=sys.stderr)
        return 1

    # 刷新 Pages/ 里的 favicon 与 index.html
    try:
        _sync_pages(out_path)
    except Exception as e:
        print(f"  !! Pages sync failed: {e}", file=sys.stderr)

    # 尝试自动用默认浏览器打开（失败也不影响脚本成功退出）
    try:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(out_path))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
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
        QQQM -> Nasdaq 100     https://siblisresearch.com/data/nasdaq-100-pe-ratio/    (月度最新 + 历年 Dec, ~10+ 期)
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
from dataclasses import dataclass


# ---------- 股票列表 ----------

USStocks = [
    "AAPL",
    "MSFT",
    "GOOGL",   # Alphabet：stockanalysis 已提供公司整体 EV/EBIT，用 GOOGL 或 GOOG 都一样
    "AMZN",
    "NVDA",
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
# (EV/EBIT, EV/EBITDA, EBIT, Debt, Cash) 一律保留为 None -> 前端显示 "-"。
#
# Forward P/E ("PE Fwd" 列) 走各自的**发行商官方**基金 characteristics 数据源:
#   SPYM -> SSGA 官网 SPYM SSR HTML (weighted harmonic FY1, FactSet Estimates, 月频)
#   QQQM -> Invesco dng-api JSON      (weighted harmonic forward P/E, 日频)
#
# 历史 P/E 曲线走"指数代理": ETF 组合层面无公开时序, 因此改抓 ETF 所跟踪指数的历史 P/E。
#   SPYM -> S&P 500        (multpl.com, 月频, 50+ 年)
#   QQQM -> Nasdaq 100     (siblisresearch.com, 月度最新 + 历年 Dec, ~10+ 期)
# EV/EBIT 时序对 ETF 一律不适用, 保持 N/A。
ETFs = [
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
    "NVDA":  "nvidia.com",
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
    "QQQM":  "invesco.com",
    "SPYM":  "ssga.com",
}


# ---------- 页面字段映射 ----------

# stockanalysis.com 页面里的字段 id -> 我们要的列
#
# 说明:
#   epsGrowth3To5Y / returnOnEquity 是 ETF 专用列 (个股不填):
#     - SPYM: SSGA HTML 提供 "Est. 3-5 Year EPS Growth", 无 ROE (SSGA 官方不发布)
#     - QQQM: Invesco API 提供 returnOnEquity, 无 EPS Growth
#   在 render_section_html 里 ETF section 只展示 [PE, PE Fwd, EPS Growth, ROE] 四列,
#   其它列 (MarketCap/EV/Debt/Cash/EBIT/EV·EBIT/EV·EBITDA) 对 ETF 语义上不成立, 直接不渲染。
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
#
# stockanalysis 的所有页面都共享一套"主机 + 市场前缀"骨架, 只是子路径 / query 不同。
# 旧版本给每种页面都写了一个 _url_* 函数, 加上散落在抓取函数里的若干字面量 URL,
# 整个脚本里 "if US / elif HK / else raise" 骨架出现了 10+ 遍。统一到一个 helper:
#
#   _sa_url(ticker, market, path="statistics", params="p=trailing", api=False)
#     -> "https://stockanalysis.com/stocks/aapl/statistics/?p=trailing"
#
# 原来的 _url_* 函数保留为薄壳 (callsite 稳定), 仅内部实现换成 _sa_url。
# 几处散落在抓取函数内的字面量 (FCF trailing / FCF annual / FX 反算三页) 也改走 _sa_url。
def _sa_url(ticker: str, market: str, path: str = "",
            params: str = "", *, api: bool = False) -> str:
    tl = ticker.lower()
    if api:
        if   market == "US": base = f"https://api.stockanalysis.com/api/symbol/s/{tl}"
        elif market == "HK": base = f"https://api.stockanalysis.com/api/symbol/q/hkg-{ticker}"
        else: raise ValueError(f"unknown market: {market}")
    else:
        if   market == "US":  base = f"https://stockanalysis.com/stocks/{tl}"
        elif market == "HK":  base = f"https://stockanalysis.com/quote/hkg/{ticker}"
        elif market == "ETF": base = f"https://stockanalysis.com/etf/{tl}"
        else: raise ValueError(f"unknown market: {market}")
    url = f"{base}/{path}/" if path else f"{base}/"
    return f"{url}?{params}" if params else url


def _url_home(ticker: str, market: str) -> str:
    """主页 URL（用于抓当前/最新收盘价）。"""
    return _sa_url(ticker, market)


def _url_stats(ticker: str, market: str) -> str:
    """statistics 页 URL（用于抓财务指标）。"""
    return _sa_url(ticker, market, "statistics")


def _url_ratios(ticker: str, market: str, period: str = "trailing") -> str:
    """财务比率页 URL（含 TTM PE / EV-EBIT 历史数组）。

    period:
      - "trailing"  → 近 20 个季度 TTM（约 5 年，每季一点）
    """
    return _sa_url(ticker, market, "financials/ratios", f"p={period}")


def _url_income_quarterly(ticker: str, market: str) -> str:
    """季度损益表页 URL，含 `datekey`+`epsdil`+`opinc` 20 期季度数组。

    用于按"报告日"对齐每周股价，得到严谨的 TTM 序列（4 季滚动求和）。
    """
    return _sa_url(ticker, market, "financials", "p=quarterly")


def _url_history_weekly(ticker: str, market: str) -> str:
    """stockanalysis 官方历史股价 API，返回近 1 年周频 K 线（约 52 个点）。

    - US : /api/symbol/s/{ticker}/history
    - HK : /api/symbol/q/hkg-{code}/history
    """
    return _sa_url(ticker, market, "history",
                   "type=stock&range=1Y&period=Weekly", api=True)


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


# siblisresearch.com 的 Nasdaq 100 页在 2026 年前后从 WordPress (supsystic 插件)
# 迁到 Astro, 老页面的 <table class="supsystic-table ...">+季末行结构已不复存在。
# 新页面结构 (2026-11 实测):
#   - 页面上有多个普通 <table>, 其中“历史表”第一行表头为:
#       Month | U.S. Large Cap | NASDAQ 100 | Russell 2000 | Russell 1000 Growth | Russell 1000 Value
#     NASDAQ 100 的 P/E (TTM) 位于列索引 2 (0-based)。
#   - 行结构: <th scope="row">Sept 2026</th><td class="n"><span>35.09</span></td>...
#   - 日期格式分两种: 最近一行是 "<MonAbbr> YYYY" (如 "Sept 2026"), 其余是
#     "YYYY <MonAbbr>" (如 "2025 Dec", 为该年末快照); 都要归一化到月末 ISO 日期。
#   - 频率变成"最新月份点 + 历年 12 月点", 不再是季末。免费可见约 10+ 期, 覆盖 10 年。
#   - 偶有 "Negative" 字符串占位 (如 Russell 2000 2020 Dec), 对 NDX 列目前没出现过,
#     但解析器仍需健壮跳过非数字值。

# 月份缩写 -> 月号。siblisresearch 页面偶尔用非标准 "Sept" (而非 "Sep"), 显式枚举以兼容。
_MON_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_siblis_date(raw: str) -> dt.date | None:
    """接受 siblisresearch 新版表的日期字符串, 归一化为该月月末日期。

    支持的形式:
        "Sept 2026"   -> 2026-09-30   ("最近一期"行, 月份缩写在前)
        "Sep 2026"    -> 2026-09-30   (标准三字母缩写, 以防站点以后统一)
        "2025 Dec"    -> 2025-12-31   (历年年末行, 年在前)
        "6/30/2026"   -> 2026-06-30   (兼容旧 WordPress 版 M/D/Y, 以防回滚)
    """
    s = raw.strip()
    if not s:
        return None

    # 旧版 M/D/Y 兼容路径
    if "/" in s:
        try:
            return dt.datetime.strptime(s, "%m/%d/%Y").date()
        except ValueError:
            return None

    parts = s.split()
    if len(parts) != 2:
        return None
    a, b = parts
    # 判定哪一个 token 是年, 哪一个是月
    if a.isdigit() and len(a) == 4:
        year_tok, mon_tok = a, b
    elif b.isdigit() and len(b) == 4:
        year_tok, mon_tok = b, a
    else:
        return None
    try:
        year = int(year_tok)
    except ValueError:
        return None
    mon = _MON_ABBR.get(mon_tok.lower().rstrip("."))
    if mon is None:
        return None

    # 月末日 = 下月 1 号 - 1 天 (无需外部库)
    if mon == 12:
        last_day = dt.date(year, 12, 31)
    else:
        last_day = dt.date(year, mon + 1, 1) - dt.timedelta(days=1)
    return last_day


def parse_siblis_ndx_pe(html: str, limit: int = 60) -> list[tuple[str, float]]:
    """解析 siblisresearch.com Nasdaq-100 P/E 表, 返回 [(date_iso, pe), ...] 旧->新序列。

    - 数据源 (免费部分): https://siblisresearch.com/data/nasdaq-100-pe-ratio/
    - 2026 年起站点改版: 频率变为"最新月份 + 历年 12 月", 免费可见约 10+ 期。
    - 定位历史表的办法: 遍历页面上所有 <table>, 找表头同时包含 "Month" 和
      "NASDAQ 100" 列的那张; 读表头确定 NDX 所在列索引 (抗未来列顺序调整)。
    - `limit` 起截断作用, 和旧版签名保持一致。
    """
    tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    target_rows: list[str] | None = None
    ndx_col: int = -1

    def _cells_text(row_html: str) -> list[str]:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.DOTALL)
        return [re.sub(r'<[^>]+>|\s+', ' ', c).strip() for c in cells]

    for tbl in tables:
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tbl, re.DOTALL)
        if len(rows) < 2:
            continue
        header = _cells_text(rows[0])
        if not header:
            continue
        # 表头第 1 列必须是 "Month" (大小写宽松), 且其中某列是 "NASDAQ 100"
        if header[0].strip().lower() != "month":
            continue
        try:
            ndx_col = next(i for i, h in enumerate(header)
                           if "nasdaq" in h.lower() and "100" in h)
        except StopIteration:
            continue
        target_rows = rows[1:]
        break

    if not target_rows or ndx_col < 0:
        return []

    out: list[tuple[str, float]] = []
    for row in target_rows:
        cells = _cells_text(row)
        if len(cells) <= ndx_col:
            continue
        d = _parse_siblis_date(cells[0])
        if d is None:
            continue
        pe_raw = cells[ndx_col].replace(",", "").strip()
        try:
            pe = float(pe_raw)
        except ValueError:
            continue  # 跳过 "Negative" 或其他非数值占位
        out.append((d.strftime("%Y-%m-%d"), pe))

    # 页面按新->旧排列, 截断后反转为旧->新
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

# SSGA HTML 里"Est. 3-5 Year EPS Growth"表格行, 与 FY1 P/E 完全同款结构:
#   <th ... > Est. 3-5 Year EPS Growth <...> </th> <td class="data">18.43%</td>
# 数值带 "%" 后缀, 抓取后剥掉再转 float.
_PAT_SSGA_EPS_GROWTH = re.compile(
    r'Est\.\s*3-5\s*Year\s+EPS\s+Growth.*?<td[^>]*class="data"[^>]*>\s*'
    r'([\d.,\-]+)\s*%?\s*</td>',
    re.DOTALL | re.IGNORECASE,
)


def _parse_ssga_metric(url: str, pattern: re.Pattern[str],
                       sane_range: tuple[float, float]) -> float | None:
    """SSGA ETF 页面指标抓取公用脚手架。

    两个 metric (FY1 P/E、3-5Y EPS Growth) 走的是同款表格行结构, 只是正则匹配的 label
    和 sanity 区间不同。抽到这里避免同一段 _get + pat.search + float + 区间校验的样板
    代码重复两次。sane_range 为 (lo, hi) 闭区间, 越界视为解析异常返回 None。
    """
    html_str = _get(url)
    if not html_str:
        return None
    m = pattern.search(html_str)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    lo, hi = sane_range
    if not (lo <= v <= hi):
        return None
    return v


def fetch_ssga_forward_pe(url: str) -> float | None:
    """抓 SSGA ETF 页面, 提取 Price/Earnings Ratio FY1 (weighted harmonic FY1)。

    SSGA 每月月末更新, 数据源 FactSet Estimates。抓取失败或数值不合理时返回 None。
    sanity 区间: FY1 P/E 通常在 5 ~ 200 之间, 这里给到 (0, 500] 的宽松上限。
    """
    # 用 nextafter 之类不划算; 直接用一个极小正数做下界即可 (0 不合法, 需 >0)
    return _parse_ssga_metric(url, _PAT_SSGA_FY1, (1e-9, 500))


def fetch_ssga_eps_growth(url: str) -> float | None:
    """抓 SSGA ETF 页面, 提取 Est. 3-5 Year EPS Growth (加权平均, FactSet Estimates)。

    返回百分比数值 (例如 18.43 代表 18.43%)。抓取失败或数值不合理返回 None。
    sanity 区间: 3-5Y EPS growth 通常 -20% ~ +50%, 这里给到 [-50, 100]。
    """
    return _parse_ssga_metric(url, _PAT_SSGA_EPS_GROWTH, (-50, 100))


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


# ---------- 解析：ratios 页（TTM 时间序列，如 PE / EV-EBIT） ----------

# 站点在 ratios 页面里以 datekey:[...] 和 <field>:[...] 的形式嵌入
# 20 个季度点的 TTM 时间序列（首个通常是 "TTM"，其余是季末日期）。
# 注意：某些季度分母极小时站点会返回 null（如 AMZN 疫情期 pe），
# 因此数组内除数字外还可能出现字面量 null。
_PAT_DATEKEY = re.compile(r'datekey:\[([^\]]+)\]')


def _parse_sa_js_str_arr(html: str, key: str) -> list[str]:
    """从 stockanalysis 页面抓 `<key>:["x","y",...]` 这种带引号的字符串数组。

    用于 datekey / fiscalYear / fiscalQuarter 这类日期/标签序列。找不到或空数组
    均返回 []，调用方按长度判断即可。
    """
    m = re.search(rf'\b{re.escape(key)}\s*:\s*\[([^\]]+)\]', html)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))


def _first_non_null_num(parts: list[str]) -> float | None:
    """从 "数值字符串列表" 里取第一个能解析为 float 的值, 跳过 '' / 'null' / 坏值。

    stockanalysis 的 JS 数组 (fcf / opinc / netinccmn ...) 常把未披露季填 null,
    这个辅助函数把 "新→旧 顺序取最新一个有效值" 这个高频小动作集中处理。
    """
    for p in parts:
        p = p.strip()
        if not p or p == "null":
            continue
        try:
            return float(p)
        except ValueError:
            continue
    return None


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


# ---------- Archive (ValuationSnapshot.xml) 用的"完整季度原子数据"抓取 ----------
#
# 主表格展示只需要 TTM 口径, 走 statistics 页一个数字就够。但 archive XML 要存
# "每一期的原子数据", 所以额外抓两份页面:
#   1. financials/?p=quarterly       -> revenue / opinc / ebitda / netinccmn / epsdil 数组
#   2. financials/balance-sheet/     -> shares_out_diluted / debt / cash 数组
# 两份数据按 datekey (期末日期) 对齐组装成 period 列表, 再写入 archive。
# 字段缺失时 (SA 字段名漂移 / HK 公司信息披露不全) 该字段位置为 None, archive 侧写 "N/A"。

# SA balance-sheet 页的字段名有若干常见变体, 为了鲁棒先尝试一组候选, 哪个有数据用哪个。
# 20 年里 SA 若再改字段名, 加候选即可, 无需改调用点。
_BS_SHARES_CANDIDATES = ["sharesoutdiluted", "shsoutdiluted", "shsdil",
                         "sharesoutstanding", "shsout", "sharesout"]
_BS_DEBT_TOTAL        = ["totaldebt", "debt"]
_BS_DEBT_PARTS        = [("longtermdebt", "shorttermdebt"), ("ltdebt", "stdebt")]
_BS_CASH_TOTAL        = ["cashstinv", "totalcash", "cashandequiv", "cashequiv", "cash"]
_BS_CASH_PARTS        = [("cashequiv", "shortterminv"), ("cash", "stinv")]


def _first_array_match(html: str, candidates: list[str]) -> list[float | None]:
    """按顺序尝试多个候选 JS 字段名, 返回第一个匹配到的数组。都没有返回 []."""
    for name in candidates:
        arr = _parse_quarterly_array(html, name)
        if arr:
            return arr
    return []


def _combine_parts(a: list[float | None], b: list[float | None]) -> list[float | None]:
    """逐元素相加, 任一为 None 则该位置为 None. 用于 longtermdebt + shorttermdebt 拆分合成。"""
    n = min(len(a), len(b))
    out: list[float | None] = []
    for i in range(n):
        if a[i] is None or b[i] is None:
            out.append(None)
        else:
            out.append(a[i] + b[i])
    return out


def _fetch_quarterly_income_pack(ticker: str, market: str) -> dict | None:
    """抓季度损益表, 返回按 asof 对齐的字段 dict (或 None 表示抓取失败)。

    返回结构:
      {
        "dates":            [YYYY-MM-DD, ...]  (新->旧),
        "revenue":          [float|None, ...],
        "gross_profit":     [float|None, ...],
        "operating_income": [float|None, ...],
        "ebit":             [float|None, ...],
        "ebitda":           [float|None, ...],
        "net_income":       [float|None, ...],
        "eps_diluted":      [float|None, ...],
      }
    所有列表长度 == len(dates)。某字段 SA 未提供时整列为 None。

    注意: SA 的季度 income statement 页通常只提供 `opinc` 一个字段作为经营性利润,
    并没有独立的 `ebit` JS 数组 (SA 在 TTM statistics 页上用 opinc 等同于 EBIT)。
    这里仍然为 `ebit` 列预留抓取尝试, 一旦抓不到就留空由 archive 侧写 "N/A",
    用户可日后手动从 10-Q/10-K 补齐 "operating_income + non_operating_income"。
    `gross_profit` 对应 SA 的 `grossprofit` 字段, 几乎所有公司页都有。
    """
    html = _get(_url_income_quarterly(ticker, market))
    if not html:
        print(f"  .. ARCHIVE income {ticker}: page not accessible", file=sys.stderr)
        return None
    dk = _PAT_DATEKEY.search(html)
    if not dk:
        return None
    dates = [s.strip().strip('"') for s in dk.group(1).split(",")]
    # 过滤掉 "TTM" 这种非日期标签, 只保留真实财季末日期
    valid_idx: list[int] = []
    for i, d in enumerate(dates):
        try:
            dt.date.fromisoformat(d)
            valid_idx.append(i)
        except ValueError:
            pass
    def _pick(arr: list[float | None]) -> list[float | None]:
        return [arr[i] if i < len(arr) else None for i in valid_idx]
    revenue    = _pick(_parse_quarterly_array(html, "revenue"))
    grossprof  = _pick(_first_array_match(html, ["grossprofit", "gp"]))
    opinc      = _pick(_parse_quarterly_array(html, "opinc"))
    ebit       = _pick(_first_array_match(html, ["ebit"]))
    ebitda     = _pick(_first_array_match(html, ["ebitda"]))
    netinc     = _pick(_first_array_match(html, ["netinccmn", "netincome"]))
    eps_dil    = _pick(_parse_quarterly_array(html, "epsdil"))
    dates_kept = [dates[i] for i in valid_idx]
    return {
        "dates":            dates_kept,
        "revenue":          revenue,
        "gross_profit":     grossprof,
        "operating_income": opinc,
        "ebit":             ebit,
        "ebitda":           ebitda,
        "net_income":       netinc,
        "eps_diluted":      eps_dil,
    }


def _fetch_quarterly_cashflow_pack(ticker: str, market: str) -> dict | None:
    """抓季度现金流量表, 返回按 asof 对齐的字段 dict (或 None 表示抓取失败)。

    返回结构:
      {
        "dates":                     [YYYY-MM-DD, ...]  (新->旧),
        "operating_cash_flow":       [float|None, ...],  # 正常为正
        "capital_expenditure":       [float|None, ...],  # SA 原样 (通常负数)
        "free_cash_flow":            [float|None, ...],  # SA 直接提供
        "dividends_paid":            [float|None, ...],  # SA 原样 (通常负数)
        "share_repurchases":         [float|None, ...],  # SA 原样 (通常负数)
        "stock_based_compensation":  [float|None, ...],  # 非现金费用, 正常为正
      }

    SA 的 cash-flow-statement 页字段名随版本有若干变体, 为鲁棒起见每项都用候选列表
    fallback, 全部抓不到的字段整列为 None, archive 侧写 "N/A"。
    """
    url = _sa_url(ticker, market, "financials/cash-flow-statement", "p=quarterly")
    html = _get(url)
    if not html:
        print(f"  .. ARCHIVE cashflow {ticker}: page not accessible", file=sys.stderr)
        return None
    dk = _PAT_DATEKEY.search(html)
    if not dk:
        return None
    dates = [s.strip().strip('"') for s in dk.group(1).split(",")]
    valid_idx: list[int] = []
    for i, d in enumerate(dates):
        try:
            dt.date.fromisoformat(d)
            valid_idx.append(i)
        except ValueError:
            pass
    def _pick(arr: list[float | None]) -> list[float | None]:
        return [arr[i] if i < len(arr) else None for i in valid_idx]
    ocf   = _pick(_first_array_match(html, ["ncfo", "operatingcashflow", "cfo"]))
    capex = _pick(_first_array_match(html, ["capex", "capitalexpenditure"]))
    fcf   = _pick(_first_array_match(html, ["fcf", "freecashflow"]))
    div   = _pick(_first_array_match(html, ["dividend", "commdividends",
                                             "dividendspaid", "divpaid"]))
    buyb  = _pick(_first_array_match(html, ["buyback", "repurchases",
                                             "sharerepurchases"]))
    sbc   = _pick(_first_array_match(html, ["sbc", "stockcomp",
                                             "stockbasedcomp"]))
    dates_kept = [dates[i] for i in valid_idx]
    return {
        "dates":                    dates_kept,
        "operating_cash_flow":      ocf,
        "capital_expenditure":      capex,
        "free_cash_flow":           fcf,
        "dividends_paid":           div,
        "share_repurchases":        buyb,
        "stock_based_compensation": sbc,
    }


def _fetch_quarterly_balance_pack(ticker: str, market: str) -> dict | None:
    """抓季度资产负债表, 返回按 asof 对齐的字段 dict。

    SA 的 balance-sheet 页字段名有变体, 这里按候选列表 fallback:
      - debt : 优先 totaldebt; 否则 longtermdebt + shorttermdebt
      - cash : 优先 cashstinv/totalcash; 否则 cashequiv + shortterminv
    """
    url = _sa_url(ticker, market, "financials/balance-sheet", "p=quarterly")
    html = _get(url)
    if not html:
        print(f"  .. ARCHIVE balance {ticker}: page not accessible", file=sys.stderr)
        return None
    dk = _PAT_DATEKEY.search(html)
    if not dk:
        return None
    dates = [s.strip().strip('"') for s in dk.group(1).split(",")]
    valid_idx: list[int] = []
    for i, d in enumerate(dates):
        try:
            dt.date.fromisoformat(d)
            valid_idx.append(i)
        except ValueError:
            pass
    def _pick(arr: list[float | None]) -> list[float | None]:
        return [arr[i] if i < len(arr) else None for i in valid_idx]

    shares = _pick(_first_array_match(html, _BS_SHARES_CANDIDATES))

    debt_total = _first_array_match(html, _BS_DEBT_TOTAL)
    if debt_total:
        debt = _pick(debt_total)
    else:
        debt = []
        for lt_name, st_name in _BS_DEBT_PARTS:
            lt = _parse_quarterly_array(html, lt_name)
            st = _parse_quarterly_array(html, st_name)
            if lt and st:
                debt = _pick(_combine_parts(lt, st)); break

    cash_total = _first_array_match(html, _BS_CASH_TOTAL)
    if cash_total:
        cash = _pick(cash_total)
    else:
        cash = []
        for c_name, si_name in _BS_CASH_PARTS:
            c = _parse_quarterly_array(html, c_name)
            si = _parse_quarterly_array(html, si_name)
            if c and si:
                cash = _pick(_combine_parts(c, si)); break

    dates_kept = [dates[i] for i in valid_idx]
    return {
        "dates":              dates_kept,
        "shares_out_diluted": shares,
        "debt":               debt,
        "cash":               cash,
    }


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
    return _sa_url(ticker, market, "history",
                   "type=stock&range=5D&period=Daily", api=True)


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


def _apply_intraday_fallback(row: dict, ticker: str, market: str) -> None:
    """盘中回退: 若 row 的 AsOf 比"最近已收盘日线"更新 (说明是盘中快照), 把 close+AsOf
    替换为前一个已收盘日的 (close, date)。

    行为与原来 _fetch_etf / fetch_ticker 里逐字重复的两段完全一致 (日志、字段写回),
    抽到这里以消除重复。ETF 场景传 market="US" 即可 (日线 API 对 ETF 兼容 type=stock)。
    """
    asof_iso = _asof_to_iso(row.get(CLOSE_DATE_COL))
    last = _fetch_last_daily_close(ticker, market)
    if last is None:
        return
    prev_date, prev_close = last
    # 只有当 web 页面日期显著地"新于"日线最后一条时才判定为盘中
    if not (asof_iso and asof_iso > prev_date):
        return
    prev_asof = _iso_to_asof(prev_date) or prev_date
    print(
        f"  .. {ticker}: intraday snapshot detected "
        f"(\"{row[CLOSE_DATE_COL]}\") -> falling back to previous close "
        f"{prev_close:g} @ {prev_asof}",
        file=sys.stderr,
    )
    row[CLOSE_COL] = f"{prev_close:g}"
    row[CLOSE_DATE_COL] = prev_asof


# 从 parse_home 返回的 asof 字符串中提取"日期部分"，例如：
#   "Aug 7, 2026, 11:55 AM HKT" -> "Aug 7, 2026"
#   "Aug 6, 2026"                -> "Aug 6, 2026"
_PAT_ASOF_DATE = re.compile(
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s*20\d{2}'
)
_MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1
)}
# 反向映射: 1 → "Jan". 由 _MONTHS 单一事实源派生, 避免写两份字面量易漂移。
_MONTH_NAMES = sorted(_MONTHS, key=_MONTHS.get)


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
    url = _sa_url(ticker, market, "financials/cash-flow-statement", "p=trailing")

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
    dates_all = _parse_sa_js_str_arr(html, "datekey")

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
    frequency = "quarterly"  # 默认
    if len(dates_kept) >= 2:
        gaps_days: list[int] = []
        for i in range(len(dates_kept) - 1):
            try:
                d0 = dt.date.fromisoformat(dates_kept[i])
                d1 = dt.date.fromisoformat(dates_kept[i + 1])
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
            d_head = dt.date.fromisoformat(dates_kept[0])
            keep_n = len(fcf_series)
            for i in range(1, len(dates_kept)):
                try:
                    di = dt.date.fromisoformat(dates_kept[i])
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
    fy_vals = _parse_sa_js_str_arr(html, "fiscalYear")
    asof = fy_vals[0] if fy_vals else None
    # 页面可能还有 fiscalQuarter: 用它补齐 "2026 Q3" 之类
    fq_vals = _parse_sa_js_str_arr(html, "fiscalQuarter")
    if asof and fq_vals:
        asof = f"{asof} {fq_vals[0]}"

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
        "history_source":  "ttm",       # 历史序列来源: 'ttm' (season/半年/季 TTM) | 'annual' (fallback 到年报)
    }


def _fetch_sa_fcf_annual(ticker: str, market: str) -> dict | None:
    """抓 stockanalysis 年度现金流表 (不带 ?p=trailing) 的 FCF 序列, 用于 5Y TTM 数据不足时 fallback。

    背景
    ----
    _fetch_sa_fcf 抓的 trailing 页对不同披露频率公司返回的点数差异很大:
      - 美股 / 大部分港股 : 20 个季度点 (覆盖 ~5 年)
      - 半年报港股         : 10 个半年度点 (覆盖 ~5 年)
      - 但某些新上市 / 报表体系变化 / 站点数据不全的公司, trailing 页可能只有 1-3 个点,
        无法算 3Y/5Y CAGR
    这时回退到年报页 (默认视图) 是最稳的选择 —— 年报页通常提供 10 年历史。

    Returns
    -------
    dict | None
        None: 页面访问失败 / 解析不到年度 fcf。
        dict: {"fcf_series": [新→旧年度值], "fcf_dates": [YYYY-MM-DD 财年末], "asof": str,
               "source_url": str, "report_currency": str | None}
        注意: 不返回 fcf_ttm, 因为年报值不是 TTM 口径 (在 _fetch_dcf 里由 trailing 页值优先填充)。
    """
    if market == "ETF":
        return None
    url = _sa_url(ticker, market, "financials/cash-flow-statement")

    html = _get(url)
    if not html:
        print(f"  .. SA FCF annual {ticker}: page not accessible ({url})", file=sys.stderr)
        return None

    # 与 trailing 页同款正则; 数组顺序也是"新→旧"
    m = re.search(r'\bfcf\s*:\s*\[([^\]]+)\]', html)
    if not m:
        print(f"  !! SA FCF annual {ticker}: 'fcf' array not found", file=sys.stderr)
        return None
    dates_all = _parse_sa_js_str_arr(html, "datekey")

    # 关键: 年报页的 datekey 数组第 0 位通常是字面量 "TTM" (代表最新 TTM 值), 后续才是
    # "2025-12-31" / "2024-12-31" 这种财年末日期. 我们只保留"能解析为日期"的年度点,
    # 避免污染前端按 datekey 匹配 N 年前那期的逻辑; TTM 值本身在 _fetch_dcf 里已由
    # trailing 页负责提供, 这里不需要重复.
    fcf_series: list[float] = []
    dates_kept: list[str]   = []
    for i, p in enumerate([s.strip() for s in m.group(1).split(",")]):
        if not p or p == "null":
            continue
        try:
            v = float(p)
        except ValueError:
            continue
        # 该点必须有对应的 YYYY-MM-DD 财年末日期, 否则跳过 (含 "TTM" 标签)
        if i >= len(dates_all):
            continue
        dk = dates_all[i]
        try:
            dt.date.fromisoformat(dk)
        except (ValueError, TypeError):
            continue
        fcf_series.append(v)
        dates_kept.append(dk)

    if not fcf_series:
        print(f"  !! SA FCF annual {ticker}: no valid annual (dated) fcf values", file=sys.stderr)
        return None

    # 保留最多 10 个年度点 (~10 年), CAGR 只需 6 年就够 (含最新一年 + 5 年前那年)
    fcf_series = fcf_series[:10]
    dates_kept = dates_kept[:10]

    asof = None
    fy_vals = _parse_sa_js_str_arr(html, "fiscalYear")
    if fy_vals:
        asof = fy_vals[0]

    report_currency: str | None = None
    mc = re.search(r'Financials\s+in\s+\w+\s+([A-Z]{3})', html)
    if mc:
        report_currency = mc.group(1)

    print(f"  ok SA FCF annual {ticker}: series_len={len(fcf_series)} "
          f"span={dates_kept[-1] if dates_kept else '?'}→{dates_kept[0] if dates_kept else '?'} "
          f"{report_currency or '?'}", file=sys.stderr)
    return {
        "fcf_series":      fcf_series,
        "fcf_dates":       dates_kept,
        "asof":            asof,
        "source_url":      url,
        "report_currency": report_currency,
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
    url_st = _sa_url(ticker, "HK", "statistics")
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
    url_is = _sa_url(ticker, "HK", "financials", "p=trailing")
    html_is = _get(url_is)
    is_first: dict[str, float] = {}
    is_asof: str | None = None
    if html_is:
        for key in ("opinc", "netinccmn"):
            m = re.search(rf'\b{key}\s*:\s*\[([^\]]+)\]', html_is)
            if m:
                v = _first_non_null_num(m.group(1).split(","))
                if v is not None:
                    is_first[key] = v
        dks = _parse_sa_js_str_arr(html_is, "datekey")
        if dks:
            is_asof = dks[0]

    # 抓 cash-flow 页 (报表原币, fcf 首值)
    url_cf = _sa_url(ticker, "HK", "financials/cash-flow-statement", "p=trailing")
    html_cf = _get(url_cf)
    cf_fcf: float | None = None
    if html_cf:
        m = re.search(r'\bfcf\s*:\s*\[([^\]]+)\]', html_cf)
        if m:
            cf_fcf = _first_non_null_num(m.group(1).split(","))

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

    # 5Y 历史 CAGR fallback: 如果 trailing 页拿到的 TTM 序列跨度不足 ~4.5 年
    # (点数少 或 首尾日期太近), 就再抓年报页作为历史序列替代来源, 保留原 fcf_ttm 不变
    # (TTM 值口径更真实)。前端 tooltip 会把 history_source 显示为 "年报 (TTM 数据不足)"
    # 的小字说明, 保证用户可见。
    ttm_series = fcf.get("fcf_series") or []
    ttm_dates  = fcf.get("fcf_dates")  or []
    span_years = 0.0
    if len(ttm_dates) >= 2:
        try:
            d0 = dt.date.fromisoformat(ttm_dates[0])
            dN = dt.date.fromisoformat(ttm_dates[-1])
            span_years = (d0 - dN).days / 365.25
        except (ValueError, TypeError):
            span_years = 0.0
    history_source = "ttm"
    # 判定"不足 5Y": 点数少于 5 或 跨度不到 4.5 年 (5Y CAGR 最少需要 ~4 年真实跨度)
    if len(ttm_series) < 5 or span_years < 4.5:
        print(f"  .. {ticker}: TTM series insufficient for 5Y CAGR "
              f"(len={len(ttm_series)}, span={span_years:.2f}y), trying annual fallback...",
              file=sys.stderr)
        ann = _fetch_sa_fcf_annual(ticker, market)
        if ann and len(ann.get("fcf_series") or []) >= 2:
            # 只替换历史序列部分, 保留 fcf_ttm/asof 用于卡片"当前 FCF" 显示
            fcf["fcf_series"]  = ann["fcf_series"]
            fcf["fcf_dates"]   = ann["fcf_dates"]
            fcf["frequency"]   = "annual"
            history_source     = "annual"
            # 若报表币在两页 (trailing / annual) 不一致 (极少见), 以 annual 页为准
            if ann.get("report_currency") and not fcf.get("report_currency"):
                fcf["report_currency"] = ann["report_currency"]
        else:
            print(f"  !! {ticker}: annual fallback also failed / too short; "
                  f"long-horizon CAGR will show N/A", file=sys.stderr)

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
        "history_source":  history_source,   # 'ttm' | 'annual'; 前端 tooltip 显示对应小字说明
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
    _apply_intraday_fallback(row, ticker, "US")

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
    # SPYM (SSGA HTML)     -> forward_pe, eps_growth        ; roe 保持 None (SSGA 不发布)
    # QQQM (Invesco JSON)  -> forward_pe,           roe     ; eps_growth 保持 None
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

    # -------- 指数代理: 历史 P/E 时序 --------
    # 5Y = 最近 60 个月度点 (SPX) / 或 ~10 个月度+年度点 (NDX 免费版 2026 改版后), 3Y = 5Y 尾部切片，
    # 1Y = 5Y 数组的更小尾部切片（月度 12 点 / NDX 则多为最近年度+当月 2 点）。
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
                    #       (SPYM: multpl 每天都会更新一个"今日"点, 差 ~1 天; QQQM 改版后当月点通常已很新, 绝大多数情况走此分支)
                    #     * 距今  > STALE_DAYS: 数据源点太老, 保留原点作为"历史点"不动,
                    #       在末尾追加一个日期=今天 / 值=snap 的新点。曲线尾段会拉出一条直线,
                    #       诚实反映"数据源空档期" (NDX 改版前常态: 6/30 -> today 追加一个点; 改版后末点是当月点, 极少触发)。
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
            # 5Y / 3Y / 1Y 尾部切片 (全部按**时间窗口**切, 不再按点数切):
            #   * siblis_ndx: 2026 改版后口径为"历年 Dec + 当月最新"的混合序列, 点间距非均匀
            #     (多数年份只有 1 个 Dec 点), 共 ~11 点覆盖 ~10 年. 若按点数切:
            #       - 5Y 会把全部 11 点吐出来 → 跨度 ~10 年
            #       - 3Y 取 12 点 → 跨度 ~10 年
            #     完全失真. 按时间窗口切后: 5Y 吐 5 个 Dec 点 + 当月点 (真 5 年),
            #     3Y 吐 3 个 Dec 点 + 当月点 (真 3 年), 1Y 吐 1~2 点 (真 1 年).
            #   * multpl_spx: 严格月频, 按时间窗口切与按点数切 (60/36/12) 结果一致, 更鲁棒,
            #     未来若数据源频率变化不会被打回原形.
            #   窗口内不足 2 点时兜底取尾部 2 点, 保证曲线至少能画一段.
            def _slice_by_years(hist: list[tuple[str, float]], years: int) -> list[tuple[str, float]]:
                """按日期窗口裁切: 保留末点往前 `years` 年内的所有点, 至少 2 点."""
                if len(hist) <= 2:
                    return list(hist)
                try:
                    last_dt = dt.date.fromisoformat(hist[-1][0])
                except ValueError:
                    return list(hist)
                try:
                    cutoff = last_dt.replace(year=last_dt.year - years)
                except ValueError:
                    # 处理 2/29 等罕见情况 (本脚本月末日期不会命中, 兜底)
                    cutoff = last_dt - dt.timedelta(days=365 * years)
                picked: list[tuple[str, float]] = []
                for d_str, v in hist:
                    try:
                        d_val = dt.date.fromisoformat(d_str)
                    except ValueError:
                        continue
                    if d_val >= cutoff:
                        picked.append((d_str, v))
                # 窗口内不足 2 点 -> 兜底取尾部 2 点, 保证至少能连成一条短线段
                if len(picked) < 2:
                    picked = list(hist[-2:])
                return picked

            r.pe_history    = _slice_by_years(pe_hist, 5)
            r.pe_history_3y = _slice_by_years(pe_hist, 3)
            r.pe_history_1y = _slice_by_years(pe_hist, 1)
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
    _apply_intraday_fallback(row, ticker, market)

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


# ---------- HTML 输出 ----------

import html
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
    return ""


def _cell_html(col: str, v: str | None, currency: str) -> str:
    if v is None or v == "":
        return '<td class="num na">—</td>'

    if col in _MONEY_COLS:
        num = _fmt_number(v)
        if num != "N/A" and re.match(r"^-?[\d,]+(?:\.\d+)?[TBM]?$", num):
            # 右下角小字币种标签: stockanalysis 的 statistics 页把这些金额列 (Market
            # Cap / EV / Debt / Cash&STI / EBIT-TTM) 都按报价币展示 (US=USD, HK=HKD),
            # 即使公司报表原币是 CNY, 这些数字也已经过 stockanalysis 内部即期换算.
            # 因此这里直接标注每行的报价币, 与表头一致.
            ccy = html.escape(currency or "")
            ccy_html = f'<span class="ccy" aria-hidden="true">{ccy}</span>' if ccy else ""
            return (f'<td class="num money">'
                    f'<span class="val">{html.escape(num)}</span>{ccy_html}</td>')
        return f'<td class="num">{html.escape(num)}</td>'

    if col in ("EV/EBIT", "EV/EBITDA", "PE", "PE Fwd"):
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
    #   - EPS Growth  (发行商: SSGA 3-5Y EPS Growth; Invesco 无此口径)
    #   - ROE         (发行商: Invesco returnOnEquity; SSGA 不发布, SPYM 该列留空)
    #   - PE          (ETF 加权 P/E, 来自 stockanalysis.com/etf/{t}/)
    #   - PE Fwd      (发行商官方加权前瞻 P/E: SSGA / Invesco)
    # 其余列 (MarketCap/EV/Debt/Cash/EBIT/EV·EBIT/EV·EBITDA) 对 ETF
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
  /* Money 列右下角超小字币种标签 (USD / HKD 等). 与右上角 .src-info 圆圈互不重叠;
     字号极小、颜色偏灰、不遮挡数字, 只做视觉提示. */
  td.money {{ position: relative; padding-bottom: 12px; }}
  td.money .ccy {{
    position: absolute;
    right: 6px; bottom: 2px;
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 600;
    letter-spacing: .02em;
    color: var(--muted);
    opacity: .72;
    pointer-events: none;
    user-select: none;
  }}
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
      Coloring uses rough rules of thumb (EV/EBIT ≤15 good ≤25 fair; P/E ≤20 good ≤30 fair). Not investment advice.
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
    // SSGA: FactSet 3-5Y 前瞻.
    const EPS_GROWTH_SOURCE = {eps_growth_source_json};
    // ticker -> ROE 单元格数据源标签 (仅 ETF).
    // Invesco (QQQM): fundCharacteristics API; SPYM 该列留空 (SSGA 不发布).
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

        // 历史序列来源提示: 'ttm' (常规季度/半年度 TTM) 或 'annual' (fallback 到年报)
        // 只在 fallback 情形下加个小字, 让用户知道 3Y/5Y CAGR 是用年报点计算的
        const hs = dcf.history_source || 'ttm';
        const histNote = (hs === 'annual')
          ? ` <span class="dcf-sub" title="TTM (季度/半年度) 序列不足以覆盖 ~5 年，已 fallback 到 stockanalysis 年报 FCF 序列，历史 CAGR 端点法用年度点计算。">· 历史 CAGR 用年报 (TTM 数据不足)</span>`
          : '';

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
             +     `<span>FCF₀ <b>${{fcf0OrigTxt}}</b>${{histNote}}</span>`
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
        const histSrc = dcf.history_source || 'ttm';
        // freqLabel 用于 tooltip; 年报 fallback 时明确标注, 让用户知道端点法用的是年度点
        const freqLabel = (histSrc === 'annual')
          ? '年度 (fallback: TTM 数据不足)'
          : ((freq === 'annual') ? '年度' : (freq === 'semiannual' ? '半年度' : '季度'));
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
      // 个股所有数据列 (Close / AsOf / MarketCap ... P/E Fwd) 走下面的 annotateStockCols.
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
      //     P/E(TTM) / P/E(Fwd)             -> statistics 页
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
    # 把所有要序列化为 JSON 字面量注入前端的字段统一走一次 dumps + ** 展开, 替代旧版
    # 11 行逐个 xx_json = json.dumps(xx) + 再在 .format(**...) 里逐个列字段的样板。
    _payload = {
        "pe_5y_json":              pe_5y,
        "ev_5y_json":              ev_5y,
        "pe_3y_json":              pe_3y,
        "ev_3y_json":              ev_3y,
        "pe_1y_json":              pe_1y,
        "ev_1y_json":              ev_1y,
        "pe_source_json":          pe_source,
        "fwd_source_json":         fwd_source,
        "pe_ttm_source_json":      pe_ttm_source,
        "eps_growth_source_json":  eps_growth_source,
        "roe_source_json":         roe_source,
        # ETF 集合注入前端, 用于动态隐藏对 ETF 无意义的 Tab (EV/EBIT / DCF)。
        "etf_set_json":            [t.upper() for t in ETFs],
        "logo_map_json":           LOGO_DOMAIN,
    }
    json_kwargs = {k: json.dumps(v, ensure_ascii=False) for k, v in _payload.items()}

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
        snapshot_json=snapshot_json,
        dcf_json=dcf_json,
        favicon_link=_favicon_link_tag(),
        vs_data_json=vs_data_json,
        **json_kwargs,
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


# ---------- ValuationSnapshot.xml (20 年归档) ----------
#
# 设计见项目里相关讨论; 核心约束:
#   1. append-only: 已存在的 <week>/<period> 一字不改 (用户会手动补 "N/A")。
#   2. 缺失值: 全局统一字符串 "N/A", 不省略 attribute 也不用 ""/null/0。
#   3. 幂等 key: ETF -> (symbol, week.asof); 个股 -> (symbol, period.asof)。
#   4. 格式稳定: 读用 ElementTree, 写用手工字符串拼接, 以保留 FIELD REGISTRY
#      注释块、attribute 顺序、用户手动改动。
#   5. 原子写: tmp + os.replace, 避免中断破坏历史文件。

ARCHIVE_XML_FILENAME = "ValuationSnapshot.xml"

# 字段登记表 (FIELD REGISTRY). 更改字段时需要同步调整 _ARCHIVE_HEADER。
# 新增字段的 "20 年合同": 加 attribute + 下方登记一行 + minor+1.
_ARCHIVE_SCHEMA = "1"
_ARCHIVE_MINOR  = "2"
_ARCHIVE_HEADER = f"""<?xml version="1.0" encoding="UTF-8"?>
<!--
  ValuationSnapshot history archive
  ================================================================
  LAYOUT
    <snapshots schema="N" minor="M">
      <etf_weekly>           : one row per ETF per week
        <quote>              : one per ETF
          <week>             : key = asof (Friday close date), unique, idempotent
      <fundamentals>         : quarterly atomic data for US/HK stocks
        <quote>              : one per stock
          <period>           : key = asof (period end), unique, idempotent

  EXTENSIBILITY RULES
    1. New ticker        -> add <quote>, schema unchanged
    2. New attribute     -> add attr + register below + bump minor
    3. Rename/redefine   -> bump schema (breaking)
    4. Structured data   -> use child elements, not attributes
    5. Missing value     -> literal "N/A" (never omit, never "" / null / 0)
                            Reader: v = e.get(k, "N/A"); x = float(v) if v != "N/A" else None
                            Users may replace "N/A" with real values by hand;
                            append-only guarantee ensures edits are preserved.

  SCHEMA EVOLUTION
    - Fields are append-only; new fields bump minor (e.g. 1.0 -> 1.1)
    - Historical rows written under an older minor NEVER get back-filled
      with the new attribute; those cells are naturally absent (treated
      as "N/A" by readers via `.get(k, "N/A")`).

  APPEND-ONLY GUARANTEE
    1. Script only appends new (symbol, asof) nodes
    2. Existing nodes are never touched, including hand edits
    3. Users may fill in "N/A" at any time; future runs will not overwrite

  FIELD REGISTRY (schema={_ARCHIVE_SCHEMA}, minor={_ARCHIVE_MINOR})
  ==== <etf_weekly>/<quote> ==== static meta ====
    symbol              since 1.0  ETF ticker
    market              since 1.0  constant "ETF"
    currency            since 1.0  quote ccy (ISO 4217)
  ==== <week> ==== weekly snapshot ====
    asof                since 1.0  Friday ISO date, UNIQUE KEY per (symbol)
    captured_week       since 1.0  script run date (ISO)
    price               since 1.0  Friday close, quote ccy
    pe_ttm              since 1.0  peRatio from SA /etf/ page, dimensionless
    pe_forward          since 1.0  forward PE from SSGA (SPYM) / Invesco (QQQM)
  ==== <fundamentals>/<quote> ==== static meta ====
    symbol              since 1.0  ticker; HK = 4-digit
    market              since 1.0  "US" | "HK"
    currency            since 1.0  quote ccy
    report_currency     since 1.0  reporting ccy; HK stocks may be CNY
  ==== <period> ==== quarterly atomic data ====
    asof                since 1.0  period-end ISO date, UNIQUE KEY per (symbol)
    label               since 1.0  "2026 Q3" / "2026 H1" / "FY2016"
    period_type         since 1.0  "quarterly" | "semiannual" | "annual"
    captured_week       since 1.0  script run date when first written (ISO)
    price_at_asof       since 1.0  close of first week captured after release, quote ccy
    ~~~~ income statement ~~~~
    revenue             since 1.0  quarterly revenue, report ccy, absolute units
    gross_profit        since 1.1  quarterly gross profit (SA grossprofit), report ccy
    operating_income    since 1.0  quarterly operating income (SA opinc), report ccy
    ebit                since 1.1  quarterly EBIT; SA rarely exposes this as a
                                   separate field, so it is usually "N/A" and
                                   can be filled in by hand from 10-Q / 10-K
    ebitda              since 1.0  quarterly EBITDA, report ccy
    net_income          since 1.0  quarterly net income (SA netinccmn), report ccy
    eps_diluted         since 1.0  quarterly diluted EPS, report ccy
    ~~~~ cash flow statement ~~~~
    operating_cash_flow      since 1.2  cash from operations (SA ncfo), report ccy;
                                        normally positive
    capital_expenditure      since 1.2  CapEx (SA capex), report ccy; SA stores as
                                        negative number (cash outflow), do NOT flip
    free_cash_flow           since 1.2  FCF = OCF + CapEx (SA fcf), report ccy;
                                        normally positive
    dividends_paid           since 1.2  cash dividends paid, report ccy; SA stores
                                        as negative (cash outflow)
    share_repurchases        since 1.2  share buyback cash out, report ccy; SA stores
                                        as negative
    stock_based_compensation since 1.2  SBC non-cash expense (SA sbc), report ccy;
                                        normally positive
    ~~~~ balance sheet (period-end) ~~~~
    shares_out_diluted  since 1.0  period-end diluted share count
    debt                since 1.0  period-end total debt, report ccy
    cash                since 1.0  period-end cash + STI, report ccy
-->
<snapshots schema="{_ARCHIVE_SCHEMA}" minor="{_ARCHIVE_MINOR}" generator="ValuationSnapshot.py">
  <etf_weekly>
  </etf_weekly>
  <fundamentals>
  </fundamentals>
</snapshots>
"""

# ETF <week> 的 attribute 顺序 (写入时的固定顺序, 读取时顺序无关)
_ETF_WEEK_ATTRS = ("asof", "captured_week", "price", "pe_ttm", "pe_forward")
# <period> 的 attribute 顺序
_PERIOD_ATTRS = (
    "asof", "label", "period_type", "captured_week", "price_at_asof",
    # Income statement
    "revenue", "gross_profit", "operating_income", "ebit", "ebitda",
    "net_income", "eps_diluted",
    # Cash flow statement
    "operating_cash_flow", "capital_expenditure", "free_cash_flow",
    "dividends_paid", "share_repurchases", "stock_based_compensation",
    # Balance sheet
    "shares_out_diluted", "debt", "cash",
)


def _fmt_archive_num(v) -> str:
    """数值 -> archive 字符串. 规则:
      - None/空串/无效  -> "N/A"
      - 整数值         -> 不带小数 (股数 / 绝对金额单位常是整数)
      - 浮点           -> 保留原精度, 最多 6 位有效数字
    """
    if v is None or v == "":
        return "N/A"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "N/A"
    if f != f:  # NaN
        return "N/A"
    # 整数样式: 大金额 / 股数
    if abs(f) >= 1e6 and float(int(f)) == f:
        return str(int(f))
    # 其余按 "最多 6 位有效数字, 去尾部 0"
    s = f"{f:.6g}"
    return s


def _attr_str(k: str, v: str) -> str:
    """构造一个 attribute 片段 (已做 XML attr-value 转义)."""
    esc = (v.replace("&", "&amp;")
             .replace('"', "&quot;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))
    return f'{k}="{esc}"'


def _build_etf_week_entries(rows: list[Row], run_date: str) -> list[dict]:
    """从 ETF Row 列表构造 archive 用的 week entry (每行一个 dict)。

    captured_week 用脚本运行日; asof 用 Row.data[CLOSE_DATE_COL] 转 ISO (= 周五收盘日)。
    若 asof 解析失败 (ETF 抓取整体失败) 该 Row 跳过。
    """
    out: list[dict] = []
    for r in rows:
        asof = _asof_to_iso(r.data.get(CLOSE_DATE_COL))
        if not asof:
            continue
        price = r.data.get(CLOSE_COL)
        pe_ttm = r.data.get(FIELDS["pe"])
        pe_fwd = r.data.get(FIELDS["peForward"])
        out.append({
            "symbol":        r.symbol,
            "market":        "ETF",
            "currency":      "USD",   # 当前所有 ETF 都是 USD; 未来加港股 ETF 时按需扩展
            "asof":          asof,
            "captured_week": run_date,
            "price":         _fmt_archive_num(price),
            "pe_ttm":        _fmt_archive_num(pe_ttm),
            "pe_forward":    _fmt_archive_num(pe_fwd),
        })
    return out


def _build_fundamentals_entries(ticker: str, market: str, currency: str,
                                 row: Row, run_date: str) -> list[dict]:
    """为单只个股抓季度原子数据, 组装 archive 用的 period entry 列表 (新->旧)。

    每个 dict 对应 archive 里的一个 <period>。缺失字段值一律写 "N/A"。
    若 income/balance 两份抓取都失败, 返回 []。
    """
    inc = _fetch_quarterly_income_pack(ticker, market)
    cfs = _fetch_quarterly_cashflow_pack(ticker, market)
    bal = _fetch_quarterly_balance_pack(ticker, market)
    if not inc and not cfs and not bal:
        return []
    # 以 income 的 dates 为主锚 (有 EPS/营收); cashflow/balance 按日期对齐
    dates_master: list[str] = (
        (inc or {}).get("dates", []) or
        (cfs or {}).get("dates", []) or
        (bal or {}).get("dates", [])
    )
    if not dates_master:
        return []

    cfs_idx: dict[str, int] = {}
    if cfs and cfs.get("dates"):
        for i, d in enumerate(cfs["dates"]):
            cfs_idx[d] = i
    bal_idx: dict[str, int] = {}
    if bal and bal.get("dates"):
        for i, d in enumerate(bal["dates"]):
            bal_idx[d] = i

    # 推断 period_type: 看相邻日期间隔中位数 (quarterly ~90d / semiannual ~180d / annual ~360d)
    period_type = "quarterly"
    try:
        gaps: list[int] = []
        for i in range(len(dates_master) - 1):
            d0 = dt.date.fromisoformat(dates_master[i])
            d1 = dt.date.fromisoformat(dates_master[i + 1])
            gaps.append(abs((d0 - d1).days))
        if gaps:
            gaps.sort()
            med = gaps[len(gaps) // 2]
            if   med >= 300: period_type = "annual"
            elif med >= 130: period_type = "semiannual"
            else:            period_type = "quarterly"
    except ValueError:
        pass

    def _label_for(date_str: str) -> str:
        """生成人类标签: quarterly -> 'YYYY QN'; semiannual -> 'YYYY H1/H2'; annual -> 'FYYYYY'."""
        try:
            d = dt.date.fromisoformat(date_str)
        except ValueError:
            return date_str
        if period_type == "annual":
            return f"FY{d.year}"
        if period_type == "semiannual":
            return f"{d.year} H{1 if d.month <= 6 else 2}"
        # quarterly
        q = (d.month - 1) // 3 + 1
        return f"{d.year} Q{q}"

    entries: list[dict] = []
    for i, date_str in enumerate(dates_master):
        ci = cfs_idx.get(date_str)
        bi = bal_idx.get(date_str)
        def _inc_val(key: str) -> str:
            if not inc: return "N/A"
            arr = inc.get(key, []) or []
            return _fmt_archive_num(arr[i]) if i < len(arr) else "N/A"
        def _cfs_val(key: str) -> str:
            if ci is None or not cfs: return "N/A"
            arr = cfs.get(key, []) or []
            return _fmt_archive_num(arr[ci]) if ci < len(arr) else "N/A"
        def _bal_val(key: str) -> str:
            if bi is None or not bal: return "N/A"
            arr = bal.get(key, []) or []
            return _fmt_archive_num(arr[bi]) if bi < len(arr) else "N/A"
        entries.append({
            "symbol":             ticker,
            "market":             market,
            "currency":           currency,
            # report_currency 走 DCF 流程才能拿到; 这里退而用 currency, 下游若已知可覆盖
            "report_currency":    row.dcf.get("report_currency") if row.dcf else currency,
            "asof":               date_str,
            "label":              _label_for(date_str),
            "period_type":        period_type,
            "captured_week":      run_date,
            # price_at_asof: 当前运行只知道"今日"的 close; 对最新一期可用, 对历史期用 N/A
            # (历史期的季报发布当周 close 已错过, 保留给用户手动补)
            "price_at_asof":      _fmt_archive_num(row.data.get(CLOSE_COL)) if i == 0 else "N/A",
            # ---- Income statement ----
            "revenue":                   _inc_val("revenue"),
            "gross_profit":              _inc_val("gross_profit"),
            "operating_income":          _inc_val("operating_income"),
            "ebit":                      _inc_val("ebit"),
            "ebitda":                    _inc_val("ebitda"),
            "net_income":                _inc_val("net_income"),
            "eps_diluted":               _inc_val("eps_diluted"),
            # ---- Cash flow statement ----
            "operating_cash_flow":       _cfs_val("operating_cash_flow"),
            "capital_expenditure":       _cfs_val("capital_expenditure"),
            "free_cash_flow":            _cfs_val("free_cash_flow"),
            "dividends_paid":            _cfs_val("dividends_paid"),
            "share_repurchases":         _cfs_val("share_repurchases"),
            "stock_based_compensation":  _cfs_val("stock_based_compensation"),
            # ---- Balance sheet ----
            "shares_out_diluted":        _bal_val("shares_out_diluted"),
            "debt":                      _bal_val("debt"),
            "cash":                      _bal_val("cash"),
        })
    return entries


def _read_archive_keys(xml_path: str) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """读 archive 已有的 (symbol, asof) key 集合, 分别返回 (etf_week_keys, period_keys).

    文件不存在/空文件/全空白 -> 返回两个空集合, 调用方据此走初始化路径(写入完整骨架)。
    文件存在但解析失败 -> 返回 sentinel 集合, 调用方据此拒绝写入, 保护用户已有的
    (可能是手动编辑中的) 文件内容, 避免被脚本意外覆盖。
    """
    import os
    import xml.etree.ElementTree as ET
    if not os.path.exists(xml_path):
        return set(), set()
    # 空文件 (0 字节) 或只有空白 -> 等同于"不存在", 走初始化。这个分支覆盖用户常见操作:
    #   在仓库里 `touch ValuationSnapshot.xml` 预建一个空壳文件, 期望脚本首次运行时填充。
    try:
        with open(xml_path, "rb") as f:
            raw = f.read()
        if not raw.strip():
            return set(), set()
    except OSError:
        pass
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        print(f"  !! archive parse failed: {e}; will NOT overwrite", file=sys.stderr)
        # 返回"全集"的 sentinel: 读失败时宁可什么都不写, 也不要毁掉文件
        # 这里用一个不可能匹配的 (symbol, asof) 单例, 让 _diff 逻辑清空所有待写
        return {("__PARSE_ERROR__", "")}, {("__PARSE_ERROR__", "")}
    root = tree.getroot()
    etf_keys: set[tuple[str, str]] = set()
    for q in root.findall('./etf_weekly/quote'):
        sym = q.get("symbol") or ""
        for w in q.findall('week'):
            asof = w.get("asof") or ""
            if sym and asof:
                etf_keys.add((sym, asof))
    period_keys: set[tuple[str, str]] = set()
    for q in root.findall('./fundamentals/quote'):
        sym = q.get("symbol") or ""
        for p in q.findall('period'):
            asof = p.get("asof") or ""
            if sym and asof:
                period_keys.add((sym, asof))
    return etf_keys, period_keys


def _format_week_line(entry: dict) -> str:
    attrs = " ".join(_attr_str(k, entry[k]) for k in _ETF_WEEK_ATTRS)
    return f'      <week {attrs}/>'


def _format_period_line(entry: dict) -> str:
    attrs = " ".join(_attr_str(k, entry[k]) for k in _PERIOD_ATTRS)
    return f'      <period {attrs}/>'


def _format_etf_quote_block(sym: str, market: str, currency: str, week_lines: list[str]) -> str:
    header = (f'    <quote symbol="{sym}" market="{market}" currency="{currency}">')
    body   = "\n".join(week_lines)
    return f'{header}\n{body}\n    </quote>'


def _format_fund_quote_block(sym: str, market: str, currency: str,
                              report_currency: str, period_lines: list[str]) -> str:
    header = (f'    <quote symbol="{sym}" market="{market}" '
              f'currency="{currency}" report_currency="{report_currency}">')
    body   = "\n".join(period_lines)
    return f'{header}\n{body}\n    </quote>'


def _append_archive_xml(xml_path: str,
                        etf_entries: list[dict],
                        fund_entries: list[dict]) -> None:
    """把 entry 列表幂等地 append 到 archive XML 中。

    流程:
      1. 读旧文件得到已有 keys; 过滤掉重复的 entry。
      2. 若文件不存在, 用 _ARCHIVE_HEADER 作为初始骨架。
      3. 对需要写的 entry, 分 ticker 聚合, 用正则定位插入点:
         - 若该 ticker 的 <quote> 节点已存在 -> 在该节点的 </quote> 之前插入新 <week>/<period>
         - 否则 -> 在 </etf_weekly> 或 </fundamentals> 之前插入一个新 <quote>...</quote> 块
      4. 用临时文件 + os.replace 原子写入。

    注意: 全程不触碰已有节点内容/格式/注释, 用户手动改动完全保留。
    """
    import os
    existing_etf, existing_period = _read_archive_keys(xml_path)
    if (("__PARSE_ERROR__", "") in existing_etf
            or ("__PARSE_ERROR__", "") in existing_period):
        return  # 读失败时放弃写入, 保护已有文件

    # 过滤已存在的 key
    etf_todo  = [e for e in etf_entries  if (e["symbol"], e["asof"]) not in existing_etf]
    fund_todo = [e for e in fund_entries if (e["symbol"], e["asof"]) not in existing_period]
    if not etf_todo and not fund_todo:
        print("> archive: no new entries (up to date)", file=sys.stderr)
        return

    # 读文件内容 (或用初始骨架)
    # 空文件 / 全空白 的情况也走骨架路径, 与 _read_archive_keys 的处理保持一致
    txt = ""
    if os.path.exists(xml_path):
        with open(xml_path, "r", encoding="utf-8") as f:
            txt = f.read()
    if not txt.strip():
        txt = _ARCHIVE_HEADER

    # ---------- ETF 分节 ----------
    # 按 symbol 聚合待写的 week
    etf_by_sym: dict[str, list[dict]] = {}
    for e in etf_todo:
        etf_by_sym.setdefault(e["symbol"], []).append(e)
    for sym, entries in etf_by_sym.items():
        week_lines = [_format_week_line(e) for e in entries]
        # 定位 <quote symbol="SYM" ...>...</quote> 块
        pat = re.compile(
            rf'(<quote\s+symbol="{re.escape(sym)}"[^>]*>)(.*?)(</quote>)',
            re.DOTALL,
        )
        m = pat.search(txt)
        if m:
            # 已有 quote 块: 在 </quote> 之前插入新 week 行.
            # group(2) 末尾通常是 "...\n    " (前导缩进指向 </quote>), rstrip 掉再
            # 用统一的 "\n" + 6空格 + line + "\n    " 格式, 保证缩进整齐。
            inner = m.group(2).rstrip()
            new_block = (m.group(1) + inner + "\n"
                         + "\n".join(week_lines) + "\n    " + m.group(3))
            txt = txt[:m.start()] + new_block + txt[m.end():]
        else:
            # 没有该 ticker 的 quote 块: 在 </etf_weekly> 之前插入
            block = _format_etf_quote_block(sym, entries[0]["market"],
                                             entries[0]["currency"], week_lines)
            txt = re.sub(r'(\s*)</etf_weekly>',
                         r'\n' + block + r'\1</etf_weekly>', txt, count=1)

    # ---------- Fundamentals 分节 ----------
    fund_by_sym: dict[str, list[dict]] = {}
    for e in fund_todo:
        fund_by_sym.setdefault(e["symbol"], []).append(e)
    for sym, entries in fund_by_sym.items():
        period_lines = [_format_period_line(e) for e in entries]
        pat = re.compile(
            rf'(<quote\s+symbol="{re.escape(sym)}"[^>]*>)(.*?)(</quote>)',
            re.DOTALL,
        )
        # ⚠ fundamentals 和 etf_weekly 都有 <quote>, 要先定位 <fundamentals>...</fundamentals> 片段
        fm = re.search(r'<fundamentals>(.*?)</fundamentals>', txt, re.DOTALL)
        if not fm:
            print(f"  !! archive: <fundamentals> section missing, skip {sym}",
                  file=sys.stderr)
            continue
        fund_section = fm.group(1)
        m = pat.search(fund_section)
        if m:
            inner = m.group(2).rstrip()
            new_block = (m.group(1) + inner + "\n"
                         + "\n".join(period_lines) + "\n    " + m.group(3))
            new_fund_section = fund_section[:m.start()] + new_block + fund_section[m.end():]
        else:
            block = _format_fund_quote_block(
                sym, entries[0]["market"], entries[0]["currency"],
                entries[0]["report_currency"] or entries[0]["currency"],
                period_lines,
            )
            # 在 </fundamentals> 的内侧末尾插入
            new_fund_section = fund_section.rstrip() + "\n" + block + "\n  "
        txt = txt[:fm.start()] + "<fundamentals>" + new_fund_section + "</fundamentals>" + txt[fm.end():]

    # ---------- 原子写入 ----------
    tmp_path = xml_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    os.replace(tmp_path, xml_path)
    print(f"> archive: +{len(etf_todo)} ETF week(s), +{len(fund_todo)} period(s) "
          f"-> {xml_path}", file=sys.stderr)


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

    # ----- 更新 ValuationSnapshot.xml (20 年归档, append-only) -----
    # 核心约束: 已有节点一字不改 (保留用户手动补的 N/A 替换); 幂等 key = (symbol, asof)。
    # 失败不阻塞主流程, 下周脚本运行时会自动尝试补齐。
    try:
        run_date = now.strftime("%Y-%m-%d")
        xml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 ARCHIVE_XML_FILENAME)
        etf_entries = _build_etf_week_entries(etf_rows, run_date)
        fund_entries: list[dict] = []
        print("\n> Fetching archive fundamentals (quarterly income + balance-sheet)...",
              file=sys.stderr)
        for r in us_rows:
            fund_entries.extend(
                _build_fundamentals_entries(r.symbol, "US", "USD", r, run_date)
            )
        for r in hk_rows:
            fund_entries.extend(
                _build_fundamentals_entries(r.symbol, "HK", "HKD", r, run_date)
            )
        _append_archive_xml(xml_path, etf_entries, fund_entries)
    except Exception as e:
        print(f"  !! archive update failed: {e}", file=sys.stderr)

    # 尝试自动用默认浏览器打开（失败也不影响脚本成功退出）
    try:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(out_path))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
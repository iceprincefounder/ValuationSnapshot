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
    # ETFs
    "VUG":   "vanguard.com",
    "QQQM":  "invesco.com",
    "SPYM":  "ssga.com",
}


# ---------- 页面字段映射 ----------

# stockanalysis.com 页面里的字段 id -> 我们要的列
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


def fetch_invesco_forward_pe(cusip: str) -> float | None:
    """通过 Invesco dng-api 拿 fundCharacteristics.forwardPriceToEarningsRatio。

    URL 模式:
      https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}
        ?expand=nav&idType=cusip&variationType=fundCharacteristics&productType=ETF

    返回加权 harmonic forward P/E, effectiveDate 通常滞后 1 个月末点。
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
    v = j.get("forwardPriceToEarningsRatio")
    if not isinstance(v, (int, float)):
        return None
    if v <= 0 or v > 500:
        return None
    return float(v)


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
    if pe_raw is not None:
        row[FIELDS["pe"]] = pe_raw
    if aum_raw is not None:
        # aum 形如 "$229.61B" / "$97.11B" -> 去掉 $ 前缀, 与股票列 "3.90T" 风格保持一致;
        # _fmt_number 只处理裸数字, fullmatch 失败会原样返回, 因此这里保留 "229.61B"
        # 字符串, 前端表格会原样显示 "229.61B"。
        row[FIELDS["marketcap"]] = aum_raw.lstrip("$").strip() or aum_raw

    # -------- 发行商官方 Forward P/E (ETF 组合加权前瞻 P/E) --------
    # 来自 ETF_FWD_PE_SOURCE 配置: SPYM 走 SSGA HTML, QQQM 走 Invesco dng-api,
    # VUG 无源保持 None。Row.pe_forward_source 记录数据源标签, 前端在
    # "P/E (Fwd)" 单元格的 title tooltip 里明确出处。
    fwd_pe_source_label: str | None = None
    fwd_src = ETF_FWD_PE_SOURCE.get(ticker.upper())
    if fwd_src is not None:
        fwd_label, fwd_arg, fwd_parser = fwd_src
        fwd_val: float | None = None
        if fwd_parser == "ssga_html":
            fwd_val = fetch_ssga_forward_pe(fwd_arg)
        elif fwd_parser == "invesco_api":
            fwd_val = fetch_invesco_forward_pe(fwd_arg)
        if fwd_val is not None:
            # 与其他 P/E 列的字符串格式保持一致 (2 位小数, 供 _parse_num 再解析)
            row[FIELDS["peForward"]] = f"{fwd_val:.2f}"
            fwd_pe_source_label = fwd_label
        else:
            print(f"  .. {ticker}: forward P/E unavailable from {fwd_label}", file=sys.stderr)

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
            return r
        else:
            print(f"  .. {ticker}: index PE history unavailable ({url})", file=sys.stderr)

    tail = Row(ticker, row)
    tail.pe_forward_source = fwd_pe_source_label
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

    if col == CLOSE_COL:
        # 收盘价：粗体单独展示
        return f'<td class="num close">{html.escape(v)}</td>'

    if col == CLOSE_DATE_COL:
        return f'<td class="asof">{html.escape(v)}</td>'

    return f'<td>{html.escape(v)}</td>'


def render_section_html(title: str, rows: list[Row], currency: str) -> str:
    cols = ["Ticker", CLOSE_COL, CLOSE_DATE_COL] + list(FIELDS.values())
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
  }}
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
  /* 单元格右上角小圆点: 提示"悬停可见数据源" (给 ETF 的 P/E Fwd 用) */
  td.num.has-src {{ position: relative; cursor: help; }}
  td.num.has-src::after {{
    content: ''; position: absolute; top: 6px; right: 6px;
    width: 5px; height: 5px; border-radius: 50%;
    background: var(--accent); opacity: .55;
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
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: var(--shadow);
    z-index: 20;
    transform: translateX(calc(100% + 40px));
    opacity: 0;
    pointer-events: none;
    transition: transform .28s ease, opacity .2s ease;
    overflow: hidden;
  }}
  .chart-panel.open {{
    transform: translateX(0);
    opacity: 1;
    pointer-events: auto;
  }}
  .chart-head {{
    display:flex; justify-content:space-between; align-items:center;
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, var(--panel-2), transparent);
  }}
  .chart-title {{ font-size: 15px; font-weight: 700; letter-spacing:-.01em; }}
  .chart-sub   {{ font-size: 11.5px; color: var(--muted); margin-top: 2px; }}
  .chart-close {{
    width: 30px; height: 30px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--panel-2); color: var(--muted); cursor: pointer;
    font-size: 20px; line-height: 1; padding: 0;
  }}
  .chart-close:hover {{ color: var(--text); border-color: var(--accent); }}

  .chart-selected {{
    display:flex; flex-wrap:wrap; gap:6px; padding: 10px 18px 0;
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

  /* Range 切换 (1Y / 3Y / 5Y)：与 metric tab 同行，靠右站 */
  .chart-toolbar {{
    display:flex; align-items:center; gap:10px; padding: 10px 18px 0;
  }}
  .chart-toolbar .chart-tabs {{ flex: 1; padding: 0; }}
  .range-group {{
    display:inline-flex; padding: 3px; gap: 2px;
    border: 1px solid var(--border); border-radius: 10px;
    background: var(--panel-2);
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
      </div>
      <div class="range-group" role="tablist" aria-label="time range">
        <button type="button" class="range-btn"        data-range="1y" role="tab" aria-selected="false">1Y</button>
        <button type="button" class="range-btn"        data-range="3y" role="tab" aria-selected="false">3Y</button>
        <button type="button" class="range-btn active" data-range="5y" role="tab" aria-selected="true">5Y</button>
      </div>
    </div>
    <div class="chart-selected" id="chartSelected"></div>
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
    // ticker -> Forward P/E (P/E Fwd 列) 的数据源标签（仅 ETF 有值, 例 SSGA/Invesco）。
    // 页面加载后会给对应 ticker 行的 "P/E (Fwd)" 单元格追加 title tooltip, 悬停可见出处。
    const FWD_SOURCE = {fwd_source_json};
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
      evebit: {{ title: 'EV / EBIT (TTM) History', label: 'EV/EBIT (TTM)' }}
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
      const tabs      = document.querySelectorAll('.chart-tab');
      const rangeBtns = document.querySelectorAll('.range-btn');

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

      function render() {{
        const bucket  = HISTORY_DATA[currentRange] || {{}};
        const dataMap = bucket[currentMetric] || {{}};
        const meta    = METRIC_META[currentMetric];
        const rmeta   = RANGE_META[currentRange];
        titleE.textContent = meta.title;

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
          currentMetric = m;
          tabs.forEach(x => {{
            const on = x.dataset.metric === m;
            x.classList.toggle('active', on);
            x.setAttribute('aria-selected', on ? 'true' : 'false');
          }});
          render();
        }});
      }});
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

      // ---- 给 ETF 的 "P/E (Fwd)" 单元格追加数据源 tooltip ----
      // 先按表头文本定位 "P/E (Fwd)" 列的索引（每个 section 表头可能相同, 但保险起见按表逐个定位）,
      // 再对 tbody 里每一行匹配 data-ticker, 命中 FWD_SOURCE 就写 title 属性 + 加视觉提示 class。
      (function annotateFwdSource() {{
        if (!FWD_SOURCE || Object.keys(FWD_SOURCE).length === 0) return;
        document.querySelectorAll('table').forEach(tbl => {{
          const ths = tbl.querySelectorAll('thead th');
          let fwdIdx = -1;
          ths.forEach((th, i) => {{
            // 表头是 "P/E&nbsp;(Fwd)" -> textContent 变成 "P/E (Fwd)"
            const txt = (th.textContent || '').replace(/\\s+/g, ' ').trim();
            if (txt === 'P/E (Fwd)') fwdIdx = i;
          }});
          if (fwdIdx < 0) return;
          tbl.querySelectorAll('tbody tr[data-ticker]').forEach(tr => {{
            const sym = tr.getAttribute('data-ticker');
            const label = FWD_SOURCE[sym];
            if (!label) return;
            const cell = tr.children[fwdIdx];
            if (!cell) return;
            cell.setAttribute('title', 'Source: ' + label);
            cell.classList.add('has-src');
          }});
        }});
      }})();
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
    pe_5y_json = json.dumps(pe_5y, ensure_ascii=False)
    ev_5y_json = json.dumps(ev_5y, ensure_ascii=False)
    pe_3y_json = json.dumps(pe_3y, ensure_ascii=False)
    ev_3y_json = json.dumps(ev_3y, ensure_ascii=False)
    pe_1y_json = json.dumps(pe_1y, ensure_ascii=False)
    ev_1y_json = json.dumps(ev_1y, ensure_ascii=False)
    pe_source_json = json.dumps(pe_source, ensure_ascii=False)
    fwd_source_json = json.dumps(fwd_source, ensure_ascii=False)
    logo_map_json = json.dumps(LOGO_DOMAIN, ensure_ascii=False)

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
        logo_map_json=logo_map_json,
        snapshot_json=snapshot_json,
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
      4. 把最新那份 HTML 额外复制一份为 ``Pages/latest.html`` （覆盖式），
         同时把 HTML 内的 ``../logos/`` 引用改写为 ``logos/`` ——因为年份子目录
         下的报告用 ``../logos/`` 引用 ``Pages/logos/`` (相对上跳一级)，而
         latest.html 位于 Pages/ 根下需要同级 ``logos/``。
         用途: 供 CI 上的 Playwright 用固定路径截图, 输出 ``Pages/latest.png``,
         再由 README.md 以相对路径 ``Pages/latest.png`` 引用。

    设计取舍：
      - Pages/index.html 若不存在则跳过重写（首次使用者应先手工放好模板）。
      - 历史 report 保留在 Pages/ 里不清理，旧链接依然可达。
      - latest.html 是"当日快照的浅拷贝"，不是重定向页, 因为无头浏览器截图时
        需要真正渲染出内容, meta refresh 页面会截到空壳。
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

    # 5) 复制当日 HTML 为 Pages/latest.html （覆盖式），供 CI 截图使用
    # report_path 位于 Pages/<YYYY>/ValuationSnapshot-YYYYMMDD.html;
    # 目标固定在 Pages/latest.html, 因此需要覆盖同名文件, 并且要把 HTML 内所有对
    # ``../logos/`` 的引用改写为 ``logos/`` —— 因为年份子目录里的报告需要 ``../logos/``
    # 才能跳到 Pages/logos/, 而 latest.html 在 Pages/ 根下, 应该用 ``logos/`` 同级引用。
    latest_path = os.path.join(pages_dir, "latest.html")
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            latest_html = f.read()
        # 只替换紧跟在 src=" / href=" 之后的 ../logos/ 前缀, 避免误伤注释里的文字
        latest_html = latest_html.replace('src="../logos/', 'src="logos/')
        with open(latest_path, "w", encoding="utf-8") as f:
            f.write(latest_html)
    except OSError as e:
        print(f"  !! failed to refresh latest.html: {e}", file=sys.stderr)


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
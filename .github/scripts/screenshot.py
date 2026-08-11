"""用 Playwright headless Chromium 把 ``Pages/latest.html`` 截成 ``Pages/latest.png``。

用途:
    仓库的 README.md 里通过相对路径 ``Pages/latest.png`` 引用这张截图, 让访客
    在 GitHub 首页就能看到“最新一期 ValuationSnapshot 的完整全貌”（表格 +
    P/E, P/FCF, EV/EBIT 五年趋势图）。

设计取舍:
    - 只截 ``Pages/latest.html``（由 ValuationSnapshot.py 每次跑完写入的当日
      快照浅拷贝）。避开截 ``Pages/index.html``：那是 meta refresh 重定向页,
      截出来是空壳卡片。
    - viewport 底线宽度 1600px, 若页面真实内容更宽则动态扩展到最多 3000px;
      高度用 ``full_page=True`` 自动扩展成长图.
      GitHub README 显示区约 1000px, 1600 给出足够的清晰度余量, 且能容下近期
      DCF Tab 拓宽后的主表 + 侧栏; 缩放后仍不失真。
    - 用 ``networkidle`` 等 SVG 图表与 favicon 加载完；若 30s 内没静默, 退回
      到 ``load`` 事件即可, 因为主体内容其实是脚本注入的, DOMContentLoaded 后
      就已经渲染完毕, 剩下的只是各家 favicon CDN 的最后几个请求。
    - CI 上以 ``pip install playwright && playwright install --with-deps chromium``
      的方式装依赖, 本文件不做 auto-install。

失败策略:
    脚本本身 fail-fast (return code != 0): CI 会看到明显的失败标记。若因为网络
    抖动想让它 "软失败", 请在 workflow 步骤上加 ``continue-on-error: true``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    # 默认布局: 仓库根 / .github / scripts / screenshot.py, Pages/ 在仓库根下
    # 所以要往上走两级才回到 repo_root
    here = Path(__file__).resolve().parent           # <repo>/.github/scripts
    repo_root = here.parent.parent                    # <repo>
    src_html = repo_root / "Pages" / "latest.html"
    dst_png = repo_root / "Pages" / "latest.png"

    if not src_html.exists():
        print(f"!! source html not found: {src_html}", file=sys.stderr)
        print("   run `python ValuationSnapshot.py` first to generate it.", file=sys.stderr)
        return 2

    try:
        # 延迟导入, 让“仅仅 --help / 语法检查”不需要装 playwright
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("!! playwright not installed. run `pip install playwright` first.", file=sys.stderr)
        return 3

    # 用 file:// URL 让 Chromium 直接读本地 HTML, 保证图表脚本正常执行
    file_url = src_html.as_uri()
    print(f">> rendering {file_url}", file=sys.stderr)

    # 视口宽度取舍:
    #   - GitHub README 显示区约 1000px, 但截图会被浏览器自动缩放, 所以给宽一点
    #     可以提升清晰度. 之前 1200 在近期新增 DCF Tab (CAGR chips + tooltip 表格 +
    #     header 里 1Y/3Y/5Y 切换徽标) 后, 主表右侧列会被横向裁掉一部分.
    #   - 直接抬到 1600 作为"底线宽度", 再叠加下面的动态量取, 无论页面未来横向
    #     怎么长, 都能完整入镜.
    BASE_WIDTH = 1600
    # 安全上限, 避免异常情况下截出巨图 (例如某个内联 SVG 意外拉宽):
    MAX_WIDTH = 3000

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            # deviceScaleFactor=2 让输出图片在 Retina 屏上更清晰; GitHub 会自动
            # 按 CSS 宽度缩放显示, 视觉密度更好, 代价是文件体积翻倍 (约 500KB-1MB).
            ctx = browser.new_context(
                viewport={"width": BASE_WIDTH, "height": 900},
                device_scale_factor=2,
            )
            page = ctx.new_page()
            page.goto(file_url, wait_until="domcontentloaded", timeout=30_000)
            # 先等 SVG 图表全部注入完 (脚本末尾会 append 到各 section);
            # 再等网络静默确认图片、favicon 都加载完。networkidle 有超时兜底。
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception as e:
                # 有些 favicon CDN 会挂起, 但对页面视觉无影响, 忽略即可
                print(f"   networkidle timeout, proceeding: {e}", file=sys.stderr)

            # 额外给 800ms, 让 SVG 动画与 favicon 三级 fallback 稳定下来
            page.wait_for_timeout(800)

            # 动态量取真实内容宽度, 若超过当前 viewport 则扩展视口再截, 避免
            # 右侧被 clip. 这里同时取 documentElement / body 的多个属性, 因为
            # 某些容器上的 overflow: hidden 会让 scrollWidth 失真, 多路取最大更稳.
            real_width = page.evaluate(
                """() => {
                    const d = document.documentElement, b = document.body;
                    return Math.max(
                      d.scrollWidth,  d.offsetWidth,  d.clientWidth,
                      b ? b.scrollWidth : 0, b ? b.offsetWidth : 0
                    );
                }"""
            )
            try:
                real_width = int(real_width)
            except Exception:
                real_width = BASE_WIDTH

            if real_width > BASE_WIDTH:
                target = min(real_width, MAX_WIDTH)
                print(
                    f">> content width {real_width}px > viewport {BASE_WIDTH}px, "
                    f"expanding viewport to {target}px",
                    file=sys.stderr,
                )
                page.set_viewport_size({"width": target, "height": 900})
                # 视口变化后给 SVG 重排一点时间 (图表宽度大多是 CSS 100%, 靠
                # 容器宽度自适应, 需要一次布局回流)
                page.wait_for_timeout(400)

            page.screenshot(path=str(dst_png), full_page=True)
        finally:
            browser.close()

    size_kb = dst_png.stat().st_size / 1024
    print(f">> wrote {dst_png} ({size_kb:.1f} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

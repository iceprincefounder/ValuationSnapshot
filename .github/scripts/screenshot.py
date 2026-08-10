"""用 Playwright headless Chromium 把 ``Pages/latest.html`` 截成 ``Pages/latest.png``。

用途:
    仓库的 README.md 里通过相对路径 ``Pages/latest.png`` 引用这张截图, 让访客
    在 GitHub 首页就能看到“最新一期 ValuationSnapshot 的完整全貌”（表格 +
    P/E, P/FCF, EV/EBIT 五年趋势图）。

设计取舍:
    - 只截 ``Pages/latest.html``（由 ValuationSnapshot.py 每次跑完写入的当日
      快照浅拷贝）。避开截 ``Pages/index.html``：那是 meta refresh 重定向页,
      截出来是空壳卡片。
    - viewport 宽度固定 1200px, 高度用 ``full_page=True`` 自动扩展成长图。
      GitHub README 显示区域约 1000px, 1200 稍宽保证清晰度, 缩放后仍不失真。
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            # deviceScaleFactor=2 让输出图片在 Retina 屏上更清晰; GitHub 会自动
            # 按 CSS 宽度缩放显示, 视觉密度更好, 代价是文件体积翻倍 (约 500KB-1MB).
            ctx = browser.new_context(
                viewport={"width": 1200, "height": 800},
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

            page.screenshot(path=str(dst_png), full_page=True)
        finally:
            browser.close()

    size_kb = dst_png.stat().st_size / 1024
    print(f">> wrote {dst_png} ({size_kb:.1f} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

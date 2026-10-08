"""Check article images, rich-text copy and mobile layout against a running app."""

import argparse
import asyncio
import json
import shutil

from playwright.async_api import async_playwright


async def check_previews(urls, browser_path=None):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=browser_path)
        try:
            context = await browser.new_context(
                permissions=["clipboard-read", "clipboard-write"],
                viewport={"width": 1000, "height": 850},
            )
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            results = []
            for url in urls:
                errors.clear()
                await page.goto(url)
                await page.wait_for_function(
                    'Array.from(document.querySelectorAll("#article img"))'
                    ".every(image => image.complete && image.naturalWidth > 0)"
                )
                count = await page.locator("#article img").count()
                captions = await page.locator("#article [data-figure-caption]").count()
                await page.click("#copy")
                await page.wait_for_function(
                    'document.getElementById("status").textContent.includes("已复制")'
                )
                html = await page.evaluate(
                    "async () => await (await (await navigator.clipboard.read())[0]"
                    '.getType("text/html")).text()'
                )
                assert html.count('src="data:image/png;base64,') == count, "复制内容缺少图片"
                assert html.count('data-figure-caption="true"') == captions, "复制内容缺少图注样式"
                await page.evaluate("""const editor = document.createElement('div');
                    editor.id = 'paste-check'; editor.contentEditable = 'true';
                    document.body.appendChild(editor); editor.focus();""")
                await page.keyboard.press("Control+V")
                await page.wait_for_function(
                    '(count) => document.querySelectorAll("#paste-check img").length === count '
                    '&& Array.from(document.querySelectorAll("#paste-check img"))'
                    ".every(image => image.complete && image.naturalWidth > 0)",
                    arg=count,
                )
                await page.locator("#paste-check").evaluate("(node) => node.remove()")
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    "手机页面横向溢出"
                )
                assert not errors, errors
                await page.set_viewport_size({"width": 1000, "height": 850})
                results.append(
                    {"url": url, "images": count, "captions": captions, "status": "passed"}
                )
            return results
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", nargs="+", required=True, help="Article preview URL(s)")
    parser.add_argument(
        "--browser-path", default=shutil.which("google-chrome"), help="Optional Chromium executable"
    )
    args = parser.parse_args()
    results = asyncio.run(check_previews(args.url, args.browser_path))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

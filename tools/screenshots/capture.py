"""Capture the README / timar.tools screenshots from `demo.py` on :8099, light and dark.

1280 px at 2x; the browser clock is fixed so relative times read "2 hours ago", "in 18 hours".
After a capture, check the image sizes against the `width`/`height` attributes in docs/index.html.
"""
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright

OUT = sys.argv[1]
BASE = "http://127.0.0.1:8099"
NOW = datetime.fromisoformat("2026-10-24T09:35:00+03:00")


def page_for(browser, scheme, height=800):
    ctx = browser.new_context(viewport={"width": 1280, "height": height}, device_scale_factor=2,
                              color_scheme=scheme, timezone_id="Europe/Istanbul", locale="en-US")
    page = ctx.new_page()
    page.clock.set_fixed_time(NOW)
    return ctx, page


def login(page):
    page.goto(BASE + "/setup")
    if "/setup" in page.url:
        page.fill("input[name=username]", "admin")
        page.fill("input[name=password]", "demo-password-1234")
        page.click("button[type=submit]")
    elif "/login" in page.url:
        page.fill("input[name=username]", "admin")
        page.fill("input[name=password]", "demo-password-1234")
        page.click("button[type=submit]")


def settle(page, ms=600):
        page.wait_for_timeout(ms)


def dialog(page, name):
    for _ in range(50):
        page.wait_for_timeout(100)
        if page.locator("dialog[open] section.panel").count():
            break
    settle(page)
    page.locator("dialog[open] section.panel").screenshot(path=f"{OUT}/{name}")


with sync_playwright() as p:
    browser = p.chromium.launch()
    for scheme in ("dark", "light"):
        ctx, page = page_for(browser, scheme)
        login(page)

        page.goto(BASE + "/")
        for _ in range(50):
            settle(page, 100)
            if page.locator('a[href="/settings/containers/photos/edit"]').count():
                break
        settle(page)
        page.screenshot(path=f"{OUT}/dashboard-{scheme}.png", full_page=True)

        # thead and the nas-01 rows are not adjacent: hide the rows in between for the crop.
        page.evaluate("""() => {
            const rows = [...document.querySelectorAll('tbody tr')];
            const from = rows.findIndex(r => r.querySelector('a[href="/settings/servers/nas-01/edit"]'));
            rows.slice(0, from).forEach(r => r.style.display = 'none');
            const to = rows.findIndex(r => r.querySelector('a[href="/settings/containers/vault/edit"]'));
            rows.slice(to + 1).forEach(r => r.style.display = 'none');
            document.querySelector('.chip-bar').style.display = 'none';
            document.querySelector('[hx-trigger]').removeAttribute('hx-trigger');
        }""")
        panel = page.locator(".panel").first.bounding_box()
        page.screenshot(path=f"{OUT}/containers-{scheme}.png", clip={
            "x": panel["x"] - 24, "y": panel["y"] - 24, "width": panel["width"] + 48,
            "height": panel["height"] + 48})

        ctx.close()
        ctx, page = page_for(browser, scheme, height=1100)
        login(page)
        page.goto(BASE + "/")
        page.wait_for_selector('a[href="/settings/servers/vm-01/edit"]')
        page.click('a[href="/settings/servers/vm-01/edit"]')
        dialog(page, f"server-dialog-{scheme}.png")
        ctx.close()

        ctx, page = page_for(browser, scheme)
        login(page)
        page.goto(BASE + "/reports")
        settle(page)
        page.screenshot(path=f"{OUT}/reports-{scheme}.png", full_page=True)
        ctx.close()

        for name, selector in (("report-sweep", 0), ("report-update", 3)):
            ctx, page = page_for(browser, scheme, height=1200)
            login(page)
            page.goto(BASE + "/reports")
            settle(page)
            page.locator("#report-list a.icon-btn").nth(selector).click()
            dialog(page, f"{name}-{scheme}.png")
            ctx.close()

        ctx, page = page_for(browser, scheme, height=1200)
        login(page)
        page.goto(BASE + "/reports")
        settle(page)
        page.click('a[href="/settings/jobs/update/edit"]')
        dialog(page, f"job-dialog-{scheme}.png")
        ctx.close()
    browser.close()

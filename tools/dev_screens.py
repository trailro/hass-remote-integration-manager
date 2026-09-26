"""Screenshots of the manager's page, with Playwright, after tools/dev_driver.py has run.

    python tools/dev_screens.py <manager URL> <output folder>

Run by tools/dev_smoke.sh in a Playwright container whose address is one of the manager's development peers.  The
page is loaded as Home Assistant's ingress would serve it, with a Home Assistant user name header."""

import sys

from playwright.sync_api import sync_playwright

URL, OUT = sys.argv[1].rstrip("/") + "/", sys.argv[2].rstrip("/")


def main() -> int:
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, extra_http_headers={"X-Remote-User-Name": "alice"})
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(URL)
        page.wait_for_selector("#inst tbody tr")
        # selectors only: the page's policy (script-src 'self') refuses the evaluated strings wait_for_function sends
        page.locator("#c-version option").nth(1).wait_for(state="attached")
        page.screenshot(path=f"{OUT}/01-instances.png", full_page=True)

        page.click("#inst button[data-act=update][data-name=attic]")
        page.wait_for_selector("#confirm[open]")
        page.screenshot(path=f"{OUT}/02-update-dialog.png")
        page.click("#confirm button[value=cancel]")

        page.click("#inst button[data-act=delete][data-name=attic]")
        page.wait_for_selector("#confirm[open]")
        page.check("#cf-data")
        page.fill("#cf-name", "atti")
        page.screenshot(path=f"{OUT}/03-delete-dialog.png")
        page.click("#confirm button[value=cancel]")

        page.fill("#c-name", "porch")
        page.click("#c-go")
        page.wait_for_selector("#jobcard:not([hidden])")
        page.wait_for_timeout(1500)
        page.screenshot(path=f"{OUT}/04-job-running.png", full_page=True)
        page.wait_for_selector("#jobtitle.ok", timeout=60000)
        page.wait_for_timeout(500)
        page.screenshot(path=f"{OUT}/05-job-done.png", full_page=True)

        page.select_option("#c-channel", "git")
        page.fill("#c-ref", "main")
        page.screenshot(path=f"{OUT}/06-git-form.png", clip={"x": 0, "y": 0, "width": 1280, "height": 900})

        mobile = browser.new_context(viewport={"width": 390, "height": 844}, extra_http_headers={"X-Remote-User-Name": "alice"})
        mp = mobile.new_page()
        mp.goto(URL)
        mp.wait_for_selector("#inst tbody tr")
        mp.screenshot(path=f"{OUT}/07-mobile.png", full_page=True)
        width = mp.locator("html").evaluate("e => e.scrollWidth")
        browser.close()
    print(f"screenshots in {OUT}; page scroll width on a phone: {width}px")
    if errors:
        print("console errors:", errors)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

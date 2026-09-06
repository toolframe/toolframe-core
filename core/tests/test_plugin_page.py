"""Browser acceptance helper used by the opt-in packaged executable test.

Set TOOLFRAME_TEST_BROWSER=1 and install playwright in the test environment.
Uses installed Chrome unless TOOLFRAME_TEST_BROWSER_EXECUTABLE names another
Chromium executable. No mocks: all page actions call the running Toolframe API.
"""

import os
from pathlib import Path


async def exercise_plugin_page(url, directory):
    from playwright.async_api import async_playwright, expect

    artifacts = Path(os.environ.get("TOOLFRAME_TEST_BROWSER_ARTIFACTS", directory))
    artifacts.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = os.environ.get("TOOLFRAME_TEST_BROWSER_EXECUTABLE")
        options.update(
            {"executable_path": executable} if executable else {"channel": "chrome"}
        )
        browser = await playwright.chromium.launch(**options)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        formatter = page.locator("#plugin-list article").filter(
            has=page.get_by_role("heading", name="formatter", exact=True)
        )
        domain = page.locator("#plugin-list article").filter(
            has=page.get_by_role("heading", name="domain.probe", exact=True)
        )

        async def configure(config, *, noop=False):
            await formatter.get_by_text("Configure", exact=True).click()
            await formatter.get_by_role("textbox").fill(config)
            await formatter.get_by_role(
                "button", name="Preview changes", exact=True
            ).click()
            await expect(formatter.locator(".plugin-plan")).to_contain_text(
                "No change" if noop else "Settings change."
            )
            await expect(formatter.locator(".plugin-plan")).to_contain_text(
                "Can apply."
            )
            await formatter.get_by_role(
                "button", name="Apply configuration", exact=True
            ).click()
            await expect(page.locator("#refresh-button")).to_be_enabled()
            await expect(formatter.get_by_role("textbox")).not_to_be_visible()

        try:
            await page.goto(url + "/info")
            await expect(formatter).to_be_visible()
            await expect(formatter).to_contain_text(
                "Provides: format.text, format.label"
            )
            await formatter.get_by_role(
                "button", name="Stop formatter", exact=True
            ).click()
            await expect(domain).to_contain_text("blocked ·")
            await expect(domain).to_contain_text("Waiting for: formatter")
            await expect(
                domain.get_by_role("button", name="Enable domain.probe", exact=True)
            ).to_have_count(0)
            await formatter.get_by_role(
                "button", name="Enable formatter", exact=True
            ).click()
            await expect(domain).to_contain_text("active ·")
            await configure('{"prefix":"Hello"}', noop=True)
            await configure('{"prefix":"Updated"}')
            await configure('{"fail":true}')
            await expect(formatter).to_contain_text("failed ·")
            await expect(
                formatter.get_by_role("button", name="Retry formatter", exact=True)
            ).to_be_visible()
            await expect(
                formatter.get_by_role("button", name="Enable formatter", exact=True)
            ).to_have_count(0)
            await configure('{"prefix":"Corrected"}')
            await expect(formatter).to_contain_text("failed ·")
            await formatter.get_by_role(
                "button", name="Retry formatter", exact=True
            ).click()
            await expect(domain).to_contain_text("active ·")
            await formatter.get_by_text("Lifecycle details", exact=True).click()
            await expect(formatter).to_contain_text("Generation:")
            await expect(formatter).to_contain_text("Owned tasks: 0 · Task failures: 0")
            assert (
                "never-expose-cordis-config"
                not in await page.locator("body").inner_text()
            )
            host = page.locator("#plugin-list article").filter(
                has=page.get_by_role(
                    "heading", name="toolframe.database.sqlite", exact=True
                )
            )
            await expect(host.get_by_role("button")).to_have_count(0)
            await page.locator("#plugins-title").scroll_into_view_if_needed()
            await page.screenshot(
                path=str(artifacts / "plugins-desktop.png"), full_page=True
            )
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(
                path=str(artifacts / "plugins-mobile.png"), full_page=True
            )
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            assert errors == []
        except BaseException:
            await page.screenshot(
                path=str(artifacts / "plugins-failure.png"), full_page=True
            )
            raise
        finally:
            await browser.close()

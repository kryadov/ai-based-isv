"""Browser e2e of the organization panel (M11): a real Chromium against a live panel.

Run through infra/tests/e2e/run.sh (Playwright image). Env: PANEL_URL,
PANEL_USER, PANEL_PASSWORD, PANEL_CARD_ID (a pending HITL card this test decides).
"""

from __future__ import annotations

import os
import re

from playwright.sync_api import Page, expect

URL = os.environ["PANEL_URL"].rstrip("/")
TABS = ["inbox", "projects", "swarm", "pipeline", "automations", "metrics", "memory", "settings"]


def test_panel_end_to_end(page: Page) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console: {msg.text}") if msg.type == "error" else None,
    )

    # 1) login: a wrong password is refused, the right one opens the shell
    page.goto(f"{URL}/ui")
    page.fill("input[name=username]", os.environ["PANEL_USER"])
    page.fill("input[name=password]", "definitely-wrong")
    page.click("button[type=submit]")
    expect(page.locator("body")).to_contain_text("Invalid username or password")
    errors.clear()  # the 401 of the refused login is logged by the browser; expected
    page.goto(f"{URL}/ui")
    page.fill("input[name=username]", os.environ["PANEL_USER"])
    page.fill("input[name=password]", os.environ["PANEL_PASSWORD"])
    page.click("button[type=submit]")
    expect(page.locator('[data-tab="inbox"]')).to_be_visible()

    # 2) every tab renders without a JS error
    for tab in TABS:
        page.click(f'[data-tab="{tab}"]')
        expect(page.locator(f'[data-tab="{tab}"]')).to_have_class(re.compile("active"))
        page.wait_for_load_state("networkidle")
        expect(page.locator("#view")).not_to_be_empty()
    assert not errors, errors

    # 3) RU switch translates the shell
    page.select_option("#lang", "ru")
    expect(page.locator('[data-tab="inbox"]')).to_have_text("Входящие")
    page.select_option("#lang", "en")
    expect(page.locator('[data-tab="inbox"]')).to_have_text("Inbox")

    # 4) decide the prepared card from the Inbox
    card_id = os.environ["PANEL_CARD_ID"]
    page.click('[data-tab="inbox"]')
    card = page.locator(".card", has_text=f"· #{card_id}")
    expect(card).to_be_visible()
    card.locator("button.approve").click()
    expect(page.locator(".card", has_text=f"· #{card_id}")).to_have_count(0)
    assert not errors, errors

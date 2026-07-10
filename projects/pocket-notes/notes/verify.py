#!/usr/bin/env python3
"""M1+M2 acceptance check for pocket-notes: layout renders, CRUD works,
notes survive a reload. Saves a screenshot as evidence."""
from pathlib import Path
from playwright.sync_api import sync_playwright

APP = Path("/home/claude/foundry/projects/pocket-notes/src/index.html").as_uri()
SHOT = "/home/claude/foundry/projects/pocket-notes/notes/verify-m1-m2.png"

errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1100, "height": 700})
    page = ctx.new_page()
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    # M1: layout
    page.goto(APP)
    assert page.locator("#note-list").is_visible(), "sidebar list missing"
    assert page.locator("#new-note").is_visible(), "New button missing"
    assert page.locator("#empty").is_visible(), "empty state missing"

    # M2: create + edit
    page.click("#new-note")
    page.fill("#title", "Groceries")
    page.fill("#body", "eggs\nespresso beans\nhot sauce")
    page.click("#new-note")
    page.fill("#title", "Foundry test note")
    page.fill("#body", "written by the verification script")
    assert page.locator(".note-item").count() == 2, "expected 2 notes in list"

    # M2: persistence across reload
    page.reload()
    assert page.locator(".note-item").count() == 2, "notes lost on reload!"
    texts = page.locator(".note-item .t").all_text_contents()
    assert "Groceries" in texts and "Foundry test note" in texts, f"titles wrong: {texts}"

    # M2: delete
    page.locator(".note-item", has_text="Foundry test note").click()
    page.click("#delete-note")
    assert page.locator(".note-item").count() == 1, "delete failed"
    page.reload()
    assert page.locator(".note-item").count() == 1, "delete didn't persist"

    # Evidence screenshot: one surviving note, editor open
    page.locator(".note-item").first.click()
    page.screenshot(path=SHOT)
    browser.close()

assert not errors, f"console errors: {errors}"
print("PASS — M1 layout ✓  M2 create/edit/persist/delete ✓  no console errors ✓")
print(f"evidence: {SHOT}")

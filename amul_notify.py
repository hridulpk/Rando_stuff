"""
Amul stock -> ntfy phone notification.

Watches one product on shop.amul.com for one delivery pincode and sends a
push notification (via the ntfy app) when it flips from out of stock to in stock.

Env vars:
  NTFY_TOPIC    your secret topic name (subscribe to it in the ntfy app)
  PINCODE       6-digit delivery pincode
Optional:
  FORCE_NOTIFY=1   send an alert whenever it is in stock (testing)

Usage:
  python amul_notify.py          # normal check
  python amul_notify.py --test   # just send a test notification
"""
import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

PINCODE = os.environ.get("PINCODE", "").strip()
ALIAS = "amul-chocolate-whey-protein-34-g-or-pack-of-60-sachets"
PRODUCT_URL = f"https://shop.amul.com/en/product/{ALIAS}"
PRODUCT_NAME = "Amul Chocolate Whey Protein, 34 g | Pack of 60 sachets"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = Path("state.json")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def notify(title: str, text: str, click_url: str | None = None, priority: str = "high") -> None:
    headers = {"Title": title, "Priority": priority, "Tags": "shopping_cart"}
    if click_url:
        headers["Click"] = click_url  # tapping the notification opens the product
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=text.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()


def set_pincode(page, pincode: str) -> None:
    """Fill the delivery pincode box. Amul binds your regional store to it."""
    box = page.locator(
        "input[placeholder*='incode' i], input[name*='pincode' i], input#search"
    ).first
    if not box.is_visible():
        # Open the pincode popup from the header
        for label in ("Select Pincodes", "Select Delivery Pincode", "Pincode"):
            link = page.get_by_text(label, exact=False).first
            if link.count() and link.is_visible():
                link.click()
                break
    box.wait_for(state="visible", timeout=15000)
    box.fill(pincode)
    page.wait_for_timeout(1500)
    # Pick the first suggestion if one appears, otherwise press Enter
    suggestion = page.locator(f"a:has-text('{pincode}'), li:has-text('{pincode}')").first
    if suggestion.count() and suggestion.is_visible():
        suggestion.click()
    else:
        box.press("Enter")
    page.wait_for_load_state("networkidle", timeout=30000)


def check_stock() -> bool | None:
    """True = in stock, False = sold out, None = could not tell."""
    api_hits = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.on("response", lambda r: api_hits.append(r) if "ms.products" in r.url else None)

        page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        try:
            set_pincode(page, PINCODE)
        except Exception as e:  # selectors can change; keep going and report
            print(f"[warn] pincode step failed: {e}")
            page.screenshot(path="debug_pincode.png")

        api_hits.clear()
        page.reload(wait_until="networkidle", timeout=60000)

        # 1) Preferred: the site's own product API response
        result = None
        for resp in api_hits:
            try:
                data = resp.json().get("data", [])
            except Exception:
                continue
            for item in data if isinstance(data, list) else []:
                if item.get("alias") == ALIAS and "available" in item:
                    result = bool(int(item["available"]))
        if result is not None:
            browser.close()
            return result

        # 2) Fallback: read the button on the page
        body = page.inner_text("body")
        if page.get_by_role("button", name=re.compile("add to cart", re.I)).count():
            result = True
        elif re.search(r"sold out", body, re.I):
            result = False
        else:
            page.screenshot(path="debug_page.png")
        browser.close()
        return result


def main() -> int:
    if not NTFY_TOPIC:
        print("Set NTFY_TOPIC (the topic you subscribed to in the ntfy app).")
        return 2

    if "--test" in sys.argv:
        notify("Amul notifier test", "ntfy is wired up correctly.", priority="default")
        print("Test notification sent.")
        return 0

    if not re.fullmatch(r"\d{6}", PINCODE):
        print(f"PINCODE '{PINCODE}' is not a valid 6-digit Indian pincode. "
              "Set the PINCODE env var and try again.")
        return 2

    in_stock = check_stock()
    print(f"in_stock={in_stock}")
    if in_stock is None:
        print("Could not determine stock; leaving previous state untouched.")
        return 1

    prev = None
    if STATE_FILE.exists():
        prev = json.loads(STATE_FILE.read_text()).get("in_stock")

    if in_stock and (prev is not True or os.environ.get("FORCE_NOTIFY")):
        notify("Amul is IN STOCK", f"{PRODUCT_NAME}\nPincode {PINCODE}", click_url=PRODUCT_URL,
               priority="urgent")
    elif prev is True and not in_stock:
        notify("Amul sold out again", PRODUCT_NAME, priority="default")

    STATE_FILE.write_text(json.dumps({"in_stock": in_stock}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

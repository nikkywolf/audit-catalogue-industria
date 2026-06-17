from playwright.sync_api import sync_playwright

with sync_playwright() as p:

    browser = p.chromium.launch_persistent_context(
        user_data_dir="lightspeed_session",
        headless=False
    )

    page = browser.new_page()

    page.goto("https://us.merchantos.com/?form_name=ui_tab&tab=inventory")

    print("")
    print("Connecte-toi à Lightspeed.")
    print("Quand tu es rendu dans le backoffice, appuie sur ENTER.")
    print("")

    input()

    browser.close()

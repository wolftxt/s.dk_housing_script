import asyncio
import getpass
import os
import re

from playwright.async_api import async_playwright

BASE_URL = "https://mit.s.dk"
LOGIN_URL = "https://mit.s.dk/studiebolig/home/"
STORAGE_STATE_PATH = "state.json"

async def login_and_save_state(page):
    """Handles terminal input login and saves authentication cookies."""
    username = input("Enter username: ")
    password = getpass.getpass("Enter password: ")

    await page.goto(LOGIN_URL, wait_until="networkidle")

    # Fill login form
    await page.wait_for_selector('input[type="password"]')
    await page.fill('input[type="text"], input[type="email"]', username)
    await page.fill('input[type="password"]', password)
    await page.click('button[type="submit"], input[type="submit"]')

    # Submit login
    await page.wait_for_load_state("networkidle")

    # Save authentication state
    await page.context.storage_state(path=STORAGE_STATE_PATH)
    print("Login successful. Session saved to 'state.json'.")


async def load_all_buildings(page):
    """Selects CIU tab and continuously clicks 'Vis flere ejendomme' until all dorms are rendered."""
    await page.goto(LOGIN_URL, wait_until="networkidle")

    # Step 1: Click the CIU tab element to activate dorm listings
    ciu_selector = 'span:has-text("CIU - Centralindstillingsudvalget")'
    try:
        print("Selecting CIU tab...")
        ciu_element = page.locator(ciu_selector).first
        await ciu_element.wait_for(state="visible", timeout=10000)

        # Click CIU tab (using evaluate click to bypass potential span overlay issues)
        await ciu_element.evaluate("el => el.click()")

    except Exception as e:
        print(f"Could not click CIU tab: {e}")

    # Step 2: Continuously click 'Vis flere ejendomme'
    click_count = 0
    button_locator = page.get_by_role("button", name="Vis flere ejendomme")
    await button_locator.wait_for(state="visible", timeout=10000)

    while True:
        if await button_locator.is_hidden():
            break
        await button_locator.scroll_into_view_if_needed()

        # Trigger click directly in DOM
        await button_locator.evaluate("el => el.click()")
        click_count += 1
        print(f"Clicked 'Vis flere ejendomme' ({click_count})...")

        loader = page.get_by_text("Opdaterer")
        await loader.wait_for(state="hidden", timeout=10000)

    print(f"Finished loading all dorms. Total extra pages loaded: {click_count}")

async def extract_dorm_links(page):
    """Collects all matching dorm URLs from the page."""
    hrefs = await page.eval_on_selector_all(
        'a[href^="/studiebolig/building/"]',
        'elements => elements.map(e => e.getAttribute("href"))'
    )
    # Deduplicate while preserving order
    unique_hrefs = list(dict.fromkeys(hrefs))
    return [f"{BASE_URL}{href}" for href in unique_hrefs]

async def scrape_dorm_details(context, url):
    """Visits a dorm page to extract its name and best queue position."""
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded")

        # Extract dorm name
        name_elem = page.locator("h1, h2.building-name").first
        name = await name_elem.inner_text() if await name_elem.count() > 0 else "Unknown Dorm"
        name = name.strip()

        # Extract all waiting list category positions on the page
        queue_elems = page.locator(".waiting-list-category")
        count = await queue_elems.count()

        if count > 0:
            raw_texts = await queue_elems.all_inner_texts()
            cleaned_positions = []
            pattern = r'([A-G])\xa0info_outline'
            for raw_text in raw_texts:
                match = re.search(pattern, raw_text)
                if match:
                    letter = match.group(1)
                    cleaned_positions.append(letter)

            if cleaned_positions:
                # Pick the best category position (A > B > ... > G)
                best_position = min(cleaned_positions)
                queue_pos = best_position
            else:
                queue_pos = "N/A"
        else:
            queue_pos = "N/A"

        return {"name": name, "queue": queue_pos}
    except Exception as e:
        return {"name": url, "queue": f"Error: {e}"}
    finally:
        await page.close()

async def scrape_with_semaphore(semaphore, context, url):
    """Wraps scrape_dorm_details with a concurrency limiter."""
    async with semaphore:
        return await scrape_dorm_details(context, url)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # Reuse state if available, else prompt login
        if os.path.exists(STORAGE_STATE_PATH):
            print("Loading existing session state...")
            context = await browser.new_context(storage_state=STORAGE_STATE_PATH)
        else:
            print("No saved session found. Initializing login...")
            context = await browser.new_context()
            temp_page = await context.new_page()
            await login_and_save_state(temp_page)
            await temp_page.close()

        main_page = await context.new_page()

        # Step 1: Load page and continuously click "Vis flere ejendomme"
        print("Loading dorm listings...")
        await load_all_buildings(main_page)

        # Step 2: Extract building links
        dorm_urls = await extract_dorm_links(main_page)
        print(f"Found {len(dorm_urls)} dorm links.")

        # Step 3: Visit each dorm page concurrently
        print("Scraping individual dorm pages...")
        CONCURRENT_LIMIT = 8
        semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)

        tasks = [
            scrape_with_semaphore(semaphore, context, url)
            for url in dorm_urls
        ]
        results = await asyncio.gather(*tasks)

        await browser.close()

        # Step 4: Sort results by queue position rank
        sorted_results = sorted(results, key=lambda x: x["queue"])

        # Step 5: Output results
        # Positions according to https://www.s.dk/raad-og-vejledning/#toggle-id-10
        map_to_position = {
            "A": "1-10",
            "B": "11-40",
            "C": "41-100",
            "D": "101-200",
            "E": "201-400",
            "F": "401-1000",
            "G": "1001-"
        }

        for dorm in sorted_results:
            print(f"{dorm['queue']} ({map_to_position[dorm['queue']]}) | {dorm['name']}")

if __name__ == "__main__":
    asyncio.run(main())
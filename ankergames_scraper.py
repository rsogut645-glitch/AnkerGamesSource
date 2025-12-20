#!/usr/bin/env python3
import asyncio
import re
import datetime
import ujson
import sys
from playwright.async_api import async_playwright

# --- Configuration ---
BASE_URL = "https://ankergames.net/games-list"
OUTPUT_FILE = "ankergames.json"
CONCURRENCY_LIMIT = 5
HEADLESS_MODE = True

class AnkerScraper:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    async def clean_title(self, raw_title):
        """Refines the title to improve matching in Hydra."""
        clean = re.sub(r'\s*\(.*?\)', '', raw_title)
        clean = re.sub(r'\sv\d+(\.\d+)*', '', clean)
        return clean.strip()

    async def parse_date(self, date_text):
        """Parses vague date strings into YYYY-MM-DD."""
        try:
            clean_date = date_text.lower().replace("last updated:", "").strip()
            dt = datetime.datetime.strptime(clean_date, "%b %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return datetime.datetime.now().strftime("%Y-%m-%d")

    async def discover_links(self, browser):
        print("Starting Discovery Phase...")
        page = await browser.new_page()
        game_links = set()
        
        try:
            await page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
            
            while True:
                # Extract links
                links = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('article a, .post a, .blog-post a')).map(a => a.href)
                """)
                new_links = {l for l in links if "/game/" in l or "/download/" in l}
                
                if not new_links:
                    # Fallback for generic structure
                    links = await page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href)")
                    new_links = {l for l in links if len(l) > len(BASE_URL) + 10}

                game_links.update(new_links)
                print(f"Found {len(new_links)} games on this page. Total unique: {len(game_links)}")

                # Pagination
                next_btn = await page.query_selector("a.next, a:has-text('Next'), .pagination-next")
                if not next_btn or await next_btn.get_attribute("href") is None:
                    break
                
                await next_btn.click()
                await page.wait_for_timeout(2000)
                
        except Exception as e:
            print(f"Error during discovery: {e}")
        finally:
            await page.close()
            
        return list(game_links)

    async def extract_game_details(self, context, url):
        async with self.semaphore:
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                
                # Title
                title_el = await page.query_selector("h1.entry-title, h1")
                if not title_el: return None
                title = await self.clean_title(await title_el.inner_text())

                # Size
                try:
                    size_el = page.get_by_text("Size:", exact=False)
                    if await size_el.count() > 0:
                        size_raw = await size_el.first.inner_text()
                        file_size = size_raw.replace("Size:", "").replace("-", "").strip()
                    else:
                        file_size = "Unknown"
                except: file_size = "Unknown"

                # URI (Direct Link)
                uris = []
                download_btn = page.get_by_text("Direct", exact=True)
                
                if await download_btn.count() > 0:
                    parent = download_btn.locator("xpath=..") 
                    href = await parent.get_attribute("href")
                    if href: uris.append(href)
                
                if not uris: return None

                # Date
                try:
                    date_el = page.get_by_text("Updated:", exact=False)
                    if await date_el.count() > 0:
                        upload_date = await self.parse_date(await date_el.first.inner_text())
                    else:
                        upload_date = datetime.datetime.now().strftime("%Y-%m-%d")
                except:
                    upload_date = datetime.datetime.now().strftime("%Y-%m-%d")

                return {
                    "title": title,
                    "fileSize": file_size,
                    "uris": uris,
                    "uploadDate": upload_date
                }

            except Exception as e:
                # print(f"Failed to scrape {url}: {e}") # Optional: Uncomment to see specific failures
                return None
            finally:
                await page.close()

    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS_MODE)
            links = await self.discover_links(browser)
            
            if not links:
                print("No links found. Exiting.")
                sys.exit(1)

            print(f"Starting extraction for {len(links)} games...")
            context = await browser.new_context()
            tasks = [self.extract_game_details(context, link) for link in links]
            results = await asyncio.gather(*tasks)
            valid_results = [r for r in results if r is not None]
            
            source_data = {
                "name": "AnkerGames",
                "downloads": valid_results
            }
            
            with open(OUTPUT_FILE, "w") as f:
                ujson.dump(source_data, f, indent=2)
            
            print(f"Success! Generated {OUTPUT_FILE} with {len(valid_results)} entries.")
            await browser.close()

if __name__ == "__main__":
    scraper = AnkerScraper()
    asyncio.run(scraper.run())

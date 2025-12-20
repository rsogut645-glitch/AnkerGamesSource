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
CONCURRENCY_LIMIT = 3  # Reduced to 3 because deep scraping takes more memory/cpu
HEADLESS_MODE = True

class AnkerScraper:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    async def clean_title(self, raw_title):
        clean = re.sub(r'\s*\(.*?\)', '', raw_title)
        clean = re.sub(r'\sv\d+(\.\d+)*', '', clean)
        return clean.strip()

    async def parse_date(self, date_text):
        try:
            clean_date = date_text.lower().replace("last updated:", "").strip()
            dt = datetime.datetime.strptime(clean_date, "%b %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return datetime.datetime.now().strftime("%Y-%m-%d")

    async def resolve_final_download_link(self, context, initial_url):
        """
        Follows the link chain to find the actual file URL.
        Handles: Intermediate pages, 'Click here' buttons, and auto-downloads.
        """
        # If the link is already external (e.g. drive.google, 1fichier), return it immediately
        if "ankergames.net" not in initial_url and "http" in initial_url:
            return initial_url

        page = await context.new_page()
        final_url = None
        
        try:
            # We wrap the navigation in a try/except specifically for the download event
            # because 'expect_download' waits for an event that might not happen if we just find a link.
            try:
                # Start listening for a download event (triggered by the countdown/auto-popup)
                async with page.expect_download(timeout=15000) as download_info:
                    await page.goto(initial_url, wait_until="domcontentloaded", timeout=30000)
                    
                    # Attempt to traverse up to 3 "steps" of buttons
                    for step in range(3):
                        # Look for common "Next Step" buttons
                        # prioritizing buttons that look like "Download" or "Continue"
                        btn = await page.query_selector("""
                            a.btn-download, 
                            a:has-text('Download'), 
                            button:has-text('Download'), 
                            a:has-text('Click here to continue'),
                            a:has-text('Go to Link')
                        """)
                        
                        if btn:
                            # Check if this button is just a direct link to an external site
                            href = await btn.get_attribute("href")
                            if href and "http" in href and "ankergames.net" not in href:
                                final_url = href
                                break # Found an external link, stop clicking
                            
                            # Otherwise, click it to go to the next step
                            await btn.click()
                            await page.wait_for_timeout(2000) # Wait for page reaction
                        else:
                            # No buttons found, maybe we are just waiting for the countdown?
                            await page.wait_for_timeout(2000)
                    
                    # If we found a text link above, return it
                    if final_url:
                        return final_url

                # If the 'async with' block finishes successfully, it means a download started!
                download = await download_info.value
                return download.url

            except asyncio.TimeoutError:
                # No download started automatically. 
                # Let's check if the current page URL is the destination (redirected).
                if "ankergames.net" not in page.url:
                    return page.url
                return None # Could not resolve
            except Exception as e:
                return None

        except Exception as outer_e:
            return None
        finally:
            await page.close()

    async def discover_links(self, browser):
        print("Starting Discovery Phase...")
        page = await browser.new_page()
        game_links = set()
        
        try:
            await page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
            while True:
                links = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('article a, .entry-title a, .post a')).map(a => a.href)
                """)
                new_links = {l for l in links if "/game/" in l or "/download/" in l}
                
                if not new_links:
                    links = await page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href)")
                    new_links = {l for l in links if len(l) > len(BASE_URL) + 15}

                game_links.update(new_links)
                print(f"Found {len(new_links)} games on this page. Total unique: {len(game_links)}")

                next_btn = await page.query_selector("a.next, a:has-text('Next')")
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

                # URI Discovery
                initial_uris = []
                
                # Find the initial "Direct" or "Download" button
                direct_btn = await page.query_selector("a:has-text('Direct'), a.btn-download")
                if direct_btn:
                    href = await direct_btn.get_attribute("href")
                    if href: initial_uris.append(href)
                
                # Fallback
                if not initial_uris:
                    download_links = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('a')).filter(a => a.innerText.includes('Download')).map(a => a.href)
                    """)
                    initial_uris.extend([l for l in download_links if "ankergames.net" in l or "http" in l])

                if not initial_uris: 
                    return None

                # Deep Resolve: Follow the link to find the REAL file
                final_uris = []
                for uri in initial_uris[:1]: # Limit to first link to save time
                     # Pass 'context' so we can open a new tab for resolution
                     resolved = await self.resolve_final_download_link(context, uri)
                     if resolved:
                         final_uris.append(resolved)

                if not final_uris:
                    return None

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
                    "uris": final_uris,
                    "uploadDate": upload_date
                }

            except Exception as e:
                return None
            finally:
                await page.close()

    async def run(self):
        async with async_playwright() as p:
            print("Launching Browser...")
            browser = await p.chromium.launch(headless=HEADLESS_MODE)
            links = await self.discover_links(browser)
            
            if not links:
                print("No links found. Exiting.")
                sys.exit(1)

            print(f"Starting extraction for {len(links)} games...")
            context = await browser.new_context(accept_downloads=True) # Enable downloads
            
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

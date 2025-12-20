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
CONCURRENCY_LIMIT = 2  # Reduced to 2 for stability with popups
HEADLESS_MODE = True

class AnkerScraper:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    async def clean_title(self, raw_title):
        clean = re.sub(r'\s*\(.*?\)', '', raw_title)
        clean = re.sub(r'\sv\d+(\.\d+)*', '', clean)
        return clean.strip()
    
    async def clean_size(self, raw_size):
        # Removes "Game", "Size:", and extra whitespace
        clean = re.sub(r'(?i)(Game|Size:|Size)\s*', '', raw_size)
        return clean.strip()

    async def parse_date(self, date_text):
        try:
            clean_date = date_text.lower().replace("last updated:", "").strip()
            dt = datetime.datetime.strptime(clean_date, "%b %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return datetime.datetime.now().strftime("%Y-%m-%d")

    async def handle_download_page(self, page):
        """
        Handles the final download page (countdown, verify buttons, etc.)
        Returns the final resolved URL string or None.
        """
        final_url = None
        try:
            # Wait for potential redirects or countdowns
            await page.wait_for_load_state("domcontentloaded")
            
            # Listen for the actual file download trigger
            # We wait up to 45 seconds for the countdown + click
            async with page.expect_download(timeout=45000) as download_info:
                
                # Logic to handle "Click here to download" buttons that appear after countdowns
                # We try clicking "Download" buttons if they exist
                for _ in range(5): # Check periodically
                    # Look for obvious download buttons
                    btn = await page.query_selector("""
                        a.btn-download, 
                        a:has-text('Download Now'), 
                        button:has-text('Download'), 
                        a[href*='drive.google.com'],
                        a[href*='mega.nz'],
                        a[href*='1fichier']
                    """)
                    
                    if btn:
                        href = await btn.get_attribute("href")
                        # If it's a known file host, we have our link!
                        if href and any(host in href for host in ['drive.google', 'mega.nz', '1fichier', 'mediafire']):
                            return href
                        
                        # Otherwise, it might be a button that triggers the download event
                        try:
                            if await btn.is_visible():
                                await btn.click()
                                await asyncio.sleep(2)
                        except: pass
                    
                    # Wait a bit for countdowns (e.g. "Wait 10 seconds...")
                    await asyncio.sleep(2)

            # If the 'expect_download' block catches a download event
            download = await download_info.value
            return download.url

        except asyncio.TimeoutError:
            # Timeout means no auto-download started.
            # Last ditch: check if current URL is the file host
            if any(host in page.url for host in ['drive.google', 'mega.nz', '1fichier']):
                return page.url
            return None
        except Exception:
            return None

    async def extract_game_details(self, context, url):
        async with self.semaphore:
            # We use a persistent page for the main game view
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                
                # --- METADATA ---
                title_el = await page.query_selector("h1.entry-title, h1")
                if not title_el: return None
                title = await self.clean_title(await title_el.inner_text())

                file_size = "Unknown"
                try:
                    size_el = page.get_by_text("Size:", exact=False)
                    if await size_el.count() > 0:
                        size_raw = await size_el.first.inner_text()
                        file_size = await self.clean_size(size_raw)
                except: pass

                upload_date = datetime.datetime.now().strftime("%Y-%m-%d")
                try:
                    date_el = page.get_by_text("Updated:", exact=False)
                    if await date_el.count() > 0:
                        upload_date = await self.parse_date(await date_el.first.inner_text())
                except: pass

                # --- DOWNLOAD RESOLUTION (POPUP HANDLING) ---
                resolved_uris = []
                
                # Find the "Direct" trigger button
                direct_btn = await page.query_selector("a:has-text('Direct'), a.btn-download, .download-button")
                
                if direct_btn:
                    # Case 1: The button opens a POPUP (New Tab)
                    # We have to catch the new page event
                    try:
                        async with context.expect_page(timeout=10000) as new_page_info:
                            # Check if we need to scroll to it to click
                            await direct_btn.scroll_into_view_if_needed()
                            await direct_btn.click()
                        
                        # Switch control to the new popup tab
                        popup_page = await new_page_info.value
                        try:
                            # Run logic on the popup
                            final_link = await self.handle_download_page(popup_page)
                            if final_link:
                                resolved_uris.append(final_link)
                        finally:
                            await popup_page.close() # Clean up popup
                            
                    except asyncio.TimeoutError:
                        # Case 2: No popup, maybe it navigated the CURRENT page?
                        # Or maybe it revealed a hidden link?
                        current_href = await direct_btn.get_attribute("href")
                        if current_href and "http" in current_href and "ankergames.net" not in current_href:
                            resolved_uris.append(current_href)
                        elif "ankergames.net" in page.url:
                             # Logic for if it redirected the current tab
                             final_link = await self.handle_download_page(page)
                             if final_link:
                                resolved_uris.append(final_link)

                # Fallback: if Deep Resolve failed, check for static links
                if not resolved_uris:
                    # Look for untracked links like 'Google Drive' text
                    static_links = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('a')).map(a => a.href)
                    """)
                    for sl in static_links:
                        if any(host in sl for host in ['drive.google.com', 'mega.nz', '1fichier']):
                            resolved_uris.append(sl)

                if not resolved_uris:
                    # print(f"Skipping {title}: No valid download link resolved.")
                    return None

                return {
                    "title": title,
                    "fileSize": file_size,
                    "uris": list(set(resolved_uris)), # Remove duplicates
                    "uploadDate": upload_date
                }

            except Exception as e:
                # print(f"Error extracting {url}: {e}")
                return None
            finally:
                await page.close()

    async def discover_links(self, browser):
        print("Starting Discovery Phase...", flush=True)
        page = await browser.new_page()
        game_links = set()
        
        # Safety breaker for "Load More" loops
        consecutive_no_new_games = 0
        
        try:
            await page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
            
            while True:
                # 1. Grab Links
                links = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('article a, .post a, .entry-title a')).map(a => a.href)
                """)
                # Strict filtering to avoid garbage links
                new_links = {l for l in links if "/game/" in l or "/download/" in l}
                
                # Deduplicate against what we already have
                current_batch_count = len(new_links - game_links)
                game_links.update(new_links)
                print(f"Scraped page. Found {current_batch_count} new games. Total: {len(game_links)}", flush=True)

                # Stop if "Load More" isn't yielding results after 3 tries
                if current_batch_count == 0 and len(game_links) > 0:
                    consecutive_no_new_games += 1
                    if consecutive_no_new_games >= 3:
                        print("No new games found after multiple clicks. Stopping.", flush=True)
                        break
                else:
                    consecutive_no_new_games = 0

                # 2. "Load More" Pagination Logic
                # Looks for various forms of "Load More" buttons
                load_more_btn = await page.query_selector("""
                    a:has-text('Load More'),
                    button:has-text('Load More'),
                    span:has-text('Load More'),
                    .load-more,
                    [class*='load-more'],
                    text=/Load\s*More/i
                """)
                
                if not load_more_btn or not await load_more_btn.is_visible():
                    print("No 'Load More' button found or visible. End of list.", flush=True)
                    break

                # Navigate / Click
                try:
                    await load_more_btn.scroll_into_view_if_needed()
                    await load_more_btn.click()
                    
                    # Wait for content to load (AJAX)
                    await asyncio.sleep(2) # Allow animation/request to start
                    try:
                        # Wait for network to settle, but don't crash if it doesn't
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except: pass
                    
                except Exception as e:
                    print(f"Pagination click error: {e}")
                    break
                
        except Exception as e:
            print(f"Error during discovery: {e}", flush=True)
        finally:
            await page.close()
        return list(game_links)

    async def run(self):
        async with async_playwright() as p:
            print("Launching Browser...", flush=True)
            browser = await p.chromium.launch(headless=HEADLESS_MODE)
            
            # Step 1: Discovery
            links = await self.discover_links(browser)
            if not links:
                print("No links found. Exiting.", flush=True)
                sys.exit(1)

            # Step 2: Extraction
            print(f"Starting extraction for {len(links)} games...", flush=True)
            context = await browser.new_context(accept_downloads=True)
            
            valid_results = []
            try:
                tasks = [self.extract_game_details(context, link) for link in links]
                results = await asyncio.gather(*tasks)
                valid_results = [r for r in results if r is not None]
            except Exception as e:
                print(f"Critical error: {e}", flush=True)
            finally:
                # Save results
                source_data = {
                    "name": "AnkerGames",
                    "downloads": valid_results
                }
                with open(OUTPUT_FILE, "w") as f:
                    ujson.dump(source_data, f, indent=2)
                
                print(f"Done. Generated {OUTPUT_FILE} with {len(valid_results)} entries.", flush=True)
                await browser.close()

if __name__ == "__main__":
    scraper = AnkerScraper()
    asyncio.run(scraper.run())

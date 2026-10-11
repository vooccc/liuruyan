#!/usr/bin/env python3
"""
Browser Tab Manager - Universal tool for any topic
Version: 1.0.4
Last updated: 2026-02-15
Usage: python3 browser_tools.py <action> [args]

CHANGELOG:
- v1.0.4: Fixed navigation - opens target_url in found tabs
- v1.0.3: Added CDP tab ID and WebSocket URL in output
- v1.0.2: Fixed tab search across ALL contexts, no duplicates
- v1.0.1: Added x.com/twitter.com domain matching
- v1.0.0: Initial version
"""
import asyncio
import sys
import json
import requests
from urllib.parse import urlparse
from playwright.async_api import async_playwright

__version__ = "1.0.4"

CDP_URL = "http://localhost:9222"


def get_page_domain(page_url):
    """Extract domain from URL"""
    try:
        parsed = urlparse(page_url)
        return parsed.netloc.lower()
    except:
        return ""


def match_domain(target_domain, page_url):
    """Check if target domain matches page URL"""
    target = target_domain.lower()
    page_domain = get_page_domain(page_url)
    
    # Direct match
    if target in page_domain:
        return True
    
    # Handle x.com/twitter.com variations
    if target == "x.com" and ("x.com" in page_domain or "twitter.com" in page_domain):
        return True
    if target == "twitter.com" and ("twitter.com" in page_domain or "x.com" in page_domain):
        return True
    
    return False


def get_cdp_tabs():
    """Get CDP tabs list with IDs"""
    try:
        response = requests.get(f"{CDP_URL}/json", timeout=10)
        return response.json()
    except Exception as e:
        print(f"[WARNING] Could not get CDP list: {e}")
        return []


async def get_existing_tab(browser, target_domain):
    """
    Find existing tab by domain across ALL contexts
    Returns: (page, cdp_id, ws_url) or (None, None, None)
    """
    print(f"[SEARCH] Looking for domain: {target_domain}")
    print(f"[SEARCH] Total contexts: {len(browser.contexts)}")
    
    # Get CDP tabs with IDs
    cdp_tabs = get_cdp_tabs()
    
    for ctx_idx, context in enumerate(browser.contexts):
        print(f"[SEARCH] Checking context {ctx_idx}: {len(context.pages)} pages")
        
        for page in context.pages:
            print(f"[SEARCH]   Checking page: {page.url[:60]}...")
            
            if match_domain(target_domain, page.url):
                # Find matching CDP tab for ID
                tab_id = "N/A"
                ws_url = "N/A"
                for cdp_tab in cdp_tabs:
                    if cdp_tab.get("url") == page.url and cdp_tab.get("type") == "page":
                        tab_id = cdp_tab.get("id", "N/A")
                        ws_url = cdp_tab.get("webSocketDebuggerUrl", "N/A")
                        break
                
                print(f"[SEARCH] FOUND in context {ctx_idx}: {page.url}")
                print(f"[SEARCH]   CDP ID: {tab_id}")
                print(f"[SEARCH]   WebSocket: {ws_url[:50]}..." if ws_url != "N/A" else "")
                return page, tab_id, ws_url
    
    print(f"[SEARCH] No existing tab found for: {target_domain}")
    return None, None, None


async def list_tabs():
    """List all open tabs from ALL contexts with CDP info"""
    cdp_tabs = get_cdp_tabs()
    
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp(CDP_URL)
    
    all_tabs = []
    tab_index = 0
    
    for ctx_idx, context in enumerate(browser.contexts):
        for pg_idx, page in enumerate(context.pages):
            try:
                title = await page.title()
            except:
                title = "N/A"
            
            # Find matching CDP tab for ID
            tab_id = "N/A"
            ws_url = "N/A"
            for cdp_tab in cdp_tabs:
                if cdp_tab.get("url") == page.url and cdp_tab.get("type") == "page":
                    tab_id = cdp_tab.get("id", "N/A")
                    ws_url = cdp_tab.get("webSocketDebuggerUrl", "N/A")
                    break
            
            all_tabs.append({
                "index": tab_index,
                "context": ctx_idx,
                "cdp_id": tab_id,
                "websocket_url": ws_url,
                "url": page.url,
                "domain": get_page_domain(page.url),
                "title": title
            })
            tab_index += 1
    
    await browser.close()
    await p.stop()
    return all_tabs


async def find_or_create_tab(target_domain, target_url=None):
    """
    Find existing tab by domain and navigate to target_url
    Returns: (page, browser, playwright, is_new, cdp_id, ws_url)
    """
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp(CDP_URL)
    
    # Search in ALL contexts
    existing_page, tab_id, ws_url = await get_existing_tab(browser, target_domain)
    
    if existing_page:
        print(f"[RESULT] Using EXISTING tab: {existing_page.url}")
        print(f"[RESULT] CDP ID: {tab_id}")
        print(f"[RESULT] WebSocket: {ws_url}")
        
        # NAVIGATE to target_url if provided and different from current
        if target_url and existing_page.url != target_url:
            print(f"[NAVIGATE] Going to: {target_url}")
            await existing_page.goto(target_url)
            await existing_page.wait_for_load_state("networkidle")
            print(f"[NAVIGATE] Done: {existing_page.url}")
            # Get fresh CDP info after navigation
            cdp_tabs = get_cdp_tabs()
            for cdp_tab in cdp_tabs:
                if cdp_tab.get("url") == existing_page.url and cdp_tab.get("type") == "page":
                    tab_id = cdp_tab.get("id", tab_id)
                    ws_url = cdp_tab.get("webSocketDebuggerUrl", ws_url)
                    break
        
        return existing_page, browser, p, False, tab_id, ws_url
    
    # Create new tab in first context
    if browser.contexts:
        context = browser.contexts[0]
    else:
        context = await browser.new_context()
    
    new_page = await context.new_page()
    if target_url:
        await new_page.goto(target_url)
        await new_page.wait_for_load_state("networkidle")
    
    # Get CDP info for new tab
    cdp_tabs = get_cdp_tabs()
    tab_id = "N/A"
    ws_url = "N/A"
    for cdp_tab in cdp_tabs:
        if cdp_tab.get("url") == new_page.url and cdp_tab.get("type") == "page":
            tab_id = cdp_tab.get("id", "N/A")
            ws_url = cdp_tab.get("webSocketDebuggerUrl", "N/A")
            break
    
    print(f"[RESULT] Created NEW tab: {new_page.url}")
    print(f"[RESULT] CDP ID: {tab_id}")
    print(f"[RESULT] WebSocket: {ws_url}")
    return new_page, browser, p, True, tab_id, ws_url


async def main():
    if len(sys.argv) < 2:
        print("Usage: browser_tools.py <action> [args]")
        print("Actions: list_tabs, find_or_create_tab <domain> [url]")
        return
    
    action = sys.argv[1]
    
    if action == "list_tabs":
        tabs = await list_tabs()
        print(json.dumps(tabs, indent=2, ensure_ascii=False))
        return
    
    elif action == "find_or_create_tab":
        if len(sys.argv) < 3:
            print("Usage: find_or_create_tab <domain> [url]")
            return
        domain = sys.argv[2]
        url = sys.argv[3] if len(sys.argv) > 3 else None
        
        page, browser, p, is_new, tab_id, ws_url = await find_or_create_tab(domain, url)
        print(f"\n{'='*50}")
        print(f"URL: {page.url}")
        print(f"CDP ID: {tab_id}")
        print(f"WebSocket: {ws_url}")
        print(f"Is new: {is_new}")
        print(f"{'='*50}")
        await browser.close()
        await p.stop()
        return
    
    print(f"Unknown action: {action}")


if __name__ == "__main__":
    asyncio.run(main())

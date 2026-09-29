---
name: browser-automation
description: "Control remote Chrome browser via CDP for tab management, navigation, screenshots, and automation. Use when: (1) Finding existing browser tabs by domain without creating duplicates, (2) Navigating to specific URLs in existing or new tabs, (3) Taking screenshots of web pages, (4) Automating browser interactions, (5) Managing multiple browser contexts and pages."
---

# Browser Automation Skill

Control remote Chrome browser via Chrome DevTools Protocol (CDP) for reliable tab management and automation.

## Core Features

- **No duplicate tabs**: Always finds existing tabs by domain
- **Domain-based search**: Match tabs by domain (handles x.com/twitter.com variations)
- **Automatic navigation**: Opens target URL in found or created tab
- **CDP integration**: Returns tab ID and WebSocket URL for direct control
- **Cross-context search**: Checks ALL browser contexts, not just the first

## Prerequisites

- Chrome running with remote debug port (e.g., `--remote-debugging-port=9222`)
- CDP endpoint accessible (e.g., `http://localhost:9222`)
- Python 3.8+ with playwright installed

## Quick Start

```python
import sys
sys.path.insert(0, '<skill-path>/scripts')
from browser_tools import find_or_create_tab

async def example():
    # Find tab with domain "x.com" and navigate to URL
    page, browser, p, is_new, cdp_id, ws_url = await find_or_create_tab(
        "x.com",                           # Domain to search for
        "https://x.com/oraclesrun"         # URL to open in the tab
    )
    
    # is_new = False if reused existing tab, True if created new
    # cdp_id = Chrome DevTools tab ID
    # ws_url = WebSocket URL for direct CDP connection
    
    await page.screenshot(path="/tmp/screenshot.png")
    
    # Close connection, keep tab open
    await browser.close()
    await p.stop()
```

## Main Functions

### find_or_create_tab(domain, target_url=None)

Find existing tab by domain or create new one, then navigate to target_url.

**Parameters:**
- `domain` (str): Domain to search for (e.g., "x.com", "github.com")
- `target_url` (str, optional): URL to navigate to in the tab

**Returns:** `(page, browser, playwright, is_new, cdp_id, ws_url)`
- `page`: Playwright page object
- `browser`: Playwright browser object
- `p`: Playwright instance
- `is_new`: Boolean (False=reused existing, True=created new)
- `cdp_id`: Chrome DevTools tab ID
- `ws_url`: WebSocket debugger URL

### list_tabs()

List all open tabs across ALL browser contexts with CDP information.

**Returns:** List of dicts with keys:
- `index`: Tab index
- `context`: Context index
- `cdp_id`: Chrome DevTools tab ID
- `websocket_url`: WebSocket debugger URL
- `url`: Tab URL
- `domain`: Extracted domain
- `title`: Page title

## Domain Matching

The skill handles domain variations automatically:

| Search Domain | Matches URLs |
|--------------|-------------|
| `x.com` | x.com, twitter.com |
| `twitter.com` | twitter.com, x.com |
| `github.com` | github.com only |

## Command Line Usage

```bash
# List all tabs
python3 browser_tools.py list_tabs

# Find or create tab
python3 browser_tools.py find_or_create_tab <domain> [url]
```

## Configuration

Edit `scripts/browser_tools.py` to change CDP endpoint:

```python
CDP_URL = "http://localhost:9222"  # Change to your Chrome debug URL
```

## Version History

- **1.0.4**: Fixed navigation - now opens target_url in found tabs
- **1.0.3**: Added CDP ID and WebSocket URL in output
- **1.0.2**: Fixed tab search across ALL contexts
- **1.0.1**: Added x.com/twitter.com domain matching
- **1.0.0**: Initial release

# Brute-LI Scanner 🔍

A powerful **Playwright-based** web scraping and URL enumeration tool that combines 
dynamic JavaScript scraping with concurrent URL status checking.

## 🎯 What It Does

Brute-LI Scanner is designed to **discover hidden URLs and endpoints** on websites by:

1. **Dynamic JavaScript Analysis** - Executes JavaScript in the browser to capture dynamically loaded URLs and scripts
2. **Smart URL Extraction** - Finds URLs embedded in:
   - HTML content
   - JavaScript files (both inline and external)
   - Dynamically injected scripts (using MutationObserver)
3. **Intelligent Status Checking** - Verifies each discovered URL with:
   - Accurate HTTP status codes
   - Real response body content length
   - Smart 403 detection (distinguishes between actual blocks and accessible pages)
4. **Domain Filtering** - Keeps results within target domain using registered domain extraction
5. **Deduplication** - Maintains a persistent URL database to avoid re-scanning

## ✨ Key Features

- 🌐 **Playwright Browser Automation** - Uses headless Chromium for accurate page rendering
- 🔐 **WAF/Bot Bypass** - Rotates user agents and headers to avoid detection
- ⚡ **Real Content Retrieval** - Captures actual response bodies, not just headers
- 🎨 **Colored Output** - Color-coded status codes for quick visual scanning
  - 🟢 Green: 200-299 (Success)
  - 🟡 Yellow: 300-399 (Redirects) & 403 with content
  - 🔴 Red: Errors & 403 blocks
  - 🟣 Magenta: 404 (Not Found)
  - 🔵 Cyan: Other status codes
- 📊 **Performance Metrics** - Shows content length for each URL
- 💾 **Persistent Storage** - Appends new URLs to `urls.txt` (avoids duplicates)
- ⏱️ **Configurable Timeouts** - Adjust scanning duration and request delays

## 🛠️ How It Works

### Phase 1: JavaScript Scraping
- Launches headless Chromium
- Enables hidden/disabled elements via JavaScript injection
- Extracts URLs from HTML, scripts, and dynamically loaded content
- Monitors for new scripts injected by JavaScript (MutationObserver)
- Resolves relative URLs to absolute URLs
- Filters results to target domain only

### Phase 2: URL Status Checking
- Creates a fresh page for each URL (real browser navigation)
- Captures actual HTTP response status codes
- Retrieves full response body for accurate content length
- Smart 403 detection (content > 5000 bytes = likely accessible)
- Displays results in real-time with color coding

## 📦 Requirements

- Python 3.7+
- Playwright (with Chromium)
- requests
- tldextract
- colorama

## 🚀 Installation

```bash
# Clone the repository
git clone https://github.com/57ayush76/Brute-li.git
cd brute-li

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

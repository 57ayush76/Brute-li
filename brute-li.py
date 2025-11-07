#!/usr/bin/env python3
"""
brute_li.py — combined Playwright + JS scraper + status checker

Features:
- Enables hidden/disabled elements (main frame + same-origin iframes)
- Runs a dynamic JS scraper that fetches external scripts and watches for dynamically injected scripts
- Resolves relative URLs, filters to registered domain, deduplicates
- Appends only NEW URLs to urls.txt
- Concurrent HEAD->GET status checks with colored output
"""

import sys
import time
import random
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# NOTE: requests is kept for safe_fetch_preview only (unchanged). Status checks no longer use requests.
import requests
from playwright.sync_api import sync_playwright
import tldextract
import colorama
from colorama import Fore, Style

colorama.init(autoreset=True)


# ---------- Configurable knobs ----------
HEADLESS = True

# JS_SCRAPE runtime tuning (already embedded in JS, mirrored here so you can change easily)
INACTIVITY_MS = 2000   # how long to wait after last new script (ms)
MAX_TOTAL_MS = 15000   # overall cap (ms)
CONCURRENCY_FETCH = 6  # concurrency for fetches inside page

# HTTP status check tuning
# NOTE: We now perform status checks using Playwright's APIRequestContext (browser-like)
STATUS_MAX_WORKERS = 6   # still used to throttle printing concurrency if desired
HEAD_TIMEOUT = 8       # seconds for HEAD requests
GET_TIMEOUT = 10       # seconds for fallback GET requests
DELAY_BETWEEN_CHECKS = 0.06  # polite delay (seconds) between checks to reduce WAF throttling

# ---------- Header rotation for avoiding 403 errors ----------
def get_random_headers():
    """Returns a random set of headers to avoid simple blocking."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/119.0",
        "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/119.0"
    ]

    headers_sets = [
        {
            "User-Agent": random.choice(user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        },
        {
            "User-Agent": random.choice(user_agents),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        },
        {
            "User-Agent": random.choice(user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "DNT": "1"
        }
    ]

    return random.choice(headers_sets)


# ---------- Helpers for registered domain checking ----------
def get_registered_domain(url):
    """
    Return registered domain like example.com from a URL.
    Falls back to hostname if tldextract cannot find domain+suffix.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "http://" + url
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    ext = tldextract.extract(hostname)
    if ext.domain and ext.suffix:
        return ext.domain + "." + ext.suffix
    return hostname


def registered_domain_of_netloc(netloc):
    """Given a netloc or hostname, return registered domain (or hostname fallback)."""
    hostname = netloc.split('@')[-1].split(':')[0]
    ext = tldextract.extract(hostname)
    if ext.domain and ext.suffix:
        return ext.domain + "." + ext.suffix
    return hostname


def same_registered_domain(url_to_check, base_registered_domain):
    """Return True if url_to_check belongs to the same registered domain."""
    try:
        parsed = urlparse(url_to_check)
        host = parsed.hostname or ""
        return registered_domain_of_netloc(host) == base_registered_domain
    except Exception:
        return False


# ---------- Enable / reveal JS (no alerts, safe for headless) ----------
ENABLE_SCRIPT = r"""
(() => {
  try {
    function walkAndApply(root) {
      const nodes = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
      for (const n of nodes) {
        try {
          if (n.hasAttribute && (n.hasAttribute('disabled') || n.hasAttribute('readonly'))) {
            n.removeAttribute('disabled');
            n.removeAttribute('readonly');
          }
          const s = n.getAttribute && n.getAttribute('style');
          if (s && /display\s*:\s*none/i.test(s)) {
            n.style.display = 'block';
          }
          if (s && /pointer-events\s*:\s*none/i.test(s)) {
            n.style.pointerEvents = 'auto';
            n.style.opacity = n.style.opacity || '1';
          }
          if (n.shadowRoot && n.shadowRoot.querySelectorAll) {
            walkAndApply(n.shadowRoot);
          }
        } catch (e) { /* ignore per-node errors */ }
      }
    }
    walkAndApply(document);
    // also set inline display for computed display:none elements (best-effort)
    Array.from(document.querySelectorAll('*')).forEach(el => {
      try {
        const cs = window.getComputedStyle(el);
        if (cs && cs.display === 'none') {
          el.style.display = el.style.display || 'block';
        }
      } catch (e) {}
    });
    return {ok: true, note: "elements processed"};
  } catch (err) {
    return {ok: false, error: String(err)};
  }
})();
"""


# ---------- JS_SCRAPE: dynamic scanner with MutationObserver & fetches ----------
# This JS will be evaluated in page context and must return an Array of strings.

# config values (keep your own values)
INACTIVITY_MS = 2000
MAX_TOTAL_MS = 15000
CONCURRENCY_FETCH = 6

# safe JS_SCRAPE with placeholders — no % formatting, no f-string brace escaping needed
JS_SCRAPE = r"""
(async () => {
  const REGEX = /(?:(?:https?:\/\/)[\w\-\._~:\/?#\[\]@!$&'()*+,;=%]+|(?:\.{0,2}\/)[\w\-\._~\/\?#=&%]+|\/[\w\-\._~\/\?#=&%]+)/g;
  const results = new Set();
  const processedExternal = new Set();

  const INACTIVITY_MS = __INACTIVITY_MS__; // ms
  const MAX_TOTAL_MS = __MAX_TOTAL_MS__; // ms
  const CONCURRENCY = __CONCURRENCY__;

  function sanitizeAndAdd(raw) {
    if (!raw) return;
    raw = raw.trim();
    if (!raw) return;
    if (/^(javascript|mailto|tel|data|about):/i.test(raw)) return;
    raw = raw.replace(/[,;)"'\]]+$/,'');
    results.add(raw);
  }

  function extractFromText(text) {
    if (!text) return;
    for (const m of text.matchAll(REGEX)) {
      sanitizeAndAdd(m[0]);
    }
  }

  // initial scan
  try {
    extractFromText(document.documentElement.outerHTML);
    const scripts = Array.from(document.getElementsByTagName('script'));
    for (const s of scripts) {
      if (s.src) sanitizeAndAdd(s.src);
      if (s.innerText) extractFromText(s.innerText);
    }
  } catch (e) { /* ignore */ }

  async function fetchAndExtract(list) {
  const DELAY = 250, RETRIES = 2, BASE = 500;
  let idx = 0;
  async function worker() {
    while (true) {
      const i = idx++;
      if (i >= list.length) break;
      const src = list[i];
      if (!src || processedExternal.has(src)) continue;
      processedExternal.add(src);
      let t = null, a = 0;
      while (a <= RETRIES) {
        try {
          const r = await fetch(src, { credentials: 'omit' }).catch(_ => null);
          if (!r) break;
          if (r.status >= 200 && r.status < 300) {
            t = await r.text().catch(_ => null);
            break;
          }
          if (r.status === 404 || (r.status >= 500 && r.status < 600)) {
            a++;
            await new Promise(x => setTimeout(x, BASE * Math.pow(2, a - 1)));
            continue;
          }
          t = await r.text().catch(_ => null);
          break;
        } catch (e) { break; }
      }
      if (t) extractFromText(t);
      await new Promise(x => setTimeout(x, DELAY));
    }
  }
  const workers = Array.from({ length: Math.min(CONCURRENCY, list.length) }, () => worker());
  await Promise.all(workers);
}

  function collectExternalSrcs() {
    const scripts = Array.from(document.getElementsByTagName('script'));
    return scripts.map(s => s.src).filter(Boolean);
  }

  // fetch initial externals
  let initialExternals = collectExternalSrcs().filter(s => !processedExternal.has(s));
  if (initialExternals.length) await fetchAndExtract(initialExternals);

  let lastChange = Date.now();
  const observer = new MutationObserver(async (mutations) => {
    let newExternals = [];
    for (const m of mutations) {
      if (m.addedNodes && m.addedNodes.length) {
        for (const n of m.addedNodes) {
          try {
            if (n.tagName && n.tagName.toLowerCase() === 'script') {
              if (n.src) {
                sanitizeAndAdd(n.src);
                if (!processedExternal.has(n.src)) newExternals.push(n.src);
              }
              if (n.innerText) extractFromText(n.innerText);
            } else if (n.querySelectorAll) {
              const nested = n.querySelectorAll('script');
              for (const s of nested) {
                if (s.src) {
                  sanitizeAndAdd(s.src);
                  if (!processedExternal.has(s.src)) newExternals.push(s.src);
                }
                if (s.innerText) extractFromText(s.innerText);
              }
            }
          } catch (e) { /* ignore node access errors */ }
        }
      }
      if (m.type === 'attributes' && m.target && m.target.tagName && m.target.tagName.toLowerCase() === 'script') {
        const t = m.target;
        if (t.src) {
          sanitizeAndAdd(t.src);
          if (!processedExternal.has(t.src)) newExternals.push(t.src);
        }
        if (t.innerText) extractFromText(t.innerText);
      }
    }

    if (newExternals.length) {
      lastChange = Date.now();
      fetchAndExtract(newExternals).catch(_ => {});
    } else {
      lastChange = Date.now();
    }
  });

  observer.observe(document.documentElement || document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['src']
  });

  const start = Date.now();
  while (true) {
    const now = Date.now();
    if (now - start > MAX_TOTAL_MS) break;
    if (now - lastChange > INACTIVITY_MS) break;
    await new Promise(res => setTimeout(res, 200));
  }

  observer.disconnect();

  // Final rescan
  try {
    extractFromText(document.documentElement.outerHTML);
    const scripts = Array.from(document.getElementsByTagName('script'));
    for (const s of scripts) {
      if (s.src) sanitizeAndAdd(s.src);
      if (s.innerText) extractFromText(s.innerText);
    }
  } catch (e) {}

  return Array.from(results);
})();
"""

# inject numeric values by replacing placeholders (safe)
JS_SCRAPE = JS_SCRAPE.replace("__INACTIVITY_MS__", str(INACTIVITY_MS))
JS_SCRAPE = JS_SCRAPE.replace("__MAX_TOTAL_MS__", str(MAX_TOTAL_MS))
JS_SCRAPE = JS_SCRAPE.replace("__CONCURRENCY__", str(CONCURRENCY_FETCH))



# ---------- Playwright runner (modified: return page/browser + keep playwright open for request context) ----------
def run_playwright_scraper_with_browser(pw, browser, target_url):
    """
    Use an already-open Playwright instance + browser to open target_url, run enable script across frames,
    then evaluate JS_SCRAPE. Returns list of candidate strings.
    """
    page = browser.new_page()
    try:
        # try networkidle; fallback to domcontentloaded
        try:
            page.goto(target_url, wait_until="networkidle", timeout=30000)
        except Exception:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
    except Exception as e:
        print(f"{Fore.RED}Failed to open page: {e}{Style.RESET_ALL}")
        page.close()
        return []

    # 1) Inject enable script in main frame
    try:
        res = page.evaluate(ENABLE_SCRIPT)
    except Exception as e:
        print(f"{Fore.YELLOW}Enable-script failed in main frame: {e}{Style.RESET_ALL}")

    # 2) Inject enable script in same-origin frames (best effort)
    try:
        for f in page.frames:
            try:
                if f == page.main_frame:
                    continue
                # evaluate in frame; cross-origin frames will raise
                f.evaluate(ENABLE_SCRIPT)
            except Exception:
                # ignore cross-origin / access errors
                pass
    except Exception:
        pass

    # optional tiny wait to allow reactive scripts to run after enabling
    page.wait_for_timeout(400)

    # 3) Evaluate JS_SCRAPE in main frame (Playwright resolves the Promise)
    found = []
    try:
        found = page.evaluate(JS_SCRAPE)
    except Exception as e:
        print(f"{Fore.RED}JS_SCRAPE execution failed: {e}{Style.RESET_ALL}")
        found = []
    finally:
        page.close()

    return found


# ---------- Utility: safe preview (optional) ----------
def safe_fetch_preview(url, timeout=6, max_chars=800):
    headers = get_random_headers()
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        content = r.text
        return content[:max_chars].replace("\n", " ").replace("\r", " ")
    except Exception as e:
        return f"[fetch failed: {e}]"


# ---------- Entry point ----------
if __name__ == "__main__":
    BANNER = r"""
                        ____                .                   .
                       /   \  .___  ,   . _/_     ___          /      `'
                       |,_-<  /   \ |   |  |    .'   `         |      |
                       |    ` | - ' |   |  |    |----'   ---   |      |
                       `----' /     `._/|  \__/ `.___,         /---/  '

"""
    print(BANNER)
    print("\nSimple Brute-LI Scanner — Playwright + dynamic JS scraping + concurrent status checking\n")

    raw = input("Enter URL to scan: ").strip()
    if not raw:
        print("No URL provided. Exiting.")
        sys.exit(0)

    # Normalize URL: ensure scheme
    def normalize_url(raw_in):
        s = raw_in.strip()
        s = s.rstrip("/\\")
        if not (s.startswith("http://") or s.startswith("https://")):
            s = "https://" + s
        return s

    url = normalize_url(raw)
    print(f"\nScanning URL: {url}\n(Starting headless browser and running dynamic JS scraper — this may take a few seconds)\n")

    # Start Playwright once and reuse for scraping + request checks
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        try:
            found = run_playwright_scraper_with_browser(pw, browser, url)
        except Exception as e:
            print(f"{Fore.RED}Scraper failure: {e}{Style.RESET_ALL}")
            browser.close()
            sys.exit(1)

        if not found:
            print(f"{Fore.YELLOW}No URLs/paths found by JS scraper.{Style.RESET_ALL}")
            browser.close()
            sys.exit(0)

        # Ensure iterable list
        if isinstance(found, str):
            found = [found]
        found = list(found)

        # Resolve relative paths to absolute URLs where possible, filter non-http(s)
        resolved = []
        for u in found:
            if not u:
                continue
            u = u.strip()
            try:
                if u.startswith("http://") or u.startswith("https://"):
                    resolved_url = u
                else:
                    resolved_url = urljoin(url, u)
            except Exception:
                continue
            parsed = urlparse(resolved_url)
            if parsed.scheme not in ("http", "https"):
                continue
            resolved.append(resolved_url)

        # Deduplicate and sort
        resolved = sorted(set(resolved))

        # Filter to same registered domain
        domain = get_registered_domain(url)
        current_scan_urls = [u for u in resolved if same_registered_domain(u, domain)]

        if not current_scan_urls:
            print(f"{Fore.YELLOW}No URLs belonging to registered domain '{domain}' were found.{Style.RESET_ALL}")
            browser.close()
            sys.exit(0)

        # Avoid duplicates across runs: read existing urls.txt
        existing_urls = set()
        try:
            with open("urls.txt", "r") as f:
                for line in f:
                    existing_urls.add(line.strip())
        except FileNotFoundError:
            pass

        # Write only new URLs
        new_urls = [u for u in current_scan_urls if u not in existing_urls]
        if new_urls:
            with open("urls.txt", "a") as f:
                for u in new_urls:
                    f.write(u + "\n")
            print(f"{Fore.CYAN}Appended {len(new_urls)} new URL(s) to urls.txt{Style.RESET_ALL}")
        else:
            print(f"{Fore.CYAN}No new URLs to append (all found URLs are already in urls.txt).{Style.RESET_ALL}")

        # ---------- Status checks using Playwright page.goto() for accurate content ----------
        print(f"\n🔍 Checking URL status codes for current scan ({len(current_scan_urls)} URLs)...")
        print("=" * 80)

        total = len(current_scan_urls)
        i = 0

        # Use page navigation for each URL to get actual response body content
        for u in current_scan_urls:
            i += 1
            status = None
            err = None
            content_length = 0
            try:
                # Create a new page for each URL to avoid state issues
                page = browser.new_page()
                try:
                    # Navigate to the URL and capture the response
                    response = page.goto(u, wait_until="domcontentloaded", timeout=GET_TIMEOUT * 1000)
                    if response:
                        status = response.status
                        try:
                            # Get the response body and calculate its length
                            body = response.body()
                            content_length = len(body) if body else 0
                        except Exception:
                            # Fallback: try to get content from the page itself
                            try:
                                body_text = page.content()
                                content_length = len(body_text.encode('utf-8')) if body_text else 0
                            except Exception:
                                content_length = 0
                    else:
                        err = "No response received"
                except Exception as e:
                    err = str(e)[:200]
                finally:
                    page.close()
            except Exception as e:
                err = str(e)[:200]

            # print results (keep same coloring logic)
            if err:
                # Normalize common Playwright network error messages
                msg = err
                if "Timeout" in msg or "timed out" in msg:
                    print(f"{Fore.RED}[{i}/{total}] {u} - TIMEOUT")
                elif "ECONNREFUSED" in msg or "ERR_CONNECTION_REFUSED" in msg or "net::ERR" in msg or "connect" in msg.lower():
                    print(f"{Fore.RED}[{i}/{total}] {u} - CONNECTION ERROR")
                else:
                    print(f"{Fore.RED}[{i}/{total}] {u} - ERROR: {msg}")
            else:
                if status is None:
                    print(f"{Fore.RED}[{i}/{total}] {u} - NO STATUS")
                else:
                    if 200 <= status < 300:
                        color = Fore.GREEN
                    elif 300 <= status < 400:
                        color = Fore.YELLOW
                    elif status == 403:
                        # 403 with content might still be accessible in browser
                        # Check if response has substantial content (not just error page)
                        if content_length > 5000:  # threshold for real content vs error page
                            color = Fore.YELLOW  # Treat as warning instead of blocked
                        else:
                            color = Fore.RED  # Likely blocked
                    elif status == 404:
                        color = Fore.MAGENTA
                    else:
                        color = Fore.CYAN
                    print(f"{color}[{i}/{total}] {u} - {status} - {content_length} bytes")

            # polite delay between requests to reduce WAF throttling
            time.sleep(DELAY_BETWEEN_CHECKS)

        browser.close()

    print(f"\nFinished checking {total} URLs. Results appended to urls.txt (if new).")

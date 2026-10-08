"""Built-in node: Web Search — DuckDuckGo search with HTML fallbacks."""
from typing import Dict, Any, List, Optional
from tubecli.nodes.base_node import BaseNode, PortType
from ddgs import DDGS
import requests
import re
import concurrent.futures
import time
from urllib.parse import urlparse


class WebSearchNode(BaseNode):
    node_type = "web_search"
    display_name = "🔍 Web Search"
    description = "Fast web search via HTTP (no browser needed). Uses DuckDuckGo + Google fallback."
    icon = "🔍"
    category = "Network"
    config_schema = {
        "query": {"type": "string", "description": "Fallback search query when the `query` input port is not connected."},
    }

    def _setup_ports(self):
        self.add_input("query", PortType.TEXT, "Search query")
        self.add_output("results", PortType.TEXT, "Search results as formatted text")
        self.add_output("structured_results", PortType.JSON, "Normalized search result objects")
        self.add_output("raw_html", PortType.TEXT, "Raw HTML snippet (for debugging)")
        self.add_output("status", PortType.TEXT, "Execution status")

    async def execute(self, inputs: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        query = inputs.get("query") or inputs.get("prompt") or inputs.get("text") or self.config.get("query", "")

        if not query:
            return {
                "results": "",
                "structured_results": [],
                "raw_html": "",
                "status": "Error: No search query provided",
            }

        print(f"  [WebSearch] Searching: {query}")
        start = time.time()

        try:
            results = self._search(query)
            results_text = self._format_results(query, results)
            elapsed = time.time() - start
            if results_text:
                print(f"  [WebSearch] ✅ Got results in {elapsed:.1f}s")
                return {
                    "results": results_text,
                    "structured_results": results,
                    "raw_html": "",
                    "status": f"✅ Found results for: {query} ({elapsed:.1f}s)",
                }
            else:
                return {
                    "results": f"No results found for: {query}",
                    "structured_results": [],
                    "raw_html": "",
                    "status": "⚠️ No results",
                }
        except Exception as e:
            elapsed = time.time() - start
            print(f"  [WebSearch] ❌ Error after {elapsed:.1f}s: {e}")
            return {
                "results": f"Search error: {e}",
                "structured_results": [],
                "raw_html": "",
                "status": f"❌ Error: {e}",
            }

    @staticmethod
    def _normalize_result(result: Dict[str, Any], source: Optional[str] = None) -> Dict[str, Any]:
        url = result.get("url") or result.get("href") or result.get("link") or ""
        result_source = (
            result.get("source")
            or (urlparse(url).netloc if url else "")
            or source
            or "DuckDuckGo"
        )
        return {
            "title": result.get("title") or "",
            "url": url,
            "snippet": result.get("snippet") or result.get("body") or "",
            "published_date": (
                result.get("published_date")
                or result.get("date")
                or result.get("published")
            ),
            "source": result_source or "",
        }

    @staticmethod
    def _is_news_query(query: str) -> bool:
        return bool(re.search(
            r"\b(news|latest|current|today|recent|breaking|headlines|newsworthy)\b",
            query,
            re.IGNORECASE,
        ))

    def _search(self, query: str, num_results: int = 6) -> List[Dict[str, Any]]:
        """Use DDGS text/news search first, retaining HTML scraping as fallback."""
        try:
            with DDGS() as ddgs:
                if self._is_news_query(query):
                    raw_results = list(ddgs.news(query, max_results=num_results))
                    source = "news"
                else:
                    raw_results = list(ddgs.text(query, max_results=num_results))
                    source = "web"
            results = [self._normalize_result(result, source) for result in raw_results]
            results = [result for result in results if result["title"] or result["url"]]
            if results:
                return results[:num_results]
        except Exception as e:
            print(f"  [WebSearch] DDGS search failed: {e}")

        return [
            self._normalize_result(result, "web")
            for result in self._html_search(query, num_results)
        ]

    def _html_search(self, query: str, num_results: int) -> List[Dict[str, Any]]:
        results = []
        for provider, search in (
            ("DuckDuckGo", self._duckduckgo_search),
            ("Google", self._google_search_fast),
            ("DuckDuckGo Lite", self._duckduckgo_lite),
        ):
            try:
                results = search(query)
            except Exception as e:
                print(f"  [WebSearch] {provider} fallback failed: {e}")
            if results:
                break
        return results[:num_results]

    @staticmethod
    def _format_results(query: str, results: List[Dict[str, Any]]) -> str:
        if not results:
            return ""

        lines = [f"🔍 Kết quả tìm kiếm: \"{query}\"\n"]
        for i, result in enumerate(results, 1):
            lines.append(f"{i}. {result['title'] or 'No title'}")
            if result["snippet"]:
                lines.append(f"   Snippet: {result['snippet']}")
            if result["url"]:
                lines.append(f"   URL: {result['url']}")
            if result["published_date"]:
                lines.append(f"   Published: {result['published_date']}")
            if result["source"]:
                lines.append(f"   Source: {result['source']}")
            lines.append("")
        return "\n".join(lines)

    def _duckduckgo_search(self, query: str) -> list:
        """DuckDuckGo HTML search — most reliable for bots."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "b": ""},
            headers=headers,
            timeout=8,
        )
        resp.raise_for_status()

        results = []
        html = resp.text

        # DuckDuckGo HTML uses class="result__a" for titles
        title_pattern = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
        snippet_pattern = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)

        titles_and_links = title_pattern.findall(html)
        snippets = snippet_pattern.findall(html)

        for i, (link, title_html) in enumerate(titles_and_links[:8]):
            title = re.sub(r'<[^>]+>', '', title_html).strip()
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
            
            # Clean DuckDuckGo redirect URL
            clean_link = link
            if "uddg=" in link:
                import urllib.parse
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
                clean_link = parsed.get("uddg", [link])[0]

            if title:
                results.append({
                    "title": title,
                    "snippet": snippet,
                    "link": clean_link,
                })

        return results

    def _google_search_fast(self, query: str) -> list:
        """Google search with short timeout."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        url = "https://www.google.com/search"
        params = {"q": query, "num": 8, "hl": "vi"}

        resp = requests.get(url, params=params, headers=headers, timeout=8)
        resp.raise_for_status()

        html = resp.text
        results = self._parse_google_html(html)
        return results

    def _parse_google_html(self, html: str) -> list:
        """Parse Google search results from HTML."""
        results = []

        # Pattern: <a href="URL">...<h3>Title</h3>
        h3_pattern = re.compile(
            r'<a[^>]+href="(/url\?q=([^"&]+)[^"]*|https?://[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>',
            re.DOTALL
        )

        for match in h3_pattern.finditer(html):
            raw_url = match.group(1)
            title = re.sub(r'<[^>]+>', '', match.group(3)).strip()

            link = raw_url
            if link.startswith("/url?q="):
                link = match.group(2)
            if "google.com" in link and "/search" in link:
                continue
            if not title:
                continue

            results.append({"title": title, "link": link, "snippet": ""})

        # Extract snippets
        snippet_pattern = re.compile(
            r'<span[^>]*class="[^"]*(?:st|IsZvec|VwiC3b|yXK7lf)[^"]*"[^>]*>(.*?)</span>',
            re.DOTALL
        )
        snippets = []
        for match in snippet_pattern.finditer(html):
            text = re.sub(r'<[^>]+>', '', match.group(1)).strip()
            if len(text) > 30:
                snippets.append(text)

        if len(snippets) < len(results):
            broad_pattern = re.compile(
                r'<div[^>]*class="[^"]*(?:VwiC3b|IsZvec|s3v9rd)[^"]*"[^>]*>(.*?)</div>',
                re.DOTALL
            )
            for match in broad_pattern.finditer(html):
                text = re.sub(r'<[^>]+>', '', match.group(1)).strip()
                if len(text) > 30:
                    snippets.append(text)

        for i, r in enumerate(results):
            if i < len(snippets):
                r["snippet"] = snippets[i]

        if not results:
            results = self._fallback_parse(html)

        return results

    def _fallback_parse(self, html: str) -> list:
        """Simpler fallback parsing for Google HTML."""
        results = []
        h3s = re.findall(r'<h3[^>]*>(.*?)</h3>', html, re.DOTALL)
        for h3 in h3s:
            title = re.sub(r'<[^>]+>', '', h3).strip()
            if title and len(title) > 5 and "Google" not in title:
                results.append({"title": title, "link": "", "snippet": ""})
        return results[:8]

    def _duckduckgo_lite(self, query: str) -> list:
        """DuckDuckGo Lite — ultra-minimal, fastest fallback."""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            resp = requests.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers=headers,
                timeout=6,
            )
            results = []
            # Lite version has simpler HTML - extract links and titles
            link_pattern = re.compile(
                r'<a[^>]*rel="nofollow"[^>]*href="([^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>',
                re.DOTALL
            )
            for match in link_pattern.finditer(resp.text):
                link = match.group(1)
                title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
                if title and link:
                    results.append({"title": title, "link": link, "snippet": ""})

            # Also try simple td-based extraction (lite format)
            if not results:
                td_pattern = re.compile(
                    r'<td[^>]*>\s*\d+\.?\s*</td>\s*<td[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    re.DOTALL
                )
                for match in td_pattern.finditer(resp.text):
                    link = match.group(1)
                    title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
                    if title and "duckduckgo" not in link.lower():
                        results.append({"title": title, "link": link, "snippet": ""})

            return results[:8]
        except Exception:
            return []

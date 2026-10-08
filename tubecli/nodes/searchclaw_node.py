"""Optional SearchClaw research node with the existing DDGS search as fallback."""
import logging
import os
from typing import Any, Dict
from urllib.parse import urlparse

import requests

from tubecli.nodes.base_node import BaseNode, PortType
from tubecli.nodes.web_search_node import WebSearchNode

logger = logging.getLogger(__name__)


class SearchClawResearchNode(BaseNode):
    node_type = "searchclaw_research"
    display_name = "🔬 SearchClaw Research"
    description = "Deep research via SearchClaw, falling back to TubeCLI's existing DDGS search."
    category = "Network"
    config_schema = {
        "base_url": {
            "type": "string",
            "description": "SearchClaw service URL; defaults to TUBECLI_SEARCHCLAW_URL or localhost:8001.",
        },
    }

    def _setup_ports(self):
        self.add_input("query", PortType.TEXT, "Research query")
        self.add_output("results", PortType.TEXT, "Research answer and source evidence")
        self.add_output("research_answer", PortType.TEXT, "SearchClaw's synthesized answer")
        self.add_output("structured_results", PortType.JSON, "Normalized source and citation data")
        self.add_output("citations", PortType.JSON, "Original SearchClaw citation objects")
        self.add_output("backend", PortType.TEXT, "Research backend used")
        self.add_output("status", PortType.TEXT, "Execution status")

    def _base_url(self) -> str:
        configured = self.config.get("base_url", "")
        return str(
            configured
            or os.environ.get("TUBECLI_SEARCHCLAW_URL")
            or os.environ.get("SEARCHCLAW_URL")
            or "http://127.0.0.1:8001"
        ).rstrip("/")

    @staticmethod
    def _format_research(answer: str, sources: list) -> str:
        lines = ["SearchClaw research answer:", answer.strip(), "", "Sources:"]
        for index, source in enumerate(sources, 1):
            lines.append(f"{index}. {source.get('title') or 'Untitled source'}")
            if source.get("source"):
                lines.append(f"   Source: {source['source']}")
            lines.append(f"   Date: {source.get('date') or 'not available'}")
            if source.get("url"):
                lines.append(f"   URL: {source['url']}")
            if source.get("snippet"):
                lines.append(f"   Summary: {source['snippet']}")
        return "\n".join(lines)

    async def execute(self, inputs: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        query = str(
            inputs.get("query")
            or inputs.get("prompt")
            or inputs.get("text")
            or self.config.get("query", "")
        ).strip()
        if not query:
            return {
                "results": "",
                "research_answer": "",
                "structured_results": [],
                "citations": [],
                "backend": "",
                "status": "Error: No research query provided",
            }

        url = f"{self._base_url()}/api/query"
        service_host = urlparse(url).hostname
        api_key = (
            os.environ.get("TUBECLI_SEARCHCLAW_API_KEY")
            or os.environ.get("SEARCH_CLAW_API_KEY")
            or ""
        )
        headers = {"Content-Type": "application/json"}
        request_options: Dict[str, Any] = {}
        if service_host in {"localhost", "127.0.0.1", "::1"}:
            request_options["proxies"] = {"http": "", "https": ""}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            response = requests.post(
                url,
                json={"query": query, "max_turns": 20, "max_search": 12, "max_fetch": 12},
                headers=headers,
                timeout=(5, 600),
                **request_options,
            )
            if response.status_code >= 400:
                logger.warning("SearchClaw request failed with HTTP %s; using DDGS fallback", response.status_code)
                return await self._fallback(query)
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("answer"), str):
                logger.warning("SearchClaw returned an invalid research response; using DDGS fallback")
                return await self._fallback(query)
        except requests.RequestException as exc:
            logger.warning("SearchClaw request failed (%s); using DDGS fallback", type(exc).__name__)
            return await self._fallback(query)
        except ValueError:
            logger.warning("SearchClaw returned invalid JSON; using DDGS fallback")
            return await self._fallback(query)

        answer = payload["answer"].strip()
        if not answer:
            logger.warning("SearchClaw returned an empty answer; using DDGS fallback")
            return await self._fallback(query)

        citations = payload.get("citations")
        if not isinstance(citations, list):
            citations = []
        sources = []
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            source_url = citation.get("url") or citation.get("href") or citation.get("link") or ""
            parsed_url = urlparse(source_url)
            source = (
                citation.get("source")
                or citation.get("domain")
                or parsed_url.hostname
                or citation.get("source_type")
                or ""
            )
            date = (
                citation.get("published_date")
                or citation.get("date")
                or citation.get("published_at")
                or citation.get("published")
            )
            sources.append({
                **citation,
                "title": citation.get("title") or "",
                "source": source,
                "url": source_url,
                "date": date or "",
                "snippet": citation.get("snippet") or citation.get("summary") or "",
            })

        return {
            "results": self._format_research(answer, sources),
            "research_answer": answer,
            "structured_results": sources,
            "citations": citations,
            "backend": "searchclaw",
            "status": f"SearchClaw research completed with {len(sources)} citations",
        }

    async def _fallback(self, query: str) -> Dict[str, Any]:
        result = await WebSearchNode().execute({"query": query})
        return {
            "results": result.get("results", ""),
            "research_answer": "",
            "structured_results": result.get("structured_results", []),
            "citations": [],
            "backend": "ddgs",
            "status": f"SearchClaw unavailable; existing web search fallback: {result.get('status', 'completed')}",
        }

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import requests

from tubecli.nodes.searchclaw_node import SearchClawResearchNode


class SearchClawResearchNodeTest(unittest.TestCase):
    def test_searchclaw_success_and_ddgs_fallback(self):
        query = "Research recent AI developments"
        citation = {
            "title": "Example AI announcement",
            "url": "https://example.com/ai",
            "snippet": "The source describes an AI announcement.",
            "source_type": "web",
            "published_date": "2026-10-07",
        }

        class SearchResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"answer": "A cited research summary.", "citations": [citation]}

        with patch.dict("os.environ", {"TUBECLI_SEARCHCLAW_API_KEY": "test-secret"}):
            with patch("tubecli.nodes.searchclaw_node.requests.post", return_value=SearchResponse()) as post:
                result = asyncio.run(SearchClawResearchNode().execute({"query": query}))

        self.assertEqual(result["backend"], "searchclaw")
        self.assertEqual(result["research_answer"], "A cited research summary.")
        self.assertEqual(result["structured_results"][0]["title"], citation["title"])
        self.assertEqual(result["structured_results"][0]["source"], "example.com")
        self.assertEqual(result["structured_results"][0]["date"], "2026-10-07")
        self.assertEqual(result["structured_results"][0]["url"], citation["url"])
        self.assertEqual(result["citations"], [citation])
        self.assertIn("A cited research summary.", result["results"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-secret")

        with patch("tubecli.nodes.searchclaw_node.requests.post", side_effect=requests.ConnectionError()):
            with patch(
                "tubecli.nodes.web_search_node.WebSearchNode.execute",
                new=AsyncMock(return_value={
                    "results": "DDGS source evidence",
                    "structured_results": [{
                        "title": "DDGS result",
                        "url": "https://ddgs.example/result",
                        "published_date": None,
                        "snippet": "Fallback snippet",
                        "source": "ddgs.example",
                    }],
                    "status": "Found results",
                }),
            ):
                fallback = asyncio.run(SearchClawResearchNode().execute({"query": query}))

        self.assertEqual(fallback["backend"], "ddgs")
        self.assertIn("DDGS source evidence", fallback["results"])
        self.assertEqual(fallback["structured_results"][0]["title"], "DDGS result")
        self.assertEqual(fallback["citations"], [])


if __name__ == "__main__":
    unittest.main()

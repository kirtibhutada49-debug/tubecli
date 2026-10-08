import json
import re
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
REGISTRY = DATA_DIR / "company_registry.json"
EVIDENCE = DATA_DIR / "capability_evidence.index.json"
BINDINGS = DATA_DIR / "capability_bindings.json"

company_registry = {}
agents = {}
capability_bindings = {}
_data_loaded = False


def _load_data():
    global company_registry, agents, capability_bindings, _data_loaded
    if _data_loaded:
        return

    with REGISTRY.open(encoding="utf-8") as f:
        company_registry = json.load(f)
    with EVIDENCE.open(encoding="utf-8") as f:
        evidence = json.load(f)
    with BINDINGS.open(encoding="utf-8") as f:
        bindings = json.load(f)

    agents = {a["name"]: a for a in evidence["agents"]}
    capability_bindings = bindings.get("capabilities", {})
    _data_loaded = True


def norm(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def agent_evidence(agent):
    parts = []

    for section in agent.get("headings") or []:
        if isinstance(section, dict):
            parts.append(str(section.get("heading", "")))
            parts.append(str(section.get("content", "")))
        else:
            parts.append(str(section))

    for bullet in agent.get("explicit_bullets") or []:
        parts.append(str(bullet))

    return norm(" ".join(parts))


def _company_name_values(value):
    name_keys = {
        "name", "company", "company_name", "business", "business_name",
        "brand", "brand_name", "aliases",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in name_keys:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, list):
                    yield from (entry for entry in item if isinstance(entry, str))
            yield from _company_name_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _company_name_values(item)


def _company_for(task):
    task_text = norm(task)
    for name in _company_name_values(company_registry):
        normalized = norm(name)
        if normalized and normalized in task_text:
            return name
    return None


# Explicit capability contracts.
# preferred_agents are accepted only when the source evidence also
# contains at least one required evidence phrase.
CONTRACTS = {
    "web_design": {
        "phrases": ["web design", "website", "interface design", "landing page"],
        "preferred_agents": [
            "Web Agent",
            "UI Designer",
            "UX Architect",
        ],
    },
    "ux_design": {
        "phrases": ["user experience", "ux architecture", "interaction design"],
        "preferred_agents": [
            "UX Architect",
            "UX Researcher",
            "UI Designer",
        ],
    },
    "frontend_engineering": {
        "phrases": ["frontend developer", "front end", "frontend", "web application"],
        "preferred_agents": [
            "Frontend Developer",
            "Senior Developer",
        ],
    },
    "seo": {
        "phrases": ["seo", "search engine optimization"],
        "preferred_agents": [
            "SEO Specialist",
            "Content Creator",
        ],
    },
    "web_qa": {
        "phrases": ["accessibility", "performance testing", "quality assurance", "testing"],
        "preferred_agents": [
            "Accessibility Auditor",
            "Performance Benchmarker",
            "Reality Checker",
        ],
    },
    "research": {
        "phrases": ["research methodology", "deep research", "research"],
        "preferred_agents": [
            "Research Agent",
            "Researcher",
            "Trend Researcher",
        ],
    },
    "competitive_analysis": {
        "phrases": ["competitive analysis", "competitor analysis", "competitive signal"],
        "preferred_agents": [
            "Research Agent",
            "Ad Creative Strategist",
            "Trend Researcher",
        ],
    },
    "market_analysis": {
        "phrases": ["market analysis", "market research"],
        "preferred_agents": [
            "Research Agent",
            "Trend Researcher",
        ],
    },
    "evidence_synthesis": {
        "phrases": ["evidence", "synthesis", "research findings"],
        "preferred_agents": [
            "Research Agent",
        ],
    },
    "paid_media_strategy": {
        "phrases": ["paid media strategy", "paid social", "paid advertising"],
        "preferred_agents": [
            "Paid Social Strategist",
            "PPC Campaign Strategist",
        ],
    },
    "meta_creative_strategy": {
        "phrases": ["meta creative strategy", "meta", "facebook", "instagram"],
        "preferred_agents": [
            "Ad Creative Strategist",
        ],
    },
    "creative_testing": {
        "phrases": ["creative testing", "creative test", "a/b testing"],
        "preferred_agents": [
            "Ad Creative Strategist",
            "Tracking & Measurement Specialist",
        ],
    },
    "campaign_optimization": {
        "phrases": [
            "campaign optimization",
            "paid social optimization",
            "campaign performance optimization",
            "creative optimization",
        ],
        "preferred_agents": [
            "Paid Social Strategist",
            "PPC Campaign Strategist",
            "Paid Media Auditor",
            "Tracking & Measurement Specialist",
        ],
        "allow_fallback": False,
    },
    "content_strategy": {
        "phrases": ["content strategy", "editorial calendar", "content planning"],
        "preferred_agents": [
            "Content Creator",
            "Social Media Strategist",
        ],
    },
    "scriptwriting": {
        "phrases": ["video scripts", "scriptwriting", "scripting"],
        "preferred_agents": [
            "Content Creator",
        ],
    },
    "visual_storytelling": {
        "phrases": ["visual storytelling", "storyboards", "narrative development"],
        "preferred_agents": [
            "Visual Storyteller",
        ],
    },
    "video_production": {
        "phrases": ["video production", "video content", "multimedia content"],
        "preferred_agents": [
            "Video Agent",
            "Content Creator",
            "Visual Storyteller",
        ],
    },
    "video_editing": {
        "phrases": ["video editing", "post production", "editing workflow"],
        "preferred_agents": [
            "Video Agent",
            "Short-Video Editing Coach",
        ],
    },
    "youtube_distribution": {
        "phrases": ["youtube", "content distribution"],
        "preferred_agents": [
            "Video Agent",
            "Social Media Strategist",
            "Content Creator",
        ],
    },
    "content_analytics": {
        "phrases": ["content analytics", "performance analysis", "roi measurement"],
        "preferred_agents": [
            "Content Creator",
            "Tracking & Measurement Specialist",
        ],
    },
    "product_strategy": {
        "phrases": ["product strategy", "product management", "product decisions"],
        "preferred_agents": [
            "Product Manager",
        ],
    },
    "marketing_strategy": {
        "phrases": ["marketing strategy", "marketing strategy"],
        "preferred_agents": [
            "Chief Marketing Officer",
            "Content Creator",
        ],
    },
    "sales_strategy": {
        "phrases": ["sales strategy", "demand generation"],
        "preferred_agents": [
            "VP of Sales",
            "Sales Coach",
            "Account Strategist",
        ],
    },
    "lead_generation": {
        "phrases": [
            "lead generation",
            "prospecting",
            "demand generation",
            "qualified leads",
        ],
        "preferred_agents": [
            "Outbound Strategist",
            "Pipeline Analyst",
            "Account Strategist",
        ],
        "allow_fallback": False,
    },
    "sales_outreach": {
        "phrases": [
            "sales outreach",
            "outreach",
            "prospecting",
        ],
        "preferred_agents": [
            "Outbound Strategist",
            "Account Strategist",
        ],
        "allow_fallback": False,
    },
    "crm": {
        "phrases": [
            "crm",
            "pipeline",
            "customer relationship",
        ],
        "preferred_agents": [
            "Pipeline Analyst",
            "Account Strategist",
        ],
        "allow_fallback": False,
    },
}

WORKFLOWS = {
    "website": {
        "signals": ["website", "web site", "landing page", "react", "3d website"],
        "capabilities": [
            "web_design",
            "ux_design",
            "frontend_engineering",
            "seo",
            "web_qa",
        ],
    },
    "meta_ads": {
        "signals": ["meta ads", "facebook ads", "instagram ads", "paid social"],
        "capabilities": [
            "paid_media_strategy",
            "meta_creative_strategy",
            "creative_testing",
            "campaign_optimization",
        ],
    },
    "lead_generation": {
        "signals": [
            "whatsapp lead",
            "whatsapp leads",
            "whatsapp lead system",
            "lead system",
            "lead generation",
            "qualified leads",
            "crm",
        ],
        "capabilities": [
            "lead_generation",
            "sales_outreach",
            "crm",
        ],
    },
    "deep_research": {
        "signals": [
            "deep research",
            "market research",
            "competitor research",
            "market study",
        ],
        "capabilities": [
            "research",
            "competitive_analysis",
            "market_analysis",
            "evidence_synthesis",
        ],
    },
    "kids_youtube": {
        "signals": [
            "kids youtube",
            "kids channel",
            "youtube automation",
            "kids content",
        ],
        "capabilities": [
            "content_strategy",
            "scriptwriting",
            "visual_storytelling",
            "video_production",
            "video_editing",
            "youtube_distribution",
            "content_analytics",
        ],
    },
}


def workflow_matches(task):
    t = norm(task)
    found = []

    for name, workflow in WORKFLOWS.items():
        hits = [s for s in workflow["signals"] if s in t]
        if hits:
            found.append((len(hits), name, hits))

    return sorted(found, key=lambda x: (-x[0], x[1]))


def resolve(capability):
    contract = CONTRACTS.get(capability)

    if not contract:
        return {
            "capability": capability,
            "status": "UNRESOLVED",
            "agents": [],
        }

    candidates = []

    for preferred in contract["preferred_agents"]:
        agent = agents.get(preferred)
        if not agent:
            continue

        text = agent_evidence(agent)
        matched = [
            p for p in contract["phrases"]
            if norm(p) in text
        ]

        if matched:
            candidates.append({
                "name": preferred,
                "evidence_matches": matched,
                "score": len(matched) + 10,
            })

    # Optional fallback. Some capabilities are context-locked and
    # must never drift to unrelated specialists.
    if not candidates and contract.get("allow_fallback", True):
        for name, agent in agents.items():
            text = agent_evidence(agent)

            matched = [
                p for p in contract["phrases"]
                if norm(p) in text
            ]

            if len(matched) >= 2:
                candidates.append({
                    "name": name,
                    "evidence_matches": matched,
                    "score": len(matched),
                })

    candidates.sort(
        key=lambda x: (-x["score"], x["name"])
    )

    if not candidates:
        return {
            "capability": capability,
            "status": "REVIEW",
            "agents": [],
        }

    return {
        "capability": capability,
        "status": "PASS",
        "agents": [
            x["name"]
            for x in candidates[:3]
        ],
        "evidence": [
            {
                "agent": x["name"],
                "matches": x["evidence_matches"],
            }
            for x in candidates[:3]
        ],
    }


def plan(task):
    workflows = workflow_matches(task)

    if not workflows:
        return {
            "status": "REVIEW",
            "reason": "No controlled workflow matched.",
            "execution_allowed": False,
        }

    _load_data()
    company = _company_for(task)

    capabilities = []
    seen = set()

    for _, workflow, _ in workflows:
        for capability in WORKFLOWS[workflow]["capabilities"]:
            if capability not in seen:
                seen.add(capability)
                capabilities.append(capability)

    resolved = [
        resolve(c)
        for c in capabilities
    ]

    executable = []
    planning_only = []
    review = []

    for item in resolved:
        capability = item["capability"]
        binding = capability_bindings.get(capability)
        if not isinstance(binding, dict):
            review.append(capability)
            continue

        binding_status = str(binding.get("status") or "").strip().upper()
        item["binding_status"] = binding_status
        raw_skill_ids = binding.get("skill_ids")
        if raw_skill_ids is None and binding.get("skill_id"):
            raw_skill_ids = [binding["skill_id"]]
        if isinstance(raw_skill_ids, str):
            raw_skill_ids = [raw_skill_ids]
        item["skill_ids"] = list(dict.fromkeys(
            str(skill_id).strip()
            for skill_id in raw_skill_ids
            if isinstance(skill_id, str) and skill_id.strip()
        )) if isinstance(raw_skill_ids, list) else []

        if item["status"] != "PASS":
            review.append(capability)
        elif binding_status == "READY" and item["skill_ids"]:
            executable.append(item)
        elif binding_status in {"PLAN_ONLY", "PLANNING_ONLY", "NOT_READY"}:
            planning_only.append(capability)
        else:
            review.append(capability)

    if executable and (planning_only or review):
        final_status = "MIXED"
    elif executable:
        final_status = "READY"
    elif planning_only and not review:
        final_status = "PLAN_ONLY"
    else:
        final_status = "REVIEW"

    return {
        "status": final_status,
        "task": task,
        "company": company,
        "workflows": [
            {
                "name": name,
                "signals": hits,
            }
            for _, name, hits in workflows
        ],
        "capabilities": resolved,
        "execution_capabilities": executable,
        "planning_only_capabilities": planning_only,
        "review_capabilities": review,
        "execution_allowed": bool(executable),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python company_router.py "task"')
        raise SystemExit(1)

    print(json.dumps(
        plan(" ".join(sys.argv[1:])),
        indent=2,
        ensure_ascii=False,
    ))

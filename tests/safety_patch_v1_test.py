import unittest

from tubecli.extensions.multi_agents.extension import AgentTeam, TeamNode


class AgentHierarchySafetyTests(unittest.TestCase):
    def test_valid_hierarchy_has_one_root(self):
        team = AgentTeam(
            name="valid",
            nodes=[
                {
                    "role_id": "root",
                    "role": "Lead",
                    "children": ["child"],
                    "parent": None,
                },
                {
                    "role_id": "child",
                    "role": "Worker",
                    "children": [],
                    "parent": "root",
                },
            ],
        )
        self.assertEqual(
            [node.role_id for node in team.get_root_nodes()],
            ["root"],
        )

    def test_org_chart_handles_no_children(self):
        team = AgentTeam(
            name="single",
            nodes=[TeamNode(role_id="root", role="Lead")],
        )
        chart = team.get_org_chart()
        self.assertEqual(len(chart), 1)
        self.assertEqual(chart[0]["role_id"], "root")
        self.assertEqual(chart[0]["children_nodes"], [])


    def test_rejects_circular_hierarchy(self):
        team = AgentTeam(
            name="cycle",
            nodes=[
                {"role_id": "a", "children": ["b"], "parent": "b"},
                {"role_id": "b", "children": ["a"], "parent": "a"},
            ],
        )
        with self.assertRaises(ValueError):
            team.validate_hierarchy()



    def test_hierarchy_depth_limit_is_configurable(self):
        import os
        from unittest.mock import patch

        team = AgentTeam(
            name="deep",
            nodes=[
                {"role_id": "a", "children": ["b"], "parent": None},
                {"role_id": "b", "children": ["c"], "parent": "a"},
                {"role_id": "c", "children": [], "parent": "b"},
            ],
        )
        with patch.dict(os.environ, {"TUBECLI_MAX_AGENT_DEPTH": "1"}):
            with self.assertRaises(ValueError):
                team.validate_hierarchy()

    def test_org_chart_rejects_invalid_hierarchy(self):
        team = AgentTeam(
            name="cycle",
            nodes=[
                {"role_id": "a", "children": ["b"], "parent": "b"},
                {"role_id": "b", "children": ["a"], "parent": "a"},
            ],
        )
        with self.assertRaises(ValueError):
            team.get_org_chart()



    def test_agent_call_budget_configuration_is_positive(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"TUBECLI_MAX_AGENT_CALLS": "0"}):
            with self.assertRaises(ValueError):
                AgentTeam(name="budget").validate_call_budget()



    def test_agent_call_budget_rejects_invalid_integer(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"TUBECLI_MAX_AGENT_CALLS": "invalid"}):
            with self.assertRaises(ValueError):
                AgentTeam(name="budget").validate_call_budget()

    def test_loop_limit_configuration_must_be_positive(self):
        import os
        from unittest.mock import patch
        from tubecli.core.workflow_engine import WorkflowEngine

        with patch.dict(os.environ, {"TUBECLI_MAX_LOOP_ITEMS": "0"}):
            with self.assertRaises(ValueError):
                WorkflowEngine([], []).validate_loop_limit()



    def test_loop_limit_excess_reports_error_without_processing_items(self):
        import asyncio
        import os
        from unittest.mock import patch
        from tubecli.core.workflow_engine import WorkflowEngine
        from tubecli.nodes.registry import NodePolicy, create_node_from_dict

        loop = create_node_from_dict(
            {"id": "loop1", "type": "loop", "label": "Test Loop",
             "config": {"items": ["one", "two", "three", "four"]}},
            policy=NodePolicy.user("test.safety_patch_v1"),
        )
        engine = WorkflowEngine([loop], [])
        # The loop reads items from its input ports, not from node config.
        engine._get_node_inputs = lambda node_id: {
            "items": ["one", "two", "three", "four"]
        }

        with patch.dict(os.environ, {"TUBECLI_MAX_LOOP_ITEMS": "2"}):
            result = asyncio.run(engine.run())

        self.assertTrue(result.get("has_errors"))
        self.assertTrue(
            any("safety limit exceeded" in log.get("message", "").lower()
                for log in result.get("logs", []))
        )



    def test_agent_call_budget_stops_extra_calls(self):
        import asyncio
        import os
        from unittest.mock import patch, MagicMock
        from tubecli.extensions.multi_agents.extension import Orchestrator, AgentTeam

        orchestrator = Orchestrator()
        team = AgentTeam(
            name="budget-test",
            agent_ids=["a1", "a2", "a3"],
            strategy="sequential",
        )
        orchestrator._teams[team.id] = team

        fake_agents = {
            agent_id: MagicMock(
                id=agent_id,
                name=agent_id,
                description="test",
                to_dict=lambda aid=agent_id: {"id": aid},
            )
            for agent_id in team.agent_ids
        }

        with patch.dict(os.environ, {"TUBECLI_MAX_AGENT_CALLS": "2"}), \
             patch("tubecli.core.agent.agent_manager.get", side_effect=fake_agents.get), \
             patch("tubecli.core.skill.skill_manager.get_all", return_value=[]), \
             patch("tubecli.core.brain.AgentBrain.chat", return_value={"reply": "ok"}) as chat, \
             patch.object(orchestrator, "_save"):
            result = asyncio.run(orchestrator.delegate(team.id, "test task"))

        self.assertLessEqual(chat.call_count, 2)
        self.assertEqual(result.get("status"), "error")


    def test_hierarchy_agent_call_budget_stops_extra_calls(self):
        import asyncio
        import os
        from unittest.mock import MagicMock, patch
        from tubecli.extensions.multi_agents.extension import Orchestrator, AgentTeam

        team = AgentTeam(
            name="hierarchy-budget",
            strategy="hierarchy",
            nodes=[
                {"role_id": "root", "role": "Lead", "agent_id": "a1",
                 "children": ["child"], "parent": None},
                {"role_id": "child", "role": "Worker", "agent_id": "a2",
                 "children": ["grandchild"], "parent": "root"},
                {"role_id": "grandchild", "role": "Worker 2", "agent_id": "a3",
                 "children": [], "parent": "child"},
            ],
        )
        orchestrator = Orchestrator()
        agents = {
            agent_id: MagicMock(
                id=agent_id,
                name=agent_id,
                description="test",
                to_dict=lambda aid=agent_id: {"id": aid},
            )
            for agent_id in ("a1", "a2", "a3")
        }
        orchestrator._teams[team.id] = team

        with patch.dict(os.environ, {"TUBECLI_MAX_AGENT_CALLS": "2"}), \
             patch("tubecli.core.agent.agent_manager.get", side_effect=agents.get), \
             patch("tubecli.core.skill.skill_manager.get_all", return_value=[]), \
             patch("tubecli.core.brain.AgentBrain.chat", return_value={"reply": "ok"}) as chat, \
             patch.object(orchestrator, "_save"):
            result = asyncio.run(orchestrator.delegate(team.id, "test task"))

        self.assertEqual(chat.call_count, 2)
        self.assertEqual(result.get("status"), "error")
        self.assertIn("safety limit exceeded", result.get("message", "").lower())


if __name__ == "__main__":
    unittest.main()

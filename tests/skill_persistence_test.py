import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tubecli.core.skill import SkillManager
from tubecli.core.skill_importer import import_skill_from_file


class SkillPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.skills_file = self.root / "skills.json"
        self.data_patch = patch("tubecli.core.skill_importer.DATA_DIR", self.root / "data")
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def test_independent_managers_merge_mutations_without_losing_skills(self):
        original = SkillManager(self.skills_file)
        original.create(name="Existing Custom", description="Keep this skill")

        manager_a = SkillManager(self.skills_file)
        manager_b = SkillManager(self.skills_file)
        manager_a.create(name="Skill A", description="Created by A")
        manager_b.create(name="Skill B", description="Created by B")
        manager_a.update(
            manager_a.find_by_name("Skill A").id,
            description="Updated by A after B's save",
        )

        persisted = json.loads(self.skills_file.read_text(encoding="utf-8"))
        by_name = {skill["name"]: skill for skill in persisted}
        self.assertEqual(
            set(by_name),
            {"Existing Custom", "Skill A", "Skill B"},
        )
        self.assertEqual(by_name["Skill A"]["description"], "Updated by A after B's save")
        self.assertEqual(by_name["Skill B"]["description"], "Created by B")

    def test_stale_manager_delete_preserves_skills_created_by_another_manager(self):
        manager_a = SkillManager(self.skills_file)
        existing = manager_a.create(name="Delete Me", description="Remove this record")
        manager_b = SkillManager(self.skills_file)

        manager_a.create(name="Keep Me", description="Created after manager B loaded")
        self.assertTrue(manager_b.delete(existing.id))

        persisted = json.loads(self.skills_file.read_text(encoding="utf-8"))
        self.assertEqual([skill["name"] for skill in persisted], ["Keep Me"])

    def test_import_persists_and_fresh_manager_reloads_it(self):
        source = self.root / "source"
        source.mkdir()
        skill_file = source / "SKILL.md"
        skill_file.write_text(
            "---\nname: Imported SOP\ndescription: Imported offline\n---\n"
            "Keep this text as markdown.\n",
            encoding="utf-8",
        )

        manager = SkillManager(self.skills_file)
        imported = import_skill_from_file(str(skill_file), manager=manager)

        persisted = json.loads(self.skills_file.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["id"], imported.id)
        self.assertEqual(persisted[0]["skill_format"], "markdown")

        fresh_manager = SkillManager(self.skills_file)
        reloaded = fresh_manager.get(imported.id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.name, "Imported SOP")
        self.assertEqual(reloaded.workflow_data["markdown_content"], "Keep this text as markdown.\n")

    def test_api_style_read_refreshes_persisted_imported_skill(self):
        source = self.root / "source"
        source.mkdir()
        skill_file = source / "SKILL.md"
        skill_file.write_text(
            "---\nname: API Visible SOP\ndescription: Visible after another manager saves\n---\n"
            "Static instructions only.\n",
            encoding="utf-8",
        )

        importing_manager = SkillManager(self.skills_file)
        api_manager = SkillManager(self.skills_file)
        imported = import_skill_from_file(str(skill_file), manager=importing_manager)

        # GET /api/v1/skills reads skill_manager.get_all(); exercise that same
        # manager read path without importing the API app or starting extensions.
        api_records = [skill.to_dict() for skill in api_manager.get_all()]
        visible = [record for record in api_records if record["id"] == imported.id]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["name"], "API Visible SOP")
        self.assertEqual(visible[0]["skill_format"], "markdown")


if __name__ == "__main__":
    unittest.main()

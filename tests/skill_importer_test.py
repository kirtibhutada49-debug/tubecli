import tempfile
import unittest
import sys
import asyncio
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tubecli.core.skill import Skill
from tubecli.core.skill_importer import (
    FreeOnlyInferenceError,
    SkillImportError,
    call_imported_skill_free_model,
    import_skill_from_file,
    is_explicitly_free_model,
)


class MemorySkillManager:
    def __init__(self, skills=None):
        self.skills = list(skills or [])

    def get_all(self):
        return list(self.skills)

    def get(self, skill_id):
        return next((skill for skill in self.skills if skill.id == skill_id), None)

    def create(self, **kwargs):
        skill = Skill(**kwargs)
        self.skills.append(skill)
        return skill


def write_skill(directory, body, frontmatter=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    if frontmatter is None:
        path.write_text(body, encoding="utf-8")
    else:
        path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


class SkillImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.data_patch = patch("tubecli.core.skill_importer.DATA_DIR", self.data_dir)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def test_valid_skill_converts_with_body_and_supported_metadata(self):
        path = write_skill(
            self.root / "source",
            "Follow these steps carefully.\n",
            'name: Text Guide\ndescription: A safe reference guide\n'
            'license: MIT\nmetadata:\n  when_to_use: When a user asks for a guide\n'
            '  input_hint: A topic\n  examples: Explain this topic',
        )
        manager = MemorySkillManager()

        skill = import_skill_from_file(str(path), manager=manager)

        self.assertEqual(skill.name, "Text Guide")
        self.assertEqual(skill.description, "A safe reference guide")
        self.assertEqual(skill.skill_type, "Markdown")
        self.assertEqual(skill.skill_format, "markdown")
        self.assertEqual(skill.commands, [])
        self.assertEqual(skill.workflow_data["markdown_content"], "Follow these steps carefully.\n")
        self.assertEqual(skill.workflow_data["source_metadata"]["license"], "MIT")
        self.assertTrue(skill.workflow_data["_tubecli_importer"]["free_only"])
        self.assertIs(manager.get(skill.id), skill)

    def test_missing_frontmatter_rejected(self):
        path = write_skill(self.root, "# No frontmatter\n")
        with self.assertRaisesRegex(SkillImportError, "frontmatter"):
            import_skill_from_file(str(path), manager=MemorySkillManager())

    def test_invalid_metadata_rejected(self):
        cases = (
            "name: [invalid\ndescription: Invalid YAML",
            "name: Missing Description",
            "name: 3\ndescription: Invalid name type",
            "name: Duplicate\ndescription: Test\nname: Second",
        )
        for frontmatter in cases:
            with self.subTest(frontmatter=frontmatter):
                path = write_skill(self.root, "Body\n", frontmatter)
                with self.assertRaises(SkillImportError):
                    import_skill_from_file(str(path), manager=MemorySkillManager())

    def test_duplicate_name_is_conflict_and_existing_skill_is_unchanged(self):
        original = Skill(name="Text Guide", description="Existing", commands=["keep"])
        manager = MemorySkillManager([original])
        before = original.to_dict()
        path = write_skill(self.root, "Body\n", "name: text-guide\ndescription: Another guide")

        with self.assertRaisesRegex(SkillImportError, "already registered"):
            import_skill_from_file(str(path), manager=manager)

        self.assertEqual(manager.get_all(), [original])
        self.assertEqual(original.to_dict(), before)

    def test_path_traversal_rejected(self):
        source = self.root / "source"
        path = write_skill(source, "[outside](../secret.txt)\n", "name: Traversal\ndescription: Test")
        (self.root / "secret.txt").write_text("private", encoding="utf-8")

        with self.assertRaisesRegex(SkillImportError, "traversal"):
            import_skill_from_file(str(path), manager=MemorySkillManager())

    def test_symlink_resource_cannot_escape_source_directory(self):
        source = self.root / "source"
        source.mkdir()
        outside = self.root / "private.txt"
        outside.write_text("private", encoding="utf-8")
        (source / "linked.txt").symlink_to(outside)
        path = write_skill(
            source,
            "[outside](linked.txt)\n",
            "name: Symlink Escape\ndescription: Test",
        )

        with self.assertRaisesRegex(SkillImportError, "outside"):
            import_skill_from_file(str(path), manager=MemorySkillManager())

    def test_absolute_and_external_resources_rejected(self):
        for reference in ("/etc/passwd", "C:/private.txt", "https://example.invalid/resource.txt"):
            with self.subTest(reference=reference):
                path = write_skill(
                    self.root / reference.replace("/", "_").replace(":", "_"),
                    f"[resource]({reference})\n",
                    "name: External\ndescription: Test",
                )
                with self.assertRaisesRegex(SkillImportError, "paths|External resource"):
                    import_skill_from_file(str(path), manager=MemorySkillManager())

    def test_script_resource_is_copied_but_marked_unsupported_and_inert(self):
        source_dir = self.root / "source"
        source_dir.mkdir()
        script = source_dir / "helper.py"
        script.write_text("raise RuntimeError('must remain inert')\n", encoding="utf-8")
        path = write_skill(
            source_dir,
            "Run `scripts/helper.py` only when a human executes it.\n",
            "name: Script Reference\ndescription: Inert script resource",
        )
        scripts_dir = source_dir / "scripts"
        scripts_dir.mkdir()
        script.rename(scripts_dir / script.name)
        script = scripts_dir / script.name

        skill = import_skill_from_file(str(path), manager=MemorySkillManager())

        self.assertEqual(skill.workflow_data["unsupported_scripts"], ["scripts/helper.py"])
        stored = self.data_dir / skill.workflow_data["resource_dir"] / "scripts" / "helper.py"
        self.assertEqual(stored.read_text(encoding="utf-8"), script.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(SkillImportError, "unsupported"):
            import_skill_from_file(str(path), allow_scripts=True, manager=MemorySkillManager())

    def test_paid_or_unsupported_provider_configuration_rejected(self):
        cases = (
            "name: Paid\ndescription: Requires paid inference\nprovider: openai",
            "name: Paid\ndescription: Requires paid inference\nmetadata:\n  model: gpt-4o",
        )
        for frontmatter in cases:
            with self.subTest(frontmatter=frontmatter):
                path = write_skill(self.root, "Body\n", frontmatter)
                with self.assertRaisesRegex(SkillImportError, "provider|inference"):
                    import_skill_from_file(str(path), manager=MemorySkillManager())

    def test_free_compatible_sop_imports_without_invented_commands(self):
        path = write_skill(
            self.root,
            "Plain instructions.\n",
            "name: Free Compatible\ndescription: No model config",
        )

        skill = import_skill_from_file(str(path), manager=MemorySkillManager())

        self.assertEqual(skill.commands, [])
        self.assertEqual(skill.workflow_data["markdown_content"], "Plain instructions.\n")
        self.assertTrue(skill.workflow_data["_tubecli_importer"]["free_only"])

    def test_runtime_gate_requires_explicit_free_9router_model(self):
        self.assertTrue(is_explicitly_free_model("9router", "auto:cheap"))
        rejected_routes = (
            ("openai", "gpt-4o"),
            ("9router", "some-model"),
            ("", "auto:cheap"),
            ("9router", ""),
            ("9router", "free-router"),
            ("9router", "gemini-2.5-flash"),
            ("9router", "qwen:latest"),
            ("9router", "nvidia/nemotron-3-super-120b-a12b"),
        )
        for provider, model in rejected_routes:
            with self.subTest(provider=provider, model=model):
                self.assertFalse(is_explicitly_free_model(provider, model))
                with self.assertRaisesRegex(FreeOnlyInferenceError, "No inference request was made"):
                    call_imported_skill_free_model(
                        {"provider": provider, "model": model},
                        [{"role": "user", "content": "test"}],
                    )

    def test_imported_markdown_runtime_rejects_paid_agent_without_fallback(self):
        from tubecli.core.brain import AgentBrain

        skill = {
            "name": "Imported",
            "skill_format": "markdown",
            "workflow_data": {
                "markdown_content": "Instructions",
                "_tubecli_importer": {"version": 1, "free_only": True},
            },
        }
        with patch.object(AgentBrain, "_call_llm") as paid_call:
            result = asyncio.run(
                AgentBrain.autonomous_run(
                    "Do this",
                    {"provider": "openai", "model": "gpt-4o"},
                    skill,
                )
            )

        self.assertIn("Free-only imported skill inference refused", result)
        paid_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()

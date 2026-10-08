import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tubecli.core.skill import SkillManager
from tubecli.core.skill_bundle_importer import import_skill_bundle, validate_skill_bundle


class SkillBundleImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.bundle = self.root / "bundle"
        self.skills_file = self.root / "skills.json"
        self.data_dir = self.root / "data"
        self.manager = SkillManager(self.skills_file)
        self.data_patch = patch("tubecli.core.skill_importer.DATA_DIR", self.data_dir)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.write_manifest()

    def write_manifest(self, text="name: Test Bundle\nversion: '1'\n"):
        self.bundle.mkdir(parents=True, exist_ok=True)
        (self.bundle / "manifest.yaml").write_text(text, encoding="utf-8")

    def add_skill(self, directory, name, *, description="Offline SOP", body="Plain instructions.\n"):
        skill_dir = self.bundle / "skills" / directory
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{body}",
            encoding="utf-8",
        )
        return path

    def test_valid_two_skill_bundle_imports_atomically(self):
        existing = self.manager.create(
            name="Existing Custom",
            description="Keep existing metadata",
            commands=["keep"],
            workflow_data={"nodes": [{"type": "manual_input"}], "connections": []},
        )
        existing_before = existing.to_dict()
        self.add_skill("first", "First Skill")
        self.add_skill("second", "Second Skill")

        report = import_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertTrue(report["importable"])
        self.assertEqual(report["skills_found"], 2)
        self.assertEqual(report["skills_valid"], 2)
        self.assertEqual(report["imported_count"], 2)
        persisted = SkillManager(self.skills_file)
        self.assertEqual(
            {skill.name for skill in persisted.get_all()},
            {"Existing Custom", "First Skill", "Second Skill"},
        )
        self.assertEqual(persisted.get(existing.id).to_dict(), existing_before)

    def test_valid_ten_skill_bundle_imports_in_one_commit(self):
        for index in range(10):
            self.add_skill(f"skill-{index}", f"Skill {index}")

        report = import_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertTrue(report["importable"])
        self.assertEqual(report["skills_found"], 10)
        self.assertEqual(report["imported_count"], 10)
        self.assertEqual(len(json.loads(self.skills_file.read_text(encoding="utf-8"))), 10)

    def test_json_manifest_is_supported(self):
        (self.bundle / "manifest.yaml").unlink()
        (self.bundle / "manifest.json").write_text(
            json.dumps({"name": "JSON Bundle", "version": "1"}),
            encoding="utf-8",
        )
        self.add_skill("one", "JSON Skill")

        report = validate_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertTrue(report.importable)
        self.assertEqual(report.bundle_name, "JSON Bundle")

    def test_existing_skill_duplicate_rejects_entire_bundle(self):
        self.manager.create(name="Already Here", description="Existing")
        before = self.skills_file.read_bytes()
        self.add_skill("first", "New Skill")
        self.add_skill("duplicate", "already-here")

        report = import_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertFalse(report["importable"])
        self.assertEqual(len(report["duplicates"]), 1)
        self.assertEqual(self.skills_file.read_bytes(), before)

    def test_duplicate_names_inside_bundle_reject_entire_bundle(self):
        self.add_skill("first", "Same Name")
        self.add_skill("second", "same-name")

        report = validate_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertFalse(report.importable)
        self.assertEqual(len(report.duplicates), 1)
        self.assertEqual(self.skills_file.exists(), False)

    def test_one_invalid_skill_rejects_all_and_returns_validation_error(self):
        self.add_skill("valid", "Valid Skill")
        invalid = self.bundle / "skills" / "invalid" / "SKILL.md"
        invalid.parent.mkdir(parents=True)
        invalid.write_text("# Missing frontmatter\n", encoding="utf-8")

        report = import_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertFalse(report["importable"])
        self.assertEqual(report["imported_count"], 0)
        self.assertEqual(report["skills_valid"], 1)
        self.assertEqual(report["skills_invalid"], 1)
        self.assertTrue(report["validation_errors"])
        self.assertFalse(self.skills_file.exists())

    def test_traversal_attack_rejects_bundle_without_writes(self):
        skill = self.add_skill("traversal", "Traversal", body="[secret](../outside.txt)\n")
        (self.root / "outside.txt").write_text("not importable", encoding="utf-8")

        report = import_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertFalse(report["importable"])
        self.assertTrue(report["security_errors"])
        self.assertEqual(self.skills_file.exists(), False)
        self.assertTrue(skill.is_file())

    def test_symlink_escape_in_skill_resources_rejects_bundle(self):
        skill = self.add_skill("linked", "Linked", body="[secret](references/secret.txt)\n")
        references = skill.parent / "references"
        references.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("not importable", encoding="utf-8")
        (references / "secret.txt").symlink_to(outside)

        report = import_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertFalse(report["importable"])
        self.assertTrue(report["security_errors"])
        self.assertFalse(self.skills_file.exists())

    def test_script_resource_is_imported_as_inert_unsupported_file(self):
        skill = self.add_skill("script", "Script Reference", body="See `scripts/helper.py`.\n")
        scripts = skill.parent / "scripts"
        scripts.mkdir()
        (scripts / "helper.py").write_text("raise RuntimeError('must remain inert')\n", encoding="utf-8")

        report = import_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertTrue(report["importable"])
        self.assertEqual(report["imported_count"], 1)
        self.assertEqual(len(report["unsupported_scripts"]), 1)
        stored = next(iter(SkillManager(self.skills_file).get_all()))
        resource = self.data_dir / stored.workflow_data["resource_dir"] / "scripts" / "helper.py"
        self.assertIn("unsupported_scripts", stored.workflow_data)
        self.assertTrue(resource.is_file())

    def test_paid_provider_metadata_rejects_bundle(self):
        cases = (
            ("provider: openai", "provider"),
            ("requires_paid_llm: 'true'", "requires_paid_llm"),
        )
        for metadata, expected in cases:
            with self.subTest(metadata=metadata):
                paid_bundle = self.root / expected
                self.bundle = paid_bundle
                self.write_manifest()
                path = self.add_skill("paid", "Paid Requirement")
                path.write_text(
                    "---\nname: Paid Requirement\ndescription: Paid route\nmetadata:\n  "
                    + metadata
                    + "\n---\nInstructions.\n",
                    encoding="utf-8",
                )

                report = import_skill_bundle(str(self.bundle), manager=self.manager)

                self.assertFalse(report["importable"])
                self.assertEqual(len(report["paid_dependencies"]), 1)
                self.assertFalse(self.skills_file.exists())

    def test_failed_atomic_commit_rolls_back_resources_and_skill_records(self):
        skill = self.add_skill("resource", "Resource Skill", body="[reference](references/info.txt)\n")
        references = skill.parent / "references"
        references.mkdir()
        (references / "info.txt").write_text("supporting text", encoding="utf-8")
        with patch.object(self.manager, "_write_skills", side_effect=OSError("simulated write failure")):
            report = import_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertFalse(report["importable"])
        self.assertTrue(report["rollback_occurred"])
        self.assertEqual(report["imported_count"], 0)
        self.assertFalse(self.skills_file.exists())
        self.assertEqual(self.manager.get_all(), [])
        managed = self.data_dir / "imported_skills"
        self.assertFalse(managed.exists() and any(managed.iterdir()))

    def test_bundle_import_persists_after_fresh_manager_reload(self):
        self.add_skill("one", "Reload Skill")

        report = import_skill_bundle(str(self.bundle), manager=self.manager)

        self.assertEqual(report["imported_count"], 1)
        imported_id = report["imported_skills"][0]["id"]
        fresh = SkillManager(self.skills_file)
        self.assertEqual(fresh.get(imported_id).name, "Reload Skill")

    def test_two_independent_managers_preserve_bundle_and_later_mutations(self):
        self.add_skill("one", "Bundle One")
        self.add_skill("two", "Bundle Two")
        manager_a = SkillManager(self.skills_file)
        manager_b = SkillManager(self.skills_file)
        report = import_skill_bundle(str(self.bundle), manager=manager_a)

        manager_b.create(name="Created By B", description="Latest disk mutation")

        names = {
            skill["name"]
            for skill in json.loads(self.skills_file.read_text(encoding="utf-8"))
        }
        self.assertEqual(report["imported_count"], 2)
        self.assertEqual(names, {"Bundle One", "Bundle Two", "Created By B"})

    def test_dry_run_validates_without_changing_store_or_resources(self):
        self.add_skill("one", "Dry Run Skill")
        self.manager.create(name="Existing", description="Must remain")
        before = self.skills_file.read_bytes()

        report = import_skill_bundle(str(self.bundle), manager=self.manager, dry_run=True)

        self.assertTrue(report["importable"])
        self.assertEqual(report["skills_found"], 1)
        self.assertEqual(report["imported_count"], 0)
        self.assertFalse(report["rollback_occurred"])
        self.assertEqual(self.skills_file.read_bytes(), before)
        self.assertFalse((self.data_dir / "imported_skills").exists())


if __name__ == "__main__":
    unittest.main()

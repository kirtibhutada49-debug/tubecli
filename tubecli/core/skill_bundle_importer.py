"""Offline validator and atomic importer for local TubeCLI skill bundles."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from tubecli.core.skill import Skill, SkillManager
from tubecli.core.skill_importer import (
    SkillImportError,
    _UniqueKeySafeLoader,
    _copy_skill_resources,
    _prepare_skill_from_file,
)

_MAX_MANIFEST_BYTES = 1024 * 1024


class SkillBundleImportError(ValueError):
    """A bundle could not be safely imported."""


@dataclass
class BundleValidation:
    bundle_name: str
    skills_found: int = 0
    skills_valid: int = 0
    skills_invalid: int = 0
    duplicates: List[str] = field(default_factory=list)
    paid_dependencies: List[str] = field(default_factory=list)
    security_errors: List[str] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)
    unsupported_scripts: List[str] = field(default_factory=list)
    importable: bool = False
    prepared: List[Tuple[Skill, Dict[str, Path]]] = field(default_factory=list, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundle_name": self.bundle_name,
            "skills_found": self.skills_found,
            "skills_valid": self.skills_valid,
            "skills_invalid": self.skills_invalid,
            "duplicates": list(self.duplicates),
            "paid_dependencies": list(self.paid_dependencies),
            "security_errors": list(self.security_errors),
            "validation_errors": list(self.validation_errors),
            "unsupported_scripts": list(self.unsupported_scripts),
            "importable": self.importable,
            "imported_count": 0,
            "rollback_occurred": False,
            "imported_skills": [],
        }


def _json_no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SkillBundleImportError(f"Duplicate manifest key: {key!r}.")
        result[key] = value
    return result


def _manifest(bundle_root: Path) -> Tuple[str, List[str]]:
    yaml_path = bundle_root / "manifest.yaml"
    json_path = bundle_root / "manifest.json"
    errors = []
    manifests = [path for path in (yaml_path, json_path) if path.exists() or path.is_symlink()]
    if len(manifests) != 1:
        return bundle_root.name, ["Bundle must contain exactly one manifest.yaml or manifest.json."]

    manifest_path = manifests[0]
    try:
        resolved = manifest_path.resolve(strict=True)
        if not resolved.is_relative_to(bundle_root):
            return bundle_root.name, ["Manifest path resolves outside the bundle directory."]
        if not resolved.is_file() or resolved.stat().st_size > _MAX_MANIFEST_BYTES:
            return bundle_root.name, ["Manifest must be a regular file no larger than 1 MiB."]
        text = resolved.read_text(encoding="utf-8")
        if manifest_path.suffix == ".json":
            document = json.loads(text, object_pairs_hook=_json_no_duplicate_keys)
        else:
            document = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError, SkillBundleImportError) as exc:
        return bundle_root.name, [f"Invalid bundle manifest: {exc}"]

    if not isinstance(document, dict):
        return bundle_root.name, ["Bundle manifest must contain a mapping/object."]
    name = document.get("name", bundle_root.name)
    if not isinstance(name, str) or not name.strip():
        errors.append("Manifest 'name' must be a non-empty string.")
        name = bundle_root.name
    for field_name in ("version", "description"):
        if field_name in document and not isinstance(document[field_name], str):
            errors.append(f"Manifest '{field_name}' must be a string.")
    return name.strip(), errors


def _bundle_files(bundle_root: Path, skills_root: Path) -> Tuple[List[Path], List[str]]:
    skill_files = []
    security_errors = []
    for current, directories, files in os.walk(skills_root, followlinks=False):
        current_path = Path(current)
        safe_directories = []
        for directory in directories:
            entry = current_path / directory
            if entry.is_symlink():
                security_errors.append(
                    f"Symlinked skill directory is unsupported: {entry.relative_to(bundle_root)}."
                )
                continue
            safe_directories.append(directory)
        directories[:] = safe_directories

        for filename in files:
            entry = current_path / filename
            if filename.casefold() != "skill.md":
                continue
            try:
                resolved = entry.resolve(strict=True)
            except (OSError, RuntimeError):
                security_errors.append(f"Skill file cannot be resolved: {entry.relative_to(bundle_root)}.")
                continue
            if not resolved.is_relative_to(bundle_root):
                security_errors.append(f"Skill file escapes bundle root: {entry.relative_to(bundle_root)}.")
                continue
            if not resolved.is_file():
                security_errors.append(f"Skill path is not a regular file: {entry.relative_to(bundle_root)}.")
                continue
            skill_files.append(resolved)
    return sorted(skill_files), security_errors


def validate_skill_bundle(path: str, *, manager=None) -> BundleValidation:
    """Validate a local bundle completely without copying resources or writing skills."""
    try:
        bundle_root = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        report = BundleValidation(Path(path).name or "bundle")
        report.validation_errors.append(f"Cannot access local bundle directory: {exc}")
        return report
    if not bundle_root.is_dir():
        report = BundleValidation(bundle_root.name)
        report.validation_errors.append("Bundle path must be a directory.")
        return report

    bundle_name, manifest_errors = _manifest(bundle_root)
    report = BundleValidation(bundle_name)
    report.validation_errors.extend(manifest_errors)
    skills_root = bundle_root / "skills"
    if not skills_root.is_dir() or skills_root.is_symlink():
        report.security_errors.append("Bundle must contain a real skills/ directory.")
        return report

    skill_files, scan_security_errors = _bundle_files(bundle_root, skills_root)
    report.security_errors.extend(scan_security_errors)
    report.skills_found = len(skill_files)
    if report.skills_found == 0:
        report.validation_errors.append("Bundle contains no SKILL.md files under skills/.")

    if manager is None:
        from tubecli.core.skill import skill_manager
        manager = skill_manager

    normalize = SkillManager._normalize_name
    existing_names = {
        normalize(skill.name) or skill.name.casefold()
        for skill in manager.get_all()
    }
    seen_names = set()
    for skill_file in skill_files:
        relative = skill_file.relative_to(bundle_root).as_posix()
        try:
            skill, resources = _prepare_skill_from_file(str(skill_file), str(uuid.uuid4()))
        except SkillImportError as exc:
            message = f"{relative}: {exc}"
            lowered = str(exc).casefold()
            if any(marker in lowered for marker in (
                "provider", "model", "api_key", "base_url", "inference",
            )):
                report.paid_dependencies.append(message)
            elif any(marker in lowered for marker in (
                "path", "traversal", "outside", "external resource", "absolute",
            )):
                report.security_errors.append(message)
            else:
                report.validation_errors.append(message)
            continue

        normalized_name = normalize(skill.name) or skill.name.casefold()
        duplicate = normalized_name in existing_names or normalized_name in seen_names
        if duplicate:
            report.duplicates.append(f"{relative}: {skill.name}")
            continue
        seen_names.add(normalized_name)
        report.unsupported_scripts.extend(
            f"{relative}: {script}" for script in skill.workflow_data["unsupported_scripts"]
        )
        report.prepared.append((skill, resources))

    report.skills_valid = len(report.prepared)
    report.skills_invalid = report.skills_found - report.skills_valid
    report.importable = not any((
        report.skills_invalid,
        report.duplicates,
        report.paid_dependencies,
        report.security_errors,
        report.validation_errors,
    ))
    return report


def import_skill_bundle(path: str, *, manager=None, dry_run: bool = False) -> Dict[str, Any]:
    """Validate a bundle, then persist every contained skill as one transaction."""
    report = validate_skill_bundle(path, manager=manager)
    result = report.to_dict()
    if not report.importable or dry_run:
        return result

    if manager is None:
        from tubecli.core.skill import skill_manager
        manager = skill_manager
    if not hasattr(manager, "create_many"):
        result["importable"] = False
        result["validation_errors"].append("Skill manager does not support atomic bundle persistence.")
        return result

    resource_directories = []
    try:
        for skill, resources in report.prepared:
            resource_dir = _copy_skill_resources(skill, resources)
            if resource_dir:
                resource_directories.append(resource_dir)
        imported = manager.create_many(skill for skill, _ in report.prepared)
    except Exception as exc:
        for resource_dir in reversed(resource_directories):
            if resource_dir.exists():
                shutil.rmtree(resource_dir)
        result["importable"] = False
        result["rollback_occurred"] = True
        result["validation_errors"].append(f"Atomic bundle import failed: {exc}")
        return result

    result["imported_count"] = len(imported)
    result["imported_skills"] = [
        {"id": skill.id, "name": skill.name, "skill_format": skill.skill_format}
        for skill in imported
    ]
    return result

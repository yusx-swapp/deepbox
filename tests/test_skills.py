import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connector.local_store import LocalProjectStore
from connector.skills import (
    SkillBinding,
    SkillCollisionError,
    SkillDriftError,
    SkillManager,
    SkillValidationError,
    inspect_source,
    parse_frontmatter,
)


def write_skill(root: Path, name: str, *, description="A demo skill.",
                extra=None, script=False, frontmatter=None):
    pkg = root / name
    pkg.mkdir(parents=True)
    if frontmatter is None:
        frontmatter = f"---\nname: {name}\ndescription: {description}\n---\n"
    (pkg / "SKILL.md").write_text(frontmatter + "\nBody text.\n", encoding="utf-8")
    if extra:
        for rel, content in extra.items():
            target = pkg / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    if script:
        (pkg / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    return pkg


class FrontmatterTests(unittest.TestCase):
    def test_quoted_and_block_scalars(self):
        text = (
            "---\n"
            'name: "my-skill"\n'
            "description: |\n"
            "  Line one.\n"
            "  Line two.\n"
            "version: 1\n"
            "---\nbody\n"
        )
        fields = parse_frontmatter(text)
        self.assertEqual(fields["name"], "my-skill")
        self.assertEqual(
            fields["description"],
            "Line one." + chr(10) + "Line two." + chr(10))

    def test_folded_scalar(self):
        text = "---\nname: x\ndescription: >\n  a\n  b\n---\n"
        fields = parse_frontmatter(text)
        self.assertEqual(fields["description"], "a b")

    def test_missing_frontmatter_raises(self):
        with self.assertRaises(SkillValidationError):
            parse_frontmatter("no frontmatter here")


class InspectTests(unittest.TestCase):
    def test_valid_package_digest_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pkg = write_skill(root, "alpha", extra={"ref/notes.md": "hi"})
            m1 = inspect_source(pkg)
            m2 = inspect_source(pkg)
            self.assertEqual(m1.digest, m2.digest)
            self.assertEqual(m1.name, "alpha")
            self.assertFalse(m1.contains_scripts)

    def test_scripts_are_flagged_not_executed(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = write_skill(Path(d), "beta", script=True)
            self.assertTrue(inspect_source(pkg).contains_scripts)

    def test_invalid_name_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            fm = "---\nname: Bad_Name\ndescription: x\n---\n"
            pkg = write_skill(Path(d), "bad", frontmatter=fm)
            # directory name differs too, but name validation triggers first-ish
            with self.assertRaises(SkillValidationError):
                inspect_source(pkg)

    def test_dir_name_must_match(self):
        with tempfile.TemporaryDirectory() as d:
            fm = "---\nname: other-name\ndescription: x\n---\n"
            pkg = write_skill(Path(d), "mismatch", frontmatter=fm)
            with self.assertRaises(SkillValidationError):
                inspect_source(pkg)

    def test_missing_skill_md(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "empty"
            pkg.mkdir()
            with self.assertRaises(SkillValidationError):
                inspect_source(pkg)

    def test_file_limit(self):
        with tempfile.TemporaryDirectory() as d:
            extra = {f"f{i}.txt": "x" for i in range(300)}
            pkg = write_skill(Path(d), "big", extra=extra)
            with self.assertRaises(SkillValidationError):
                inspect_source(pkg)

    def test_byte_limit(self):
        with tempfile.TemporaryDirectory() as d:
            extra = {"big.bin": "A" * (11 * 1024 * 1024)}
            pkg = write_skill(Path(d), "heavy", extra=extra)
            with self.assertRaises(SkillValidationError):
                inspect_source(pkg)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = write_skill(Path(d), "linky")
            target = Path(d) / "outside.txt"
            target.write_text("secret", encoding="utf-8")
            try:
                os.symlink(target, pkg / "link.txt")
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation not permitted")
            with self.assertRaises(SkillValidationError):
                inspect_source(pkg)


class ManagerTests(unittest.TestCase):
    def _mgr(self, d):
        store = LocalProjectStore(Path(d) / "projects.db")
        mgr = SkillManager(store)
        return store, mgr

    def test_personal_install_store_copy_and_bindings(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha")
                bind_root = root / "runtime-a"
                res = mgr.install(
                    pkg,
                    bindings={"claude": SkillBinding("claude", str(bind_root))},
                )
                self.assertTrue(res.changed)
                dest = bind_root / "alpha"
                self.assertTrue((dest / "SKILL.md").is_file())
                # store dir exists
                self.assertTrue(Path(res.skill.store_path).is_dir())
                self.assertEqual(res.skill.scope, "personal")
                self.assertEqual(res.skill.targets, ["claude"])

    def test_project_with_installed_skill_cannot_be_removed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                (root / "repo").mkdir()
                project = store.add(root / "repo", "repo")
                pkg = write_skill(root / "src", "alpha")
                mgr.install(
                    pkg,
                    project=project,
                    bindings=[SkillBinding("claude", str(root / "repo-skills"))],
                )
                with self.assertRaisesRegex(ValueError, "remove those skills first"):
                    store.remove(project.id)
                self.assertIsNotNone(store.get(project.id))

    def test_project_install_and_separation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                proj_path = root / "repo"
                proj_path.mkdir()
                project = store.add(proj_path)
                pkg = write_skill(root / "src", "alpha")
                bp = SkillBinding("claude", str(root / "rt"))
                mgr.install(pkg, project=project, bindings=[bp])
                # same name at user scope is separate
                bu = SkillBinding("claude", str(root / "rt-user"))
                mgr.install(pkg, bindings=[bu])
                self.assertEqual(len(mgr.list(project)), 1)
                self.assertEqual(len(mgr.list()), 1)
                self.assertEqual(len(mgr.list(all_scopes=True)), 2)

    def test_sanitized_metadata_has_no_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha", script=True)
                bind_root = root / "rt"
                mgr.install(pkg, bindings=[SkillBinding("claude", str(bind_root))])
                inv = mgr.inventory()
                self.assertEqual(len(inv), 1)
                entry = inv[0]
                self.assertEqual(
                    set(entry),
                    {"id", "name", "description", "digest", "scope",
                     "project_id", "targets", "contains_scripts", "status"},
                )
                self.assertTrue(entry["contains_scripts"])
                blob = repr(inv) + repr(store.public_skills())
                self.assertNotIn(str(bind_root), blob)
                self.assertNotIn("store", blob)
                self.assertEqual(entry["status"], "installed")

    def test_idempotent_install(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha")
                b = [SkillBinding("claude", str(root / "rt"))]
                mgr.install(pkg, bindings=b)
                res2 = mgr.install(pkg, bindings=b)
                self.assertTrue(res2.idempotent)

    def test_unmanaged_collision_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha")
                dest_root = root / "rt"
                (dest_root / "alpha").mkdir(parents=True)
                (dest_root / "alpha" / "other.txt").write_text("x", encoding="utf-8")
                with self.assertRaises(SkillCollisionError):
                    mgr.install(pkg, bindings=[SkillBinding("claude", str(dest_root))])
                # force replaces
                res = mgr.install(
                    pkg, bindings=[SkillBinding("claude", str(dest_root))], force=True
                )
                self.assertTrue((dest_root / "alpha" / "SKILL.md").is_file())
                self.assertFalse((dest_root / "alpha" / "other.txt").exists())
                self.assertEqual(res.skill.name, "alpha")

    def test_drift_refusal_and_force_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha")
                b = [SkillBinding("claude", str(root / "rt"))]
                mgr.install(pkg, bindings=b)
                dest = root / "rt" / "alpha"
                (dest / "tampered.txt").write_text("bad", encoding="utf-8")
                skill = mgr.list()[0]
                self.assertEqual(mgr.status(skill), "drifted")
                with self.assertRaises(SkillDriftError):
                    mgr.install(pkg, bindings=b)
                # force upgrade repairs
                mgr.install(pkg, bindings=b, force=True)
                self.assertFalse((dest / "tampered.txt").exists())

    def test_drift_refusal_on_remove_and_force_remove(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha")
                mgr.install(pkg, bindings=[SkillBinding("claude", str(root / "rt"))])
                dest = root / "rt" / "alpha"
                (dest / "tampered.txt").write_text("bad", encoding="utf-8")
                with self.assertRaises(SkillDriftError):
                    mgr.remove("alpha")
                self.assertTrue(mgr.remove("alpha", force=True))
                self.assertFalse(dest.exists())
                self.assertEqual(mgr.list(), [])

    def test_missing_status(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha")
                mgr.install(pkg, bindings=[SkillBinding("claude", str(root / "rt"))])
                shutil.rmtree(root / "rt" / "alpha")
                skill = mgr.list()[0]
                self.assertEqual(mgr.status(skill), "missing")
                with self.assertRaises(SkillDriftError):
                    mgr.remove("alpha")
                self.assertTrue(mgr.remove("alpha", force=True))

    def test_remove_failure_keeps_registry_record(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha")
                dest = root / "rt" / "alpha"
                mgr.install(pkg, bindings=[SkillBinding("claude", str(root / "rt"))])
                real_rmtree = shutil.rmtree

                def fail_destination(path, *args, **kwargs):
                    if Path(path) == dest:
                        raise PermissionError("simulated locked destination")
                    return real_rmtree(path, *args, **kwargs)

                with mock.patch("connector.skills.shutil.rmtree", side_effect=fail_destination):
                    with self.assertRaises(PermissionError):
                        mgr.remove("alpha")
                self.assertTrue(dest.is_dir())
                self.assertEqual([skill.name for skill in mgr.list()], ["alpha"])

    def test_standard_nested_frontmatter_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pkg = root / "alpha"
            pkg.mkdir()
            (pkg / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: Nested metadata\n"
                "allowed-tools:\n  - Read\n  - Grep\n"
                "metadata:\n  owner: local-user\n---\n# Alpha\n",
                encoding="utf-8")
            meta = inspect_source(pkg)
            self.assertEqual(meta.name, "alpha")
            self.assertEqual(meta.description, "Nested metadata")

    def test_multiple_roots_per_runtime_family(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha")
                result = mgr.install(pkg, bindings=[SkillBinding(
                    "claude", (str(root / "one"), str(root / "two")))])
                self.assertEqual(result.skill.targets, ["claude"])
                self.assertTrue((root / "one" / "alpha" / "SKILL.md").is_file())
                self.assertTrue((root / "two" / "alpha" / "SKILL.md").is_file())
                self.assertEqual(len(result.skill.bindings), 2)

    def test_rediscovered_roots_do_not_orphan_previous_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            with store:
                pkg = write_skill(root / "src", "alpha")
                mgr.install(pkg, bindings=[SkillBinding("claude", str(root / "one"))])
                result = mgr.install(
                    pkg, bindings=[SkillBinding("claude", str(root / "two"))]
                )
                self.assertEqual(len(result.skill.bindings), 2)
                self.assertTrue((root / "one" / "alpha" / "SKILL.md").is_file())
                self.assertTrue((root / "two" / "alpha" / "SKILL.md").is_file())

    def test_upgrade_garbage_collects_unreferenced_store_digest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            bindings = [SkillBinding("claude", str(root / "dest"))]
            with store:
                pkg = write_skill(root, "alpha")
                first = mgr.install(pkg, bindings=bindings)
                old_store = Path(first.skill.store_path).parent
                self.assertTrue(old_store.is_dir())
                (pkg / "SKILL.md").write_text(
                    "---\nname: alpha\ndescription: updated\n---\n# New\n",
                    encoding="utf-8",
                )
                second = mgr.install(pkg, bindings=bindings)
                self.assertNotEqual(first.skill.digest, second.skill.digest)
                self.assertFalse(old_store.exists())

    def test_oversized_bound_tree_reports_drift_without_unbounded_read(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            binding = SkillBinding("claude", str(root / "dest"))
            with store:
                pkg = write_skill(root / "src", "alpha")
                result = mgr.install(pkg, bindings=[binding])
                oversized = root / "dest" / "alpha" / "oversized.bin"
                with oversized.open("wb") as handle:
                    handle.truncate(11 * 1024 * 1024)
                self.assertEqual(mgr.status(result.skill), "drifted")

    def test_multi_destination_upgrade_rolls_back_as_a_unit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store, mgr = self._mgr(d)
            bindings = [SkillBinding(
                "claude", (str(root / "one"), str(root / "two")))]
            with store:
                pkg = write_skill(
                    root, "alpha",
                    frontmatter="---\nname: alpha\ndescription: test skill\n---\n# Version one\n")
                mgr.install(pkg, bindings=bindings)
                (pkg / "SKILL.md").write_text(
                    "---\nname: alpha\ndescription: test skill\n---\n# Version two\n",
                    encoding="utf-8")
                real_replace = os.replace
                second = root / "two" / "alpha"

                def fail_second_stage(source, destination):
                    if (Path(destination) == second
                            and Path(source).name.startswith(".skill-tmp-")):
                        raise OSError("simulated second destination failure")
                    return real_replace(source, destination)

                with mock.patch("connector.skills.os.replace", side_effect=fail_second_stage):
                    with self.assertRaises(OSError):
                        mgr.install(pkg, bindings=bindings)
                for parent in ("one", "two"):
                    content = (root / parent / "alpha" / "SKILL.md").read_text(
                        encoding="utf-8")
                    self.assertIn("Version one", content)
                    self.assertNotIn("Version two", content)


if __name__ == "__main__":
    unittest.main()

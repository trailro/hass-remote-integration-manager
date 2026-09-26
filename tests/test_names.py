"""Instance names, slugs, versions and git refs."""

import unittest

from hrimgr import names


class NameTest(unittest.TestCase):
    def test_valid_names(self):
        for name in ("a", "garage", "ramses_cc", "x1", "a" * 20, "a_b_c"):
            with self.subTest(name=name):
                self.assertEqual(names.validate_name(name), name)

    def test_invalid_names(self):
        for name in ("", "A", "Garage", "1abc", "_abc", "a" * 21, "a-b", "a.b", "a b", "é", "a/b", "../x", "a\n", None, 3,
                     "manager", "self", "api", "static"):
            with self.subTest(name=name):
                with self.assertRaises(names.InvalidName):
                    names.validate_name(name)

    def test_derived_names(self):
        self.assertEqual(names.folder_name("garage_door"), "hri_garage_door")
        self.assertEqual(names.config_slug("garage_door"), "hri_garage_door")
        self.assertEqual(names.supervisor_slug("garage_door"), "local_hri_garage_door")
        self.assertEqual(names.display_name("garage_door"), "HRI Garage Door")
        self.assertEqual(names.panel_title("garage_door"), "HRI garage_door")

    def test_name_from_slug(self):
        self.assertEqual(names.name_from_slug("local_hri_garage"), "garage")
        for slug in ("local_hri_", "local_hri_Garage", "5c53de3b_hri_garage", "local_hri_manager", "local_hri_" + "a" * 21, None):
            with self.subTest(slug=slug):
                self.assertIsNone(names.name_from_slug(slug))


class VersionTest(unittest.TestCase):
    def test_order(self):
        order = ["0.24.0", "0.25.0", "0.25.1", "0.26.0b1", "0.26.0rc1", "0.26.0", "0.100.0"]
        parsed = [names.parse_version(v) for v in order]
        self.assertEqual(parsed, sorted(parsed))

    def test_tags_and_floor(self):
        self.assertEqual(names.version_from_tag("v0.25.0"), "0.25.0")
        self.assertIsNone(names.version_from_tag("0.25.0"))
        self.assertIsNone(names.version_from_tag("v0.25"))
        self.assertTrue(names.supported_version("0.25.0"))
        self.assertTrue(names.supported_version("0.26.0b1"))
        self.assertFalse(names.supported_version("0.24.0"))
        self.assertFalse(names.supported_version("0.25.0rc1"))  # before 0.25.0
        self.assertFalse(names.supported_version("latest"))

    def test_refs(self):
        for kind, ref in (("branch", "main"), ("branch", "release/0.25.0"), ("tag", "v0.25.0"), ("branch", "fix/some-thing_1"),
                          ("tag", "test-tag"), ("branch", "cafe")):
            with self.subTest(ref=ref):
                self.assertEqual(names.validate_ref(kind, ref), (kind, ref))
        for ref in ("", "../x", "a..b", "a//b", "/main", "main/", "x.lock", "a b", "a?b", "a#b", "a%2e", "-x", "x" * 101, None,
                    "a/.hidden", ".x"):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    names.validate_ref("branch", ref)

    def test_only_branches_and_tags_of_hri(self):
        """Not a pull request (anyone can open one), not a commit (a fork's commit is served under HRI's name too),
        not a full refs/ path (the manager adds refs/heads/ or refs/tags/ itself)."""
        for kind, ref in (("branch", "refs/pull/1/head"), ("branch", "pull/1/head"), ("branch", "pull/1/merge"),
                          ("branch", "refs/heads/main"), ("tag", "refs/tags/v0.25.0"), ("branch", "REFS/heads/main"),
                          ("branch", "a" * 40), ("branch", "abcdef1"), ("tag", "0123456789ABCDEF"), ("commit", "main"),
                          (None, "main"), ("pull", "1")):
            with self.subTest(kind=kind, ref=ref), self.assertRaises(ValueError):
                names.validate_ref(kind, ref)
        self.assertEqual(names.full_ref("branch", "main"), "refs/heads/main")
        self.assertEqual(names.full_ref("tag", "v0.25.0"), "refs/tags/v0.25.0")

    def test_git_version(self):
        self.assertEqual(names.git_version("0123456789abcdef" * 2 + "01234567"), "0.0.0-0123456789ab")
        self.assertTrue(names.GIT_VERSION_RE.fullmatch("0.0.0-0123456789ab"))
        self.assertFalse(names.GIT_VERSION_RE.fullmatch("0.0.0-0123456"))


if __name__ == "__main__":
    unittest.main()

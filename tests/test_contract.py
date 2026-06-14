"""Contract: validation, frontmatter rendering, domain/identity derivation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kb import contract
from kb.contract import ContractError


def _mini_parse_frontmatter(text: str) -> dict:
    """Tiny parser for kb's own flow-style output (round-trip check only)."""
    lines = text.splitlines()
    assert lines[0] == "---", "no opening fence"
    out: dict[str, object] = {}
    for line in lines[1:]:
        if line == "---":
            break
        key, _, raw = line.partition(": ")
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            items = []
            for part in inner.split(", ") if inner else []:
                if part.startswith('"') and part.endswith('"'):
                    part = part[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                items.append(part)
            out[key] = items
        elif raw.startswith('"') and raw.endswith('"'):
            out[key] = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            out[key] = raw
    return out


class TestValidation(unittest.TestCase):
    def test_topic_accepts_kebab(self):
        self.assertEqual(contract.validate_topic("blue-fallback"), "blue-fallback")

    def test_topic_rejects_spaces_slashes_upper(self):
        for bad in ["has space", "Has-Upper", "a/b", "-leading", "trailing-", "под"]:
            with self.assertRaises(ContractError):
                contract.validate_topic(bad)

    def test_description_required_and_single_line(self):
        with self.assertRaises(ContractError):
            contract.validate_description("   ")
        with self.assertRaises(ContractError):
            contract.validate_description("line1\nline2")
        self.assertEqual(contract.validate_description("  ok  "), "ok")

    def test_keywords_required(self):
        with self.assertRaises(ContractError):
            contract.validate_keywords([])
        with self.assertRaises(ContractError):
            contract.validate_keywords(["", "  "])
        self.assertEqual(contract.validate_keywords([" a ", "b", ""]), ["a", "b"])

    def test_kind_enum(self):
        for k in contract.KINDS:
            self.assertEqual(contract.validate_kind(k), k)
        with self.assertRaises(ContractError):
            contract.validate_kind("blogpost")
        self.assertEqual(contract.DEFAULT_KIND, "note")


class TestRender(unittest.TestCase):
    def test_minimal_has_three_fields_no_optionals(self):
        out = contract.render_frontmatter(description="x", keywords=["a", "b"])
        self.assertIn("kind: note", out)
        self.assertNotIn("links:", out)
        self.assertNotIn("code:", out)
        parsed = _mini_parse_frontmatter(out)
        self.assertEqual(
            parsed, {"description": "x", "keywords": ["a", "b"], "kind": "note"}
        )

    def test_field_order(self):
        out = contract.render_frontmatter(
            description="d",
            keywords=["k"],
            kind="reference",
            links=["other"],
            code=["repo/path"],
        )
        body = [
            ln.split(":")[0]
            for ln in out.splitlines()
            if ":" in ln and not ln.startswith("---")
        ]
        self.assertEqual(body, ["description", "keywords", "kind", "links", "code"])

    def test_no_killed_or_derived_fields(self):
        out = contract.render_frontmatter(
            description="d: with colon # and hash",
            keywords=["示例", "blue"],
            kind="decision",
            links=["a"],
            code=["my-kb/x"],
        )
        lowered = out.lower()
        for forbidden in contract.FORBIDDEN_FIELDS:
            self.assertNotIn(
                f"\n{forbidden}:",
                "\n" + lowered,
                f"{forbidden} leaked into frontmatter",
            )

    def test_description_with_special_chars_roundtrips(self):
        desc = 'has: colon, "quotes" and \\ backslash # hash'
        out = contract.render_frontmatter(description=desc, keywords=["a"])
        self.assertEqual(_mini_parse_frontmatter(out)["description"], desc)

    def test_cjk_keywords_stay_bare(self):
        out = contract.render_frontmatter(
            description="d", keywords=["召回排序", "blue"]
        )
        self.assertIn("keywords: [召回排序, blue]", out)

    def test_plain_strings_stay_bare(self):
        out = contract.render_frontmatter(
            description="d",
            keywords=["示例", "blue", "adr-003", "v5", "API"],
            code=["my-kb/tools/kb"],
        )
        self.assertIn("keywords: [示例, blue, adr-003, v5, API]", out)
        self.assertIn("code: [my-kb/tools/kb]", out)

    def test_coercible_tokens_are_quoted(self):
        # Bare, gopkg.in/yaml.v3 would coerce these to int/float/bool/null/date;
        # the renderer must double-quote every one to preserve the string.
        risky = [
            "2026",
            "007",
            "1e3",
            "1.5",
            "0x1f",
            "2026-06-07",
            "true",
            "False",
            "NULL",
            "~",
            "yes",
            "off",
            "+5",
            "-3",
        ]
        out = contract.render_frontmatter(description="d", keywords=risky)
        for tok in risky:
            self.assertIn(f'"{tok}"', out, f"{tok!r} must be quoted, not bare")
        self.assertEqual(_mini_parse_frontmatter(out)["keywords"], risky)

    def test_supersedes_is_decision_conditional_not_killed(self):
        self.assertNotIn("supersedes", contract.KILLED_FIELDS)
        self.assertIn("supersedes", contract.DECISION_ONLY_FIELDS)
        self.assertNotIn("supersedes", contract.FORBIDDEN_FIELDS)

    def test_assets_is_decision_conditional_not_killed(self):
        self.assertNotIn("assets", contract.KILLED_FIELDS)
        self.assertIn("assets", contract.DECISION_ONLY_FIELDS)
        self.assertNotIn("assets", contract.FORBIDDEN_FIELDS)
        self.assertIn("repo", contract.ASSET_PREFIXES)
        self.assertIn("data", contract.ASSET_PREFIXES)

    def test_asset_prefix(self):
        self.assertEqual(contract.asset_prefix("data:group/dataId#v3"), "data")
        self.assertIsNone(contract.asset_prefix("missing-prefix"))

    def test_keyword_with_comma_is_quoted(self):
        # (commas can't arrive via CLI csv-split, but render must stay valid)
        out = contract.render_frontmatter(description="d", keywords=["a,b", "c"])
        self.assertEqual(_mini_parse_frontmatter(out)["keywords"], ["a,b", "c"])

    def test_assets_render_as_quoted_flow_strings(self):
        assets = [
            "repo:my-service@release/fix",
            "data:group/dataset.json#v3",
            "svc:com.example.MyService#method(Long,Date)",
        ]
        out = contract.render_frontmatter(
            description="d",
            keywords=["a"],
            kind="decision",
            assets=assets,
        )
        self.assertIn('assets: ["repo:my-service@release/fix"', out)
        self.assertEqual(_mini_parse_frontmatter(out)["assets"], assets)

    def test_render_note_appends_body_no_h1_injected(self):
        fm = contract.render_frontmatter(description="d", keywords=["a"])
        note = contract.render_note(fm, "\n\n# My Title\n\nprose\n")
        self.assertTrue(note.startswith("---\n"))
        self.assertIn("\n\n# My Title\n\nprose\n", note)
        self.assertTrue(note.endswith("\n"))
        self.assertEqual(note.count("# My Title"), 1)


class TestDerivation(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / ".git").mkdir()
        return root

    def test_find_repo_root_dir_and_worktree_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            deep = root / "a" / "b"
            deep.mkdir(parents=True)
            self.assertEqual(contract.find_repo_root(deep), root.resolve())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("gitdir: /elsewhere\n")  # worktree marker
            sub = root / "x"
            sub.mkdir()
            self.assertEqual(contract.find_repo_root(sub), root.resolve())

    def test_find_domain_dir_inclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            d = root / "design"
            d.mkdir()
            (d / "INDEX.md").write_text("x")
            self.assertEqual(contract.find_domain_dir(d, root), d.resolve())

    def test_find_domain_dir_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            d = root / "widgets"
            sub = d / "details"
            sub.mkdir(parents=True)
            (d / "INDEX.md").write_text("x")
            self.assertEqual(contract.find_domain_dir(sub, root), d.resolve())

    def test_find_domain_dir_nearest_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            top = root / "widgets"
            deep = top / "blue"
            deep.mkdir(parents=True)
            (top / "INDEX.md").write_text("x")
            (deep / "INDEX.md").write_text("x")
            self.assertEqual(contract.find_domain_dir(deep, root), deep.resolve())

    def test_find_domain_dir_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            d = root / "design"
            d.mkdir()
            self.assertIsNone(contract.find_domain_dir(d, root))

    def test_derive_domain_posix_and_hierarchical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            deep = root / "widgets" / "blue"
            deep.mkdir(parents=True)
            self.assertEqual(contract.derive_domain(deep, root), "widgets/blue")

    def test_derive_domain_rejects_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            with self.assertRaises(ContractError):
                contract.derive_domain(root, root)

    def test_derive_identity(self):
        self.assertEqual(
            contract.derive_identity("my-kb", "widgets/blue", "ap-gap"),
            "my-kb:widgets/blue:ap-gap",
        )

    def test_repo_name_normal_and_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)  # .git is a directory
            self.assertEqual(contract.repo_name(root), root.name)
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "my-kb-kb347"
            wt.mkdir()
            (wt / ".git").write_text(
                "gitdir: /Users/x/workspace/my-kb/.git/worktrees/my-kb-kb347\n"
            )
            self.assertEqual(contract.repo_name(wt), "my-kb")


class TestCaseInsensitiveIndexGuard(unittest.TestCase):
    # on macOS APFS, path existence checks match index.md when asked
    # for INDEX.md; has_index() compares against the real directory listing so
    # a lowercase variant never silently becomes a domain.
    def test_has_index_is_exact_name(self):
        # Separate dirs: on APFS, writing INDEX.md next to an existing index.md
        # would just overwrite it under the lowercase name (case-preserving).
        with tempfile.TemporaryDirectory() as tmp:
            lower, upper = Path(tmp) / "lower", Path(tmp) / "upper"
            lower.mkdir()
            upper.mkdir()
            (lower / "index.md").write_text("# lowercase\n")
            (upper / "INDEX.md").write_text("# upper\n")
            self.assertFalse(contract.has_index(lower))
            self.assertTrue(contract.has_index(upper))
            self.assertFalse(contract.has_index(Path(tmp) / "missing-dir"))

    def test_find_domain_dir_ignores_lowercase_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            docs = repo / "docs"
            docs.mkdir()
            (docs / "index.md").write_text("# lowercase\n")
            self.assertIsNone(contract.find_domain_dir(docs, repo))

    def test_node_chain_ignores_lowercase_variant_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            mid = repo / "mid"
            leaf = mid / "leaf"
            leaf.mkdir(parents=True)
            (mid / "index.md").write_text("# lowercase, not a domain node\n")
            (leaf / "INDEX.md").write_text("# real\n")
            self.assertEqual(contract.derive_node_chain_domain(leaf, repo), "leaf")


if __name__ == "__main__":
    unittest.main()

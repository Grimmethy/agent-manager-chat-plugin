"""Prove that `VERIFY.md merge=union` in .gitattributes is what lets two
append-only VERIFY.md histories merge cleanly.

The test builds throwaway git repos in a temp directory:
  1. a base commit with VERIFY.md ("Initial") and the repo's own
     .gitattributes copied in and committed -- with the union line removed in
     the conflict case (mutation check);
  2. the current branch appends "Section A" and a side branch appends
     "Section B", both at the end of VERIFY.md;
  3. the side branch is merged back.

With the union line the merge succeeds and both sections are present.
Without it, the same merge conflicts. No network, nothing skipped.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def build_scenario(tmp, with_union_line):
    """Create a git repo in tmp with two divergent appends to VERIFY.md and
    attempt to merge them. Returns (merge_result, merged_file_text)."""
    git(tmp, "init")
    git(tmp, "config", "user.name", "test")
    git(tmp, "config", "user.email", "test@example.invalid")
    git(tmp, "config", "commit.gpgsign", "false")

    with open(os.path.join(tmp, "VERIFY.md"), "w") as f:
        f.write("## Initial (piece 1/1)\n\nInitial notes.\n")

    # Copy the repo's own .gitattributes into the temp repo.
    src = os.path.join(REPO_ROOT, ".gitattributes")
    dst = os.path.join(tmp, ".gitattributes")
    shutil.copy(src, dst)
    if not with_union_line:
        # Mutation check: drop the union line, keeping everything else.
        with open(dst) as f:
            lines = f.readlines()
        with open(dst, "w") as f:
            f.writelines(l for l in lines
                         if not l.split()[:2] == ["VERIFY.md", "merge=union"])

    git(tmp, "add", "-A")
    git(tmp, "commit", "-m", "base")

    main_branch = git(tmp, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(tmp, "branch", "side")

    with open(os.path.join(tmp, "VERIFY.md"), "a") as f:
        f.write("\n## Section A (piece 2/1)\n\nSection A content.\n")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-m", "append section A")

    git(tmp, "checkout", "side")
    with open(os.path.join(tmp, "VERIFY.md"), "a") as f:
        f.write("\n## Section B (piece 2/1)\n\nSection B content.\n")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-m", "append section B")

    git(tmp, "checkout", main_branch)
    merge = git(tmp, "merge", "side", check=False)
    with open(os.path.join(tmp, "VERIFY.md")) as f:
        text = f.read()
    return merge, text


class UnionMergeTests(unittest.TestCase):
    def test_merge_succeeds_with_union_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            merge, text = build_scenario(tmp, with_union_line=True)
            self.assertEqual(
                merge.returncode, 0,
                "merge should succeed with VERIFY.md merge=union; "
                "stdout=%r stderr=%r" % (merge.stdout, merge.stderr),
            )
            self.assertIn("## Section A (piece 2/1)", text)
            self.assertIn("## Section B (piece 2/1)", text)
            self.assertNotIn("<<<<<<<", text)

    def test_same_merge_conflicts_without_union_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            merge, text = build_scenario(tmp, with_union_line=False)
            self.assertTrue(
                merge.returncode != 0 or "<<<<<<<" in text,
                "without the union line the same append-append merge "
                "should conflict; stdout=%r stderr=%r"
                % (merge.stdout, merge.stderr),
            )


if __name__ == "__main__":
    unittest.main()

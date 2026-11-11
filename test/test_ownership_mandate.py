"""The ownership mandate stays one text across every author and reviewer copy.

Authors read it from the root ``AGENTS.md`` and the Kiro-Crew-only
``kirocrew-dev`` skills. Reviewers read a reviewer-facing copy from every AI
review lane that judges code structure. Each copy is inlined, so an edit to one
that misses the others would hand authors and reviewers different contracts.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev"

AUTHOR_FIRST = "This codebase is yours:"
AUTHOR_LAST = "harness-safety rules in the root `AGENTS.md`."
AUTHOR_SOURCES = (
    ROOT / "AGENTS.md",
    SKILLS / "kirocrew-prepare-pr" / "SKILL.md",
    SKILLS / "kirocrew-worktree-dev" / "SKILL.md",
    SKILLS / "writing-tests" / "SKILL.md",
    SKILLS / "dashboard-template" / "SKILL.md",
)

REVIEW_FIRST = "OWN THE CODEBASE, IN BOTH DIRECTIONS."
REVIEW_LAST = "The security invariants in AGENTS.md still bind."
REVIEW_SOURCES = (
    ROOT / ".github" / "review-prompts" / "first-principles.md",
    ROOT / ".github" / "review-prompts" / "gpt-repo-context.md",
    ROOT / ".github" / "review-prompts" / "opus-discovery.md",
    ROOT / ".github" / "review-prompts" / "opus-validate.md",
    ROOT / ".github" / "review-prompts" / "security-scope.md",
    ROOT / ".github" / "workflows" / "design-review.yml",
    ROOT / ".github" / "workflows" / "fork-design-review.yml",
    ROOT / ".github" / "workflows" / "fork-gpt-review.yml",
)


def _block(path: Path, first: str, last: str) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if first in line]
    assert len(starts) == 1, f"{path.relative_to(ROOT)} carries {len(starts)} copies of {first!r}"
    start = starts[0]
    end = next((i for i in range(start, len(lines)) if lines[i].rstrip().endswith(last)), None)
    assert end is not None, f"{path.relative_to(ROOT)}: {first!r} block never reaches {last!r}"
    block = lines[start : end + 1]
    indent = len(block[0]) - len(block[0].lstrip())
    return "\n".join(line[indent:] if line.strip() else "" for line in block)


@pytest.mark.parametrize(
    ("sources", "first", "last"),
    [
        pytest.param(AUTHOR_SOURCES, AUTHOR_FIRST, AUTHOR_LAST, id="author"),
        pytest.param(REVIEW_SOURCES, REVIEW_FIRST, REVIEW_LAST, id="reviewer"),
    ],
)
def test_every_copy_is_byte_identical(sources, first, last):
    reference = _block(sources[0], first, last)
    for path in sources[1:]:
        assert _block(path, first, last) == reference, (
            f"{path.relative_to(ROOT)} drifted from {sources[0].relative_to(ROOT)}; "
            "edit every copy of the ownership mandate together"
        )


def test_both_copies_keep_the_security_carve_out():
    assert "security invariants" in _block(AUTHOR_SOURCES[0], AUTHOR_FIRST, AUTHOR_LAST)
    assert "widening a security fence" in _block(REVIEW_SOURCES[0], REVIEW_FIRST, REVIEW_LAST)

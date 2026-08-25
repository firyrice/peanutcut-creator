import os
import tempfile
import textwrap

from scripts.validate_skill import validate_skill_dir


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_missing_skill_md_is_reported():
    with tempfile.TemporaryDirectory() as d:
        problems = validate_skill_dir(d)
        assert any("SKILL.md" in p for p in problems)


def test_missing_frontmatter_fields_reported():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "SKILL.md"), "# no frontmatter here\n")
        problems = validate_skill_dir(d)
        assert any("name" in p for p in problems)
        assert any("description" in p for p in problems)


def test_broken_reference_link_reported():
    with tempfile.TemporaryDirectory() as d:
        _write(
            os.path.join(d, "SKILL.md"),
            textwrap.dedent(
                """\
                ---
                name: sample
                description: sample skill
                ---
                See [topic](references/topic-selection.md).
                """
            ),
        )
        problems = validate_skill_dir(d)
        assert any("references/topic-selection.md" in p for p in problems)


_REQUIRED_SCRIPTS = [
    "content_db.py", "archive_content.py", "query_db.py",
    "update_metrics.py", "generate_cover.py", "fetch_hotlist.py",
    "web_search.py",
]


_REQUIRED_REFS = ["storyboard-plan.md", "workspace-guide.md"]


def _make_valid_skill(d, skill_md=None, with_paths=True):
    """Write a minimal skill dir that the validator accepts."""
    _write(
        os.path.join(d, "SKILL.md"),
        skill_md
        or textwrap.dedent(
            """\
            ---
            name: sample
            description: sample skill
            ---
            See [topic](references/topic-selection.md).
            """
        ),
    )
    _write(os.path.join(d, "references", "topic-selection.md"), "# topic\n")
    for name in _REQUIRED_REFS:
        _write(os.path.join(d, "references", name), "# %s\n" % name)
    # 校验器现在还要求产物自带一组必需脚本，合法样本得把它们补齐
    for name in _REQUIRED_SCRIPTS:
        _write(os.path.join(d, "scripts", name), "# stub\n")
    # 每个产物必带账号长记忆 workspace/huasheng.md
    _write(os.path.join(d, "workspace", "huasheng.md"), "# huasheng\n")
    if with_paths:
        _write(os.path.join(d, "scripts", "_paths.py"), "# stub\n")


def test_valid_skill_returns_no_problems():
    with tempfile.TemporaryDirectory() as d:
        _make_valid_skill(d)
        problems = validate_skill_dir(d)
        assert problems == []


def test_missing_storyboard_reference_reported():
    with tempfile.TemporaryDirectory() as d:
        _make_valid_skill(d)
        os.remove(os.path.join(d, "references", "storyboard-plan.md"))
        problems = validate_skill_dir(d)
        assert any("storyboard-plan.md" in p for p in problems)


def test_missing_huasheng_reported():
    with tempfile.TemporaryDirectory() as d:
        _make_valid_skill(d)
        os.remove(os.path.join(d, "workspace", "huasheng.md"))
        problems = validate_skill_dir(d)
        assert any("huasheng.md" in p for p in problems)


def test_data_tracking_without_paths_reported():
    with tempfile.TemporaryDirectory() as d:
        skill_md = textwrap.dedent(
            """\
            ---
            name: sample
            description: sample skill
            ---
            See [topic](references/topic-selection.md).
            See [data](references/data-tracking.md).
            """
        )
        _make_valid_skill(d, skill_md=skill_md, with_paths=False)
        _write(os.path.join(d, "references", "data-tracking.md"), "# data\n")
        problems = validate_skill_dir(d)
        assert any("_paths.py" in p for p in problems)

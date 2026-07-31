from pathlib import Path

from scripts.check_markdown import check_markdown, github_anchor


def test_github_anchor_normalizes_common_heading_markup():
    assert github_anchor("Routine `code` change: use this every time") == (
        "routine-code-change-use-this-every-time"
    )


def test_check_markdown_accepts_existing_target_and_anchor(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "[Guide](docs/guide.md#daily-refresh)\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\n## Daily refresh\n",
        encoding="utf-8",
    )

    assert check_markdown(tmp_path, [Path("README.md"), Path("docs")]) == []


def test_check_markdown_reports_missing_target_and_anchor(tmp_path):
    (tmp_path / "README.md").write_text(
        "[Missing](missing.md)\n[Section](README.md#missing)\n",
        encoding="utf-8",
    )

    errors = check_markdown(tmp_path, [Path("README.md")])

    assert len(errors) == 2
    assert "missing target" in errors[0]
    assert "missing anchor" in errors[1]

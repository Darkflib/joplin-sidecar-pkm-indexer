"""Tests for the Markdown task/link extractor (PRD §12, §18.7)."""

from pathlib import Path

import pytest

from pkm_sidecar.markdown_extract import classify_link, extract

FIXTURES = Path(__file__).parent.parent / "fixtures" / "notes"


class TestTasks:
    @pytest.mark.parametrize("bullet", ["-", "*", "+"])
    def test_bullets(self, bullet: str) -> None:
        tasks = extract("n", f"{bullet} [ ] do thing")["tasks"]
        assert len(tasks) == 1
        assert tasks[0].text == "do thing"
        assert tasks[0].checked is False

    @pytest.mark.parametrize(("mark", "checked"), [(" ", False), ("x", True), ("X", True)])
    def test_checked_states(self, mark: str, checked: bool) -> None:
        tasks = extract("n", f"- [{mark}] item")["tasks"]
        assert tasks[0].checked is checked

    def test_indented_task(self) -> None:
        tasks = extract("n", "  - [ ] indented")["tasks"]
        assert len(tasks) == 1

    def test_line_numbers_are_1_based(self) -> None:
        body = "line 1\n- [ ] task on line 2\n- [x] task on line 3"
        tasks = extract("n", body)["tasks"]
        assert [t.line_number for t in tasks] == [2, 3]

    def test_crlf_handling(self) -> None:
        tasks = extract("n", "- [ ] alpha\r\n- [x] beta\r\n")["tasks"]
        assert [t.text for t in tasks] == ["alpha", "beta"]
        assert "\r" not in tasks[0].raw_line

    def test_prd_example(self) -> None:
        # PRD §18.7
        body = "- [ ] Write PRD\n- [x] Confirm plugin spike"
        tasks = extract("n", body)["tasks"]
        assert len(tasks) == 2
        assert tasks[0].text == "Write PRD" and tasks[0].checked is False
        assert tasks[1].text == "Confirm plugin spike" and tasks[1].checked is True

    def test_not_a_task(self) -> None:
        assert extract("n", "- just a bullet, no checkbox")["tasks"] == []


class TestFences:
    def test_backtick_fence_suppresses_tasks(self) -> None:
        body = "```\n- [ ] inside fence\n```\n- [ ] outside"
        tasks = extract("n", body)["tasks"]
        assert [t.text for t in tasks] == ["outside"]

    def test_tilde_fence_suppresses_tasks(self) -> None:
        body = "~~~\n+ [x] inside\n~~~\n- [ ] outside"
        tasks = extract("n", body)["tasks"]
        assert [t.text for t in tasks] == ["outside"]

    def test_inline_code_suppresses_links(self) -> None:
        links = extract("n", "text `[fake](link)` more")["links"]
        assert links == []


class TestLinkClassification:
    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (":/0123456789abcdef0123456789abcdef", "internal_joplin"),
            ("https://example.com", "external_url"),
            ("http://localhost:8765/x", "external_url"),
            ("ftp://host/file", "external_url"),
            ("mailto:a@b.com", "other"),
            ("tel:+15551234", "other"),
            ("./docs/setup.md", "relative"),
            ("../up.md", "relative"),
            ("/abs/path", "relative"),
            ("#anchor", "relative"),
            (":/short", "relative"),  # not 32 hex -> not internal
        ],
    )
    def test_classify(self, target: str, expected: str) -> None:
        assert classify_link(target) == expected


class TestLinks:
    def test_basic_link(self) -> None:
        links = extract("n", "see [docs](https://x.com)")["links"]
        assert len(links) == 1
        assert links[0].link_text == "docs"
        assert links[0].target == "https://x.com"
        assert links[0].link_type == "external_url"

    def test_image_link_text_starts_with_bang(self) -> None:
        links = extract("n", "![alt text](./img.png)")["links"]
        assert len(links) == 1
        assert links[0].link_text == "!alt text"
        assert links[0].link_type == "relative"

    def test_task_line_with_link_emits_both(self) -> None:
        result = extract("n", "- [ ] read [spec](:/0123456789abcdef0123456789abcdef)")
        assert len(result["tasks"]) == 1
        assert len(result["links"]) == 1
        assert result["links"][0].link_type == "internal_joplin"

    def test_reference_and_wikilinks_not_extracted(self) -> None:
        # Documented v0.1 limitation.
        body = "[ref][1]\n[1]: https://example.com\n[[WikiLink]]"
        assert extract("n", body)["links"] == []


class TestFixtures:
    def test_with_tasks_fixture(self) -> None:
        body = (FIXTURES / "with_tasks.md").read_text()
        tasks = extract("n", body)["tasks"]
        assert len(tasks) == 5
        assert sum(1 for t in tasks if t.checked) == 2

    def test_with_links_fixture(self) -> None:
        body = (FIXTURES / "with_links.md").read_text()
        types = {lk.link_type for lk in extract("n", body)["links"]}
        assert types == {"internal_joplin", "external_url", "relative", "other"}

    def test_edge_cases_fixture(self) -> None:
        body = (FIXTURES / "edge_cases.md").read_text()
        result = extract("n", body)
        # Only the real task after the fences survives.
        assert len(result["tasks"]) == 1
        assert result["tasks"][0].text.startswith("Real task")
        # The internal link on that task line is the only extracted link.
        assert [lk.link_type for lk in result["links"]] == ["internal_joplin"]

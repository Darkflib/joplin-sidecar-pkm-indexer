"""Title generation: slug path, prompt shaping, output cleaning (docs/enrichment.md §5, §11.1)."""

import pytest

from pkm_sidecar.enrichment.title_generator import (
    BODY_HEAD_CHARS,
    BODY_TAIL_CHARS,
    best_url,
    build_prompt,
    clean_title,
    propose_title,
    slug_title,
    truncate_body,
)


class TestSlugTitle:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://www.tecmint.com/install-fail2ban-to-protect-ssh-on-centos-rhel/",
                "Install Fail2ban to Protect SSH on Centos RHEL",
            ),
            (
                "https://www.tecmint.com/free-linux-shell-scripting-books/",
                "Free Linux Shell Scripting Books",
            ),
            (
                "https://dzone.com/articles/learn-how-to-use-php-to-create-microservices",
                "Learn How to Use PHP to Create Microservices",
            ),
        ],
    )
    def test_real_slugs(self, url: str, expected: str) -> None:
        assert slug_title(url) == expected

    def test_acronyms_survive_and_pluralise(self) -> None:
        assert slug_title("https://x.dev/public-rest-apis-for-you") == "Public REST APIs for You"

    def test_numeric_ids_are_dropped(self) -> None:
        assert slug_title("https://linux.softpedia.com/get/Distros/SparkyLinux-Xfce-103288") == (
            "SparkyLinux Xfce"
        )

    def test_noise_segments_are_skipped(self) -> None:
        assert slug_title("https://site.example/blog/posts/the-death-of-disk/amp") == (
            "The Death of Disk"
        )

    def test_query_carries_the_title(self) -> None:
        """§11.6: a surviving share.google link resolves to a search URL."""
        assert slug_title("https://www.google.com/search?q=Stratford+Computer+Fair") == (
            "Stratford Computer Fair"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://share.google/crvtpycsURVHBBJTg",
            "https://youtu.be/gkTBVkYnRWQ",
            "https://news.google.com/topics/CAAqJQgKIh9DQkFTRVFvSUwyMHZNRGR6",
            "http://requestmap.webperf.tools/",
            "https://1337x.to/sub/52/0/",
        ],
    )
    def test_opaque_urls_decline_rather_than_guess(self, url: str) -> None:
        """Inventing a title from an id is worse than leaving it to a human."""
        assert slug_title(url) is None

    def test_credential_bearing_urls_are_refused(self) -> None:
        """Keeps a live key out of a suggestion row, and the words are not there anyway."""
        assert slug_title("http://api.weatherapi.com/v1/current.json?key=abc123def456") is None

    def test_non_http_schemes_declined(self) -> None:
        assert slug_title("file:///etc/passwd") is None
        assert slug_title("joplin://x-callback-url/openNote?id=abc") is None


class TestBestUrl:
    def test_body_url_beats_a_truncated_title(self) -> None:
        """Joplin cuts auto-titles at 80 chars, so the title's URL is often a fragment."""
        full = (
            "https://superuser.com/questions/792049/"
            "ffmpeg-m2ts-mkv-avi-mp4-w-tri-audio-and-subtitles"
        )
        truncated = full[:80]
        assert best_url(truncated, full) == full
        assert slug_title(best_url(truncated, full)).endswith("Subtitles")

    def test_title_used_when_body_has_no_url(self) -> None:
        url = "https://example.com/a-real-slug-here"
        assert best_url(url, "no links in this body") == url

    def test_unrelated_body_url_does_not_hijack_the_title(self) -> None:
        title = "https://example.com/the-actual-bookmark"
        assert best_url(title, "see also https://other.example/something-else") == title

    def test_no_url_anywhere(self) -> None:
        assert best_url("Just a title", "just a body") is None


class TestTruncateBody:
    def test_short_body_untouched(self) -> None:
        assert truncate_body("  short note  ") == "short note"

    def test_long_body_keeps_both_ends(self) -> None:
        body = "A" * 3000 + "ZZZEND"
        out = truncate_body(body)
        assert out.startswith("A" * 100)
        assert out.endswith("ZZZEND")
        assert len(out) < len(body)
        assert len(out) <= BODY_HEAD_CHARS + BODY_TAIL_CHARS + 10

    def test_prompt_contains_the_body_and_the_rules(self) -> None:
        prompt = build_prompt("RabbitMQ ack ordering")
        assert "RabbitMQ ack ordering" in prompt
        assert "No quotes" in prompt


class TestCleanTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('"Podman Quadlet Notes"', "Podman Quadlet Notes"),
            ("“Smart Quoted Title”", "Smart Quoted Title"),
            ("'Single Quoted'", "Single Quoted"),
            ("`Backticked Title`", "Backticked Title"),
            ("Title: OpenBao Key Rotation", "OpenBao Key Rotation"),
            ("Suggested title - Ledger Idempotency", "Ledger Idempotency"),
            ("A Title With A Full Stop.", "A Title With A Full Stop"),
            ("  spaced    out   title  ", "spaced out title"),
            ("First Line Is The Title\nrambling second line", "First Line Is The Title"),
        ],
    )
    def test_models_ignore_the_format_rules(self, raw: str, expected: str) -> None:
        """Measured: the small models emitted quotes in 3 of 5 cases despite the prompt."""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "\n\n"])
    def test_empty_is_unusable(self, raw: str) -> None:
        assert clean_title(raw) is None

    def test_rambling_answer_is_rejected(self) -> None:
        assert clean_title(" ".join(["word"] * 40)) is None

    def test_over_long_answer_is_rejected(self) -> None:
        assert clean_title("x" * 300) is None

    def test_proposing_the_existing_title_is_noise(self) -> None:
        assert clean_title("Ledger Idempotency", current_title="ledger idempotency") is None

    def test_a_genuinely_different_title_passes(self) -> None:
        assert clean_title("Ledger Idempotency", current_title="Untitled") == "Ledger Idempotency"


class TestRouting:
    def test_url_note_takes_the_offline_path(self) -> None:
        title, source = propose_title(
            issue="url",
            title="https://www.tecmint.com/free-linux-shell-scripting-books/",
            body="https://www.tecmint.com/free-linux-shell-scripting-books/",
        )
        assert source == "slug"
        assert title == "Free Linux Shell Scripting Books"

    def test_opaque_url_falls_through_to_the_model(self) -> None:
        title, source = propose_title(
            issue="url", title="https://share.google/crvtpycs", body="https://share.google/crvtpycs"
        )
        assert (title, source) == (None, "model")

    def test_prose_note_goes_straight_to_the_model(self) -> None:
        title, source = propose_title(issue="empty", title="", body="Some real prose here.")
        assert (title, source) == (None, "model")

    def test_dead_link_still_yields_a_title(self) -> None:
        """The whole point of slug-first: no network, and the host no longer exists."""
        title, source = propose_title(
            issue="url",
            title="http://www.smartjava.org/content/all-learning-three-js-third-edition-examples-online",
            body="http://www.smartjava.org/content/all-learning-three-js-third-edition-examples-online",
        )
        assert source == "slug"
        assert "Learning Three Js" in title


class TestReviewHardening:
    """Four edge cases from review, each a confident-nonsense risk."""

    def test_markdown_autolink_brackets_are_not_part_of_the_url(self) -> None:
        """`<https://…>` would otherwise yield a title ending in '>'."""
        url = best_url("", "see <https://example.com/the-right-story> for detail")
        assert url == "https://example.com/the-right-story"
        assert slug_title(url) == "The Right Story"

    @pytest.mark.parametrize(
        "body",
        [
            "ref [1]: https://example.com/the-right-story]",
            "see https://example.com/the-right-story).",
            "(https://example.com/the-right-story);",
        ],
    )
    def test_surrounding_punctuation_is_stripped(self, body: str) -> None:
        assert slug_title(best_url("", body)) == "The Right Story"

    def test_bare_domain_title_is_not_hijacked_by_an_unrelated_body_link(self) -> None:
        """Without a scheme the title never matched, so any body link won."""
        url = best_url(
            "www.example.com/the-real-article",
            "also see https://other.example/a-completely-different-topic",
        )
        assert "other.example" not in url
        assert slug_title(url) == "The Real Article"

    def test_mixed_case_opaque_ids_are_refused(self) -> None:
        assert slug_title("https://share.google/crvtpycsURVHBBJTg") is None
        assert slug_title("https://x.io/XwN0WKClyMFUbFSUO") is None

    def test_camelcase_slugs_still_work(self) -> None:
        """The id rule must not swallow a real CamelCase path segment."""
        assert slug_title("https://linux.softpedia.com/get/D/SparkyLinux-Xfce-103288") == (
            "SparkyLinux Xfce"
        )

    def test_digit_bearing_slugs_still_work(self) -> None:
        """'fail2ban' has a digit; an over-eager id rule swallowed this whole slug."""
        assert slug_title("https://www.tecmint.com/install-fail2ban-to-protect-ssh-on-centos") == (
            "Install Fail2ban to Protect SSH on Centos"
        )

    def test_overlong_slug_is_cut_at_a_word_boundary(self) -> None:
        """Slicing mid-word would recreate the truncation defect being repaired."""
        title = slug_title("https://x.io/" + "-".join(["verylongwordindeed"] * 9))
        assert title is not None
        assert not title.endswith("verylongwordi")  # no severed word
        assert title.split()[-1] == "Verylongwordindeed"

    def test_overlong_single_word_declines_rather_than_truncating(self) -> None:
        assert slug_title("https://x.io/" + "a" * 200 + "-b") is None

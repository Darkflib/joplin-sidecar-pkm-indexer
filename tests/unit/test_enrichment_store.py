"""Enrichment store: identity, generations, decisions, embeddings (docs/enrichment.md §4)."""

from pathlib import Path

import pytest

from pkm_sidecar.enrichment import db as edb
from pkm_sidecar.enrichment.models import Suggestion
from pkm_sidecar.enrichment.repository import SuggestionRepository, compute_input_hash


@pytest.fixture
def repo(tmp_path: Path):
    path = tmp_path / "data" / "suggestions.sqlite3"
    edb.init_db(path)
    conn = edb.open_connection(path)
    yield SuggestionRepository(conn)
    conn.close()


def _title(note_id: str, title: str, *, body_hash: str = "bh1", model: str = "m1", **kw):
    return Suggestion(
        note_id=note_id,
        kind="title",
        input_hash=compute_input_hash(
            kind="title",
            body_hash=body_hash,
            current_state=kw.pop("current_title", ""),
            model=model,
        ),
        payload={"title": title},
        note_body_hash=body_hash,
        model=model,
        **kw,
    )


class TestInputHash:
    def test_body_change_changes_identity(self) -> None:
        a = compute_input_hash(kind="title", body_hash="a", current_state="T", model="m")
        b = compute_input_hash(kind="title", body_hash="b", current_state="T", model="m")
        assert a != b

    def test_retitle_changes_identity(self) -> None:
        """The defect this exists to prevent: body_hash alone misses a retitle."""
        a = compute_input_hash(kind="title", body_hash="same", current_state="Old", model="m")
        b = compute_input_hash(kind="title", body_hash="same", current_state="New", model="m")
        assert a != b

    def test_model_change_changes_identity(self) -> None:
        """Without this the documented second-pass model could not store a row."""
        a = compute_input_hash(kind="title", body_hash="x", current_state="T", model="llama3.1:8b")
        b = compute_input_hash(kind="title", body_hash="x", current_state="T", model="gpt-oss:20b")
        assert a != b

    def test_prompt_version_changes_identity(self) -> None:
        a = compute_input_hash(
            kind="title", body_hash="x", current_state="", model="m", prompt_version=1
        )
        b = compute_input_hash(
            kind="title", body_hash="x", current_state="", model="m", prompt_version=2
        )
        assert a != b

    def test_tag_order_does_not_matter(self) -> None:
        a = compute_input_hash(kind="tags", body_hash="x", current_state=["b", "a"], model="m")
        b = compute_input_hash(kind="tags", body_hash="x", current_state=["a", "b"], model="m")
        assert a == b

    def test_kind_separates_identities(self) -> None:
        a = compute_input_hash(kind="title", body_hash="x", current_state="", model="m")
        b = compute_input_hash(kind="tags", body_hash="x", current_state="", model="m")
        assert a != b


class TestRecordAndDecide:
    def test_record_then_read_back(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            stored = repo.record(_title("n1", "A Better Title"))
        assert stored.id is not None
        assert repo.current_for_note("n1", "title").payload == {"title": "A Better Title"}

    def test_same_identity_is_idempotent(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            first = repo.record(_title("n1", "A Better Title"))
            again = repo.record(_title("n1", "A Better Title"))
        assert again.id == first.id
        assert len(repo.pending("title")) == 1

    def test_pending_excludes_decided(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            s = repo.record(_title("n1", "Title"))
        assert len(repo.pending("title")) == 1
        with repo.transaction():
            repo.decide(s.id, "accepted")
        assert repo.pending("title") == []

    def test_pending_excludes_opted_out_notes(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            repo.record(_title("n1", "Title"))
            repo.opt_out("n1", "title")
        assert repo.pending("title") == []
        with repo.transaction():
            repo.clear_opt_out("n1", "title")
        assert len(repo.pending("title")) == 1

    def test_pending_orders_by_confidence(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            repo.record(_title("n1", "Low", body_hash="b1", confidence=0.2))
            repo.record(_title("n2", "High", body_hash="b2", confidence=0.9))
            repo.record(_title("n3", "None", body_hash="b3"))
        assert [s.payload["title"] for s in repo.pending("title")] == ["High", "Low", "None"]


class TestGenerations:
    def test_supersede_hides_the_old_row(self, repo: SuggestionRepository) -> None:
        """A corpus re-queue must let both rows exist, and offer only the newest."""
        with repo.transaction():
            first = repo.record(_title("n1", "First"))
            gen = repo.next_generation("n1", "title", first.input_hash)
            second = repo.record(_title("n1", "Second").model_copy(update={"generation": gen}))
            repo.supersede(first.id, second.id)

        assert gen == 2
        pending = repo.pending("title")
        assert len(pending) == 1
        assert pending[0].payload == {"title": "Second"}
        assert repo.current_for_note("n1", "title").generation == 2

    def test_rejected_payload_is_not_re_asked(self, repo: SuggestionRepository) -> None:
        """Corpus churn must not re-ask a question that was already settled."""
        with repo.transaction():
            first = repo.record(_title("n1", "Same Title"))
            repo.decide(first.id, "rejected")
            gen = repo.next_generation("n1", "title", first.input_hash)
            again = repo.record(_title("n1", "Same Title").model_copy(update={"generation": gen}))

        assert again.decision == "rejected"  # inherited, not asked again
        assert repo.pending("title") == []

    def test_a_different_payload_is_still_offered(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            first = repo.record(_title("n1", "Rejected One"))
            repo.decide(first.id, "rejected")
            gen = repo.next_generation("n1", "title", first.input_hash)
            repo.record(_title("n1", "A Fresh Idea").model_copy(update={"generation": gen}))
        assert [s.payload["title"] for s in repo.pending("title")] == ["A Fresh Idea"]


class TestCorpusStaleness:
    def test_drifted_rows_are_flagged_but_still_served(self, repo: SuggestionRepository) -> None:
        s = Suggestion(
            note_id="n1",
            kind="tags",
            input_hash=compute_input_hash(kind="tags", body_hash="bh", current_state=[], model="m"),
            payload={"tags": ["t1"]},
            note_body_hash="bh",
            model="m",
            corpus_revision="rev-1",
        )
        with repo.transaction():
            repo.record(s)
            flagged = repo.mark_stale_for_corpus("tags", "rev-2")

        assert flagged == 1
        assert len(repo.stale_suggestions("tags")) == 1
        # Stale asks for regeneration; it does not hide a probably-still-right answer.
        assert len(repo.pending("tags")) == 1


class TestEmbeddings:
    def test_roundtrip(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            repo.put_embedding("n1", model="bge", body_hash="bh", vector=[0.5, -0.25, 0.125])
        got = repo.get_embedding("n1", model="bge", body_hash="bh")
        assert got == pytest.approx([0.5, -0.25, 0.125])

    def test_model_change_is_a_cache_miss(self, repo: SuggestionRepository) -> None:
        """Vectors from different models are not comparable; reuse would corrupt kNN."""
        with repo.transaction():
            repo.put_embedding("n1", model="bge", body_hash="bh", vector=[1.0, 0.0])
        assert repo.get_embedding("n1", model="nomic", body_hash="bh") is None

    def test_body_change_is_a_cache_miss(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            repo.put_embedding("n1", model="bge", body_hash="old", vector=[1.0, 0.0])
        assert repo.get_embedding("n1", model="bge", body_hash="new") is None

    def test_two_models_coexist_for_one_note(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            repo.put_embedding("n1", model="bge", body_hash="bh", vector=[1.0, 0.0])
            repo.put_embedding("n1", model="nomic", body_hash="bh", vector=[0.0, 1.0])
        assert repo.get_embedding("n1", model="bge", body_hash="bh") == pytest.approx([1.0, 0.0])
        assert repo.get_embedding("n1", model="nomic", body_hash="bh") == pytest.approx([0.0, 1.0])
        assert repo.count_embeddings(model="bge") == 1


class TestSchemaGuard:
    def test_version_is_checked_before_the_schema_is_applied(self, tmp_path: Path) -> None:
        """A mismatched store is refused untouched, not partially upgraded first.

        The index's init_db has this the other way round and grafts the new
        tables on before refusing; this store does not repeat that.
        """
        import sqlite3

        from pkm_sidecar.errors import SchemaVersionMismatchError

        path = tmp_path / "suggestions.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema.version', '99')")
        conn.commit()
        conn.close()

        with pytest.raises(SchemaVersionMismatchError):
            edb.init_db(path)

        check = sqlite3.connect(path)
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check.close()
        assert "suggestions" not in tables  # nothing was created on the way out
        assert tables == {"meta"}

    def test_init_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "suggestions.sqlite3"
        edb.init_db(path)
        edb.init_db(path)
        conn = edb.open_connection(path)
        try:
            tables = {
                r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        assert {"suggestions", "opt_outs", "note_embeddings", "meta"} <= tables


class TestDefaultPath:
    def test_store_sits_beside_the_index(self, tmp_path: Path) -> None:
        assert edb.default_path(tmp_path / "data" / "index.sqlite3") == (
            tmp_path / "data" / "suggestions.sqlite3"
        )


class TestRejectionInheritanceScope:
    """Inheritance is for corpus re-queues, not a blanket veto on a payload."""

    def test_inherits_across_generations_of_one_identity(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            first = repo.record(_title("n1", "Same Title"))
            repo.decide(first.id, "rejected")
            gen = repo.next_generation("n1", "title", first.input_hash)
            again = repo.record(_title("n1", "Same Title").model_copy(update={"generation": gen}))
        assert again.decision == "rejected"

    def test_does_not_inherit_after_the_body_changes(self, repo: SuggestionRepository) -> None:
        """A new identity means the question is asked again — that is the point."""
        with repo.transaction():
            first = repo.record(_title("n1", "Same Title", body_hash="bh1"))
            repo.decide(first.id, "rejected")
            after_edit = repo.record(_title("n1", "Same Title", body_hash="bh2"))

        assert after_edit.input_hash != first.input_hash
        assert after_edit.decision is None
        assert [s.payload["title"] for s in repo.pending("title")] == ["Same Title"]

    def test_does_not_inherit_after_a_model_change(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            first = repo.record(_title("n1", "Same Title", model="llama3.1:8b"))
            repo.decide(first.id, "rejected")
            second_pass = repo.record(_title("n1", "Same Title", model="gpt-oss:20b"))
        assert second_pass.decision is None

    def test_rejection_does_not_leak_between_notes(self, repo: SuggestionRepository) -> None:
        with repo.transaction():
            first = repo.record(_title("n1", "Shared Title"))
            repo.decide(first.id, "rejected")
            other = repo.record(_title("n2", "Shared Title"))
        assert other.decision is None

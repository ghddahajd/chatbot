"""backend/scripts/policy_snapshot.py — сеть безопасности перед правкой правил.

Чистая логика (корпус, сравнение, ожидания) проверяется в процессе. Сам прогон — только в отдельном
процессе, как в реальном запуске: он глобально подменяет random.choice и часы, и эти подмены не
должны протекать в остальные тесты."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

import policy_snapshot as snap  # noqa: E402


# ---------------------------------------------------------------- корпус


def test_message_id_ignores_whitespace_and_is_stable() -> None:
    assert snap.message_id("сколько  стоит\nчистка") == snap.message_id("сколько стоит чистка")
    assert snap.message_id("а") != snap.message_id("б")
    assert snap.message_id("привет") == "m-" + snap.hashlib.sha1("привет".encode()).hexdigest()[:12]


def test_corpus_builder_dedups_and_keeps_all_sources() -> None:
    builder = snap.CorpusBuilder()
    builder.add("привет", "evals:a")
    builder.add(" привет ", "tests")
    builder.add("привет", "tests")
    builder.add("x", "tests")  # слишком короткое
    builder.add(None, "tests")

    assert len(builder.cases) == 1
    case = next(iter(builder.cases.values()))
    assert case["sources"] == ["evals:a", "tests"]


def test_strings_under_keys_walks_nested_structures() -> None:
    payload = {"topics": [{"keywords": ["хламид", "иппп"], "text": "t"}], "fact_guards": {"triggers": "ботокс"}}

    assert snap._strings_under_keys(payload, ("keyword", "trigger")) == ["хламид", "иппп", "ботокс"]


def test_messages_from_tests_takes_call_args_and_message_values(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        'def test_x():\n'
        '    """документация, не сообщение"""\n'
        '    _chat(client, "сколько стоит чистка")\n'
        '    post(json={"message": "позовите оператора"})\n'
        '    analyze(message="у меня болит голова")\n'
        '    helper("latin only")\n',
        encoding="utf-8",
    )

    found = snap._messages_from_tests(tmp_path)

    assert set(found) == {"сколько стоит чистка", "позовите оператора", "у меня болит голова"}


def test_contexts_only_for_short_messages() -> None:
    assert snap.contexts_for("да") == snap.CONTEXTS
    assert snap.contexts_for("слово " * (snap.FOLLOWUP_MAX_WORDS + 1)) == ("fresh",)


# ---------------------------------------------------------------- сравнение и ожидания


def _row(case_id: str, context: str = "fresh", time: str = "day", **fields) -> dict:
    base = {"id": case_id, "context": context, "time": time, "action": "answer", "reason": "ok", "error": None}
    base.update(fields)
    return base


def test_diff_results_finds_changed_fields_and_missing_rows() -> None:
    base = [_row("m-1"), _row("m-2"), _row("m-3", context="offered_operator")]
    head = [_row("m-1"), _row("m-2", reason="regulated_advice"), _row("m-4")]

    diff = snap.diff_results(base, head, {"m-2": "у меня болит"})

    assert diff["compared"] == 2
    assert len(diff["changed"]) == 1
    change = diff["changed"][0]
    assert change["fields"] == ["reason"]
    assert change["before"] == {"reason": "ok"} and change["after"] == {"reason": "regulated_advice"}
    assert change["transition"] == "answer/ok → answer/regulated_advice"
    assert change["message"] == "у меня болит"
    assert diff["only_base"] == ["m-3@offered_operator@day"]
    assert diff["only_head"] == ["m-4@fresh@day"]


def test_diff_counts_errors_per_side() -> None:
    diff = snap.diff_results([_row("m-1")], [_row("m-1", error="ValueError: x")], {})

    assert diff["errors_base"] == 0 and diff["errors_head"] == 1
    assert diff["changed"][0]["fields"] == ["error"]


@pytest.mark.parametrize(
    ("expected", "matches"),
    [
        ({"m-1"}, True),
        ({"m-1@offered_operator"}, True),
        ({"m-1@offered_operator@night"}, True),
        ({"m-1@fresh"}, False),
        ({"m-1@offered_operator@day"}, False),
        (set(), False),
    ],
)
def test_expectation_granularity(expected, matches) -> None:
    change = {"id": "m-1", "context": "offered_operator", "time": "night"}

    assert snap._expected(change, expected) is matches


def test_load_expectations_accepts_object_or_list(tmp_path: Path) -> None:
    as_object = tmp_path / "a.json"
    as_object.write_text(json.dumps({"ids": ["m-1", "m-2@fresh"], "note": "перенос правила"}), encoding="utf-8")
    as_list = tmp_path / "b.json"
    as_list.write_text(json.dumps(["m-3"]), encoding="utf-8")

    assert snap.load_expectations(as_object) == {"m-1", "m-2@fresh"}
    assert snap.load_expectations(as_list) == {"m-3"}
    assert snap.load_expectations(None) == set()


def test_report_marks_expected_and_unexpected() -> None:
    diff = snap.diff_results(
        [_row("m-1"), _row("m-2")],
        [_row("m-1", action="clarify"), _row("m-2", action="clarify")],
        {"m-1": "первое", "m-2": "второе"},
    )
    meta = {
        "base_label": "base", "head_label": "head", "company_id": "c", "data_commit": "abc",
        "corpus_size": 2, "times": ["day"], "base_seconds": 1, "head_seconds": 1,
    }

    report = snap.render_report(diff, {"m-1"}, meta)

    assert "изменилось: 2, из них неожиданных: 1" in report
    assert "✓ `m-1@fresh@day`" in report and "✗ `m-2@fresh@day`" in report


def test_report_without_changes() -> None:
    diff = snap.diff_results([_row("m-1")], [_row("m-1")], {})
    meta = {
        "base_label": "b", "head_label": "h", "company_id": "c", "data_commit": "abc",
        "corpus_size": 1, "times": ["day"], "base_seconds": 1, "head_seconds": 1,
    }

    assert "Различий нет." in snap.render_report(diff, set(), meta)


# ---------------------------------------------------------------- прогон (в отдельном процессе)


def _run_child(clients_dir: Path, corpus: Path, out: Path) -> list[dict]:
    env = snap._child_env(clients_dir)
    subprocess.run(
        [
            sys.executable, str(snap.SCRIPT_PATH), "_run",
            "--repo-dir", str(REPO_DIR), "--clients-dir", str(clients_dir), "--eval-dir", str(clients_dir / "_no_eval"),
            "--company", "rosh_demo", "--corpus", str(corpus), "--times", "day,night", "--out", str(out),
        ],
        env=env,
        check=True,
        timeout=180,
    )
    return snap._read_jsonl(out)


def test_run_is_deterministic_and_covers_contexts_and_times(managed_env, tmp_path: Path) -> None:
    messages = ["здравствуйте", "сколько стоит чистка лица", "позовите оператора", "да", "у меня болит живот что принять"]
    corpus = tmp_path / "corpus.jsonl"
    snap._write_jsonl(corpus, [{"id": snap.message_id(m), "message": m, "sources": ["test"]} for m in messages])

    first = _run_child(managed_env["clients_dir"], corpus, tmp_path / "a.jsonl")
    second = _run_child(managed_env["clients_dir"], corpus, tmp_path / "b.jsonl")

    assert first == second
    assert not [row for row in first if row.get("error")]
    per_message = len(snap.CONTEXTS) * 2
    assert len(first) == len(messages) * per_message  # все сообщения короткие: все контексты, день и ночь
    assert {row["context"] for row in first} == set(snap.CONTEXTS)
    assert {row["time"] for row in first} == {"day", "night"}
    operator = next(r for r in first if r["id"] == snap.message_id("позовите оператора") and r["context"] == "fresh")
    assert operator["reason"] == "operator_requested"
    assert set(operator) >= set(snap.COMPARED_FIELDS)


def test_state_matters_for_short_follow_up(managed_env, tmp_path: Path) -> None:
    """«да» после предложения оператора и «да» в пустом диалоге — разные решения; ради этого контексты и нужны."""

    corpus = tmp_path / "corpus.jsonl"
    snap._write_jsonl(corpus, [{"id": "m-yes", "message": "да", "sources": ["test"]}])

    rows = _run_child(managed_env["clients_dir"], corpus, tmp_path / "out.jsonl")

    by_context = {row["context"]: row for row in rows if row["time"] == "day"}

    def answer(row: dict) -> tuple:
        # решение (действие/причина) может совпадать — различаются текст и кнопки; так же 22.09 перестановка
        # двух правил проявилась только в тексте и кнопках, поэтому сеть сравнивает полный ответ
        return row["action"], row["reason"], row["message_to_user"], json.dumps(row["quick_actions"], ensure_ascii=False)

    assert answer(by_context["fresh"]) != answer(by_context["offered_operator"])


# ---------------------------------------------------------------- отпечаток контекста


def test_context_hash_ignores_unread_phrasebook_keys_but_not_price_disclaimer() -> None:
    """23.09: две новые фразы в словаре дали 15 тыс. ложных различий — словарь целиком модель не видит."""

    base = {"service": {"name": "Чистки"}, "phrasebook": {"price_disclaimer": "Цена предварительная", "greeting": "Привет"}}
    new_phrase = {**base, "phrasebook": {**base["phrasebook"], "clinic_contacts": "Телефон {phone}"}}
    new_disclaimer = {**base, "phrasebook": {**base["phrasebook"], "price_disclaimer": "Другая оговорка"}}
    new_field = {**base, "service": {"name": "Пилинги"}}

    assert snap._context_hash(base) == snap._context_hash(new_phrase)
    assert snap._context_hash(base) != snap._context_hash(new_disclaimer)
    assert snap._context_hash(base) != snap._context_hash(new_field)


def test_llm_input_hash_uses_the_versions_own_context_builder() -> None:
    class FakeClient:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def _context_for_model(self, context: dict) -> str:
            return f"Услуга: {context['service']['name']}"

    class BrokenClient:
        def __init__(self, api_key: str) -> None:
            raise TypeError("old signature")

    first = snap._llm_input_hash(FakeClient, {"service": {"name": "Чистки"}, "phrasebook": {"x": 1}})
    same_model_input = snap._llm_input_hash(FakeClient, {"service": {"name": "Чистки"}, "phrasebook": {"y": 2}})
    other = snap._llm_input_hash(FakeClient, {"service": {"name": "Пилинги"}})

    assert first == same_model_input and first != other
    assert snap._llm_input_hash(BrokenClient, {}) == "error:TypeError"
    assert snap._llm_input_hash(None, {}) is None

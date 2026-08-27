"""проверки чистки message_answered в analytics.jsonl (archive_old_analytics_events)."""

from datetime import datetime, timedelta

from app.analytics import AnalyticsService, archive_old_analytics_events
from app.utils.jsonl import append_jsonl, read_jsonl


def _event(
    *,
    event_type: str,
    timestamp: datetime,
    company_id: str = "rosh_import_demo",
    action: str | None = "answer",
    message: str = "some message text",
) -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "company_id": company_id,
        "session_id": "s-1",
        "event_type": event_type,
        "message": message,
        "metadata": {"action": action} if action is not None else {},
    }


def test_archive_prunes_only_message_answered_past_retention(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    now = datetime.utcnow()
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=now - timedelta(days=90)))
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=now - timedelta(days=5)))

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 1
    remaining = read_jsonl(analytics_file)
    assert len(remaining) == 1
    assert remaining[0]["event_type"] == "message_answered"
    assert datetime.fromisoformat(remaining[0]["timestamp"]) > now - timedelta(days=10)


def test_archive_never_touches_other_event_types_regardless_of_age(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    now = datetime.utcnow()
    for event_type in ("unknown_question", "regulated_handoff", "operator_requested"):
        append_jsonl(
            analytics_file,
            _event(event_type=event_type, timestamp=now - timedelta(days=400), action=None),
        )

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 0
    remaining = read_jsonl(analytics_file)
    assert {item["event_type"] for item in remaining} == {
        "unknown_question",
        "regulated_handoff",
        "operator_requested",
    }
    assert not rollup_file.exists()


def test_archive_rolls_up_counts_by_date_company_and_action_without_message_text(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    stale_day = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(
        analytics_file,
        _event(event_type="message_answered", timestamp=stale_day, company_id="rosh_import_demo", action="answer"),
    )
    append_jsonl(
        analytics_file,
        _event(event_type="message_answered", timestamp=stale_day, company_id="rosh_import_demo", action="answer"),
    )
    append_jsonl(
        analytics_file,
        _event(event_type="message_answered", timestamp=stale_day, company_id="rosh_import_demo", action="clarify"),
    )

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 3
    rollup_entries = read_jsonl(rollup_file)
    by_action = {entry["action"]: entry["count"] for entry in rollup_entries}
    assert by_action == {"answer": 2, "clarify": 1}
    for entry in rollup_entries:
        assert "message" not in entry
        assert entry["date"] == "2026-01-05"
        assert entry["company_id"] == "rosh_import_demo"


def test_archive_noop_when_nothing_is_stale(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=datetime.utcnow()))

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 0
    assert not rollup_file.exists()
    assert len(read_jsonl(analytics_file)) == 1


def test_archive_missing_file_is_noop(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 0
    assert not rollup_file.exists()


def test_archive_keeps_message_answered_with_unparseable_timestamp(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    append_jsonl(analytics_file, {"event_type": "message_answered", "timestamp": "not-a-date", "message": "x"})

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 0
    assert len(read_jsonl(analytics_file)) == 1


def _claim_event(*, session_id: str, claimed_by: str, timestamp: datetime, company_id: str = "rosh_import_demo") -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "company_id": company_id,
        "session_id": session_id,
        "event_type": "operator_claimed",
        "message": None,
        "metadata": {"claimed_by": claimed_by},
    }


def _closed_event(*, session_id: str, claimed_by: str, timestamp: datetime, company_id: str = "rosh_import_demo") -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "company_id": company_id,
        "session_id": session_id,
        "event_type": "operator_closed",
        "message": None,
        "metadata": {"claimed_by": claimed_by},
    }


def _lead(*, session_id: str, company_id: str = "rosh_import_demo", timestamp: datetime | None = None) -> dict:
    return {
        "timestamp": (timestamp or datetime.utcnow()).isoformat(),
        "company_id": company_id,
        "session_id": session_id,
        "name": "Иван",
        "phone": "+79990000000",
        "summary": "",
        "service_id": None,
    }


def test_operator_summary_counts_claims_closes_and_avg_duration(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=t0))
    append_jsonl(analytics_file, _closed_event(session_id="s-1", claimed_by="masha", timestamp=t0 + timedelta(minutes=10)))
    append_jsonl(analytics_file, _claim_event(session_id="s-2", claimed_by="masha", timestamp=t0))
    append_jsonl(analytics_file, _closed_event(session_id="s-2", claimed_by="masha", timestamp=t0 + timedelta(minutes=20)))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary()

    assert result["operators"]["masha"]["claimed"] == 2
    assert result["operators"]["masha"]["closed"] == 2
    assert result["operators"]["masha"]["avg_dialog_minutes"] == 15.0


def test_operator_summary_attributes_leads_to_claiming_operator(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=t0))
    append_jsonl(leads_file, _lead(session_id="s-1"))
    # лид без клейма (бот сам собрал контакт, оператор не подключался) — не должен никому
    # приписаться
    append_jsonl(leads_file, _lead(session_id="s-unclaimed"))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary()

    assert result["operators"]["masha"]["leads"] == 1


def test_operator_summary_days_filter_still_attributes_lead_and_close_outside_window(tmp_path) -> None:
    """Живой баг (код-ревью, 2026-08-27): claim внутри days-окна, но лид/закрытие той же
    сессии легли СНАРУЖИ окна (нормальная ситуация на границе — claim под конец окна, лид/
    close чуть позже) — раньше молча терялись из "лидов"/"закрыто", хотя claim честно
    посчитан. Джойн по session_id должен работать независимо от того, где именно во времени
    легли сами лид/закрытие — days фильтрует только то, какие claim'ы вообще "в отчёте"."""

    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    now = datetime.utcnow()
    claimed_at = now - timedelta(days=2)  # внутри days=7
    closed_at = now - timedelta(days=10)  # СНАРУЖИ days=7 (часы сдвинулись, редкий, но возможный случай)
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=claimed_at))
    append_jsonl(analytics_file, _closed_event(session_id="s-1", claimed_by="masha", timestamp=closed_at))
    append_jsonl(leads_file, _lead(session_id="s-1", timestamp=now - timedelta(days=9)))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary(days=7)

    assert result["operators"]["masha"]["claimed"] == 1
    assert result["operators"]["masha"]["closed"] == 1
    assert result["operators"]["masha"]["leads"] == 1


def test_operator_summary_claim_without_close_has_no_avg_duration(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="petya", timestamp=datetime.utcnow()))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary()

    assert result["operators"]["petya"]["claimed"] == 1
    assert result["operators"]["petya"]["closed"] == 0
    assert result["operators"]["petya"]["avg_dialog_minutes"] is None


def test_operator_summary_ignores_close_before_claim_for_duration(tmp_path) -> None:
    """Не должно случаться в норме (сессию нельзя переклеймить, не закрыв), но если в данных
    почему-то оказалась пара claim/close с closed_at РАНЬШЕ claimed_at — не должны рисовать
    отрицательное "среднее время диалога", это разрушает доверие к дашборду."""

    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="petya", timestamp=t0))
    append_jsonl(analytics_file, _closed_event(session_id="s-1", claimed_by="petya", timestamp=t0 - timedelta(minutes=30)))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary()

    assert result["operators"]["petya"]["closed"] == 1  # закрытие само по себе честно посчитано
    assert result["operators"]["petya"]["avg_dialog_minutes"] is None  # но не отрицательное среднее


def test_operator_summary_filters_by_company_id(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime.utcnow()
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=t0, company_id="rosh_import_demo"))
    append_jsonl(analytics_file, _claim_event(session_id="s-2", claimed_by="petya", timestamp=t0, company_id="other_company"))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary(company_id="rosh_import_demo")

    assert list(result["operators"].keys()) == ["masha"]


def test_leads_by_month_includes_empty_months_and_is_chronological(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    now = datetime.utcnow()
    append_jsonl(leads_file, _lead(session_id="s-1"))  # текущий месяц (timestamp = now по умолчанию)
    two_months_ago = now.replace(day=1) - timedelta(days=40)
    append_jsonl(
        leads_file,
        {
            "timestamp": two_months_ago.isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-2",
            "name": "Пётр",
            "phone": "+79990000001",
            "summary": "",
            "service_id": None,
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.leads_by_month(months=3)

    assert len(result) == 3
    months_in_order = [entry["month"] for entry in result]
    assert months_in_order == sorted(months_in_order)  # хронологически, старое -> новое
    assert result[-1]["count"] == 1  # текущий месяц — s-1
    assert result[0]["count"] == 1  # 2 месяца назад — s-2 (месяц-в-середине — пустой, 0)
    assert result[1]["count"] == 0


def test_top_services_counts_and_sorts_by_lead_count(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    for service_id in ("chistki_e744e513", "chistki_e744e513", "fillery_f2df3e74"):
        append_jsonl(
            leads_file,
            {
                "timestamp": datetime.utcnow().isoformat(),
                "company_id": "rosh_import_demo",
                "session_id": f"s-{service_id}-{hash(service_id)}",
                "name": "Иван",
                "phone": "+79990000000",
                "summary": "",
                "service_id": service_id,
            },
        )
    # лид без service_id (например, задан вопрос без привязки к конкретной услуге) — не должен
    # попасть в топ как "None"
    append_jsonl(
        leads_file,
        {
            "timestamp": datetime.utcnow().isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-no-service",
            "name": "Ольга",
            "phone": "+79990000002",
            "summary": "",
            "service_id": None,
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.top_services()

    assert result[0] == {"service_id": "chistki_e744e513", "count": 2}
    assert {"service_id": "fillery_f2df3e74", "count": 1} in result
    assert all(entry["service_id"] != "None" for entry in result)
    assert len(result) == 2



def _impression(*, timestamp: datetime, company_id: str = "rosh_import_demo") -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "company_id": company_id,
        "session_id": "",
        "event_type": "widget_impression",
        "message": None,
        "metadata": {},
    }


def _chat_opened(*, timestamp: datetime, company_id: str = "rosh_import_demo") -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "company_id": company_id,
        "session_id": "",
        "event_type": "chat_opened",
        "message": None,
        "metadata": {},
    }


def test_leads_by_reason_counts_each_reason(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    for reason in ("booking", "booking", "price_question"):
        append_jsonl(
            leads_file,
            {
                "timestamp": datetime.utcnow().isoformat(),
                "company_id": "rosh_import_demo",
                "session_id": f"s-{reason}-{hash(reason)}",
                "name": "Иван",
                "phone": "+79990000000",
                "summary": "",
                "reason": reason,
            },
        )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.leads_by_reason()

    assert {"reason": "booking", "count": 2} in result
    assert {"reason": "price_question", "count": 1} in result


def test_conversion_funnel_counts_each_stage_and_step_percent(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    now = datetime.utcnow()

    for _ in range(100):
        append_jsonl(analytics_file, _impression(timestamp=now))
    for _ in range(20):
        append_jsonl(analytics_file, _chat_opened(timestamp=now))
    for i in range(5):
        append_jsonl(
            analytics_file,
            _event(event_type="message_answered", timestamp=now, action="answer") | {"session_id": f"s-{i}"},
        )
    # второе сообщение той же сессии не должно задваивать "разговоры" (уникальные session_id)
    append_jsonl(
        analytics_file, _event(event_type="message_answered", timestamp=now, action="answer") | {"session_id": "s-0"}
    )
    append_jsonl(
        leads_file,
        {
            "timestamp": now.isoformat(), "company_id": "rosh_import_demo", "session_id": "s-0",
            "name": "Иван", "phone": "+79990000000", "summary": "",
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.conversion_funnel()

    stages = {stage["label"]: stage for stage in result["stages"]}
    assert stages["Виджет загружен"]["count"] == 100
    assert stages["Виджет загружен"]["percent_of_previous"] == 100.0
    assert stages["Чат открыт"]["count"] == 20
    assert stages["Чат открыт"]["percent_of_previous"] == 20.0
    assert stages["Есть переписка"]["count"] == 5
    assert stages["Стал лидом"]["count"] == 1
    assert stages["Стал лидом"]["percent_of_previous"] == 20.0


def test_conversion_funnel_excludes_entries_outside_window(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    old = datetime.utcnow() - timedelta(days=45)
    append_jsonl(analytics_file, _impression(timestamp=old))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.conversion_funnel(days=30)

    assert result["stages"][0]["count"] == 0


def test_archive_rolls_up_widget_impression_and_chat_opened_without_colliding_as_unknown(tmp_path) -> None:
    """Живой баг, найден до попадания в прод (2026-08-27): widget_impression/chat_opened не
    несут metadata.action — раньше оба схлопнулись бы в один rollup-счётчик "unknown" вместе с
    любым бездействийным message_answered, теряя различимость типов."""

    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    stale_day = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(analytics_file, _impression(timestamp=stale_day))
    append_jsonl(analytics_file, _impression(timestamp=stale_day))
    append_jsonl(analytics_file, _chat_opened(timestamp=stale_day))

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 3
    rollup_entries = read_jsonl(rollup_file)
    by_action = {entry["action"]: entry["count"] for entry in rollup_entries}
    assert by_action == {"widget_impression": 2, "chat_opened": 1}


def test_unanswered_trend_includes_empty_days_and_counts_only_unknown_question(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    now = datetime.utcnow()
    append_jsonl(analytics_file, _event(event_type="unknown_question", timestamp=now, action=None))
    append_jsonl(analytics_file, _event(event_type="unknown_question", timestamp=now, action=None))
    # другой тип в тот же день не должен попасть в счёт
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=now))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.unanswered_trend(days=5)

    assert len(result) == 5
    assert result[-1]["date"] == now.strftime("%Y-%m-%d")
    assert result[-1]["count"] == 2
    assert result[0]["count"] == 0


def test_activity_by_hour_counts_message_answered_at_correct_hour(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    morning = datetime(2026, 1, 5, 9, 30, 0)
    evening = datetime(2026, 1, 5, 21, 15, 0)
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=morning))
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=morning))
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=evening))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.activity_by_hour()

    by_hour = {entry["hour"]: entry["count"] for entry in result}
    assert len(result) == 24
    assert by_hour[9] == 2
    assert by_hour[21] == 1
    assert by_hour[0] == 0


def test_activity_by_weekday_labels_and_counts(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    monday = datetime(2026, 1, 5, 12, 0, 0)  # 2026-01-05 — понедельник
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=monday))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.activity_by_weekday()

    by_label = {entry["label"]: entry["count"] for entry in result}
    assert len(result) == 7
    assert by_label["Пн"] == 1
    assert by_label["Вт"] == 0


def test_archive_rollup_rows_carry_hour_and_event_type(tmp_path) -> None:
    """2026-08-27: rollup раньше терял час навсегда и не различал event_type от action —
    без этого activity_by_hour/weekday не смогли бы честно продолжить тренд за ретеншном."""

    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "rollup.jsonl"
    stale_day = datetime(2026, 1, 5, 14, 30, 0)
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=stale_day, action="answer"))
    append_jsonl(analytics_file, _impression(timestamp=stale_day))

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 2
    rows = read_jsonl(rollup_file)
    message_row = next(r for r in rows if r["event_type"] == "message_answered")
    impression_row = next(r for r in rows if r["event_type"] == "widget_impression")
    assert message_row["hour"] == "14"
    assert message_row["action"] == "answer"
    assert impression_row["hour"] == "14"
    assert impression_row["action"] == "widget_impression"


def test_activity_by_hour_falls_back_to_rollup_past_retention(tmp_path) -> None:
    """Живой сценарий, который чинили (2026-08-27): message_answered старше ретеншна уже
    удалён из analytics_file, но activity_by_hour должен всё равно его увидеть через rollup."""

    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    rollup_file = tmp_path / "rollup.jsonl"
    append_jsonl(
        rollup_file,
        {
            "date": "2026-01-05", "hour": "09", "company_id": "rosh_import_demo",
            "event_type": "message_answered", "action": "answer", "count": 7,
        },
    )
    # событие другого типа в том же часе не должно приплюсоваться
    append_jsonl(
        rollup_file,
        {
            "date": "2026-01-05", "hour": "09", "company_id": "rosh_import_demo",
            "event_type": "widget_impression", "action": "widget_impression", "count": 100,
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file, rollup_file=rollup_file)
    result = service.activity_by_hour(company_id="rosh_import_demo", days=3650)

    by_hour = {entry["hour"]: entry["count"] for entry in result}
    assert by_hour[9] == 7


def test_activity_by_weekday_falls_back_to_rollup_past_retention(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    rollup_file = tmp_path / "rollup.jsonl"
    append_jsonl(
        rollup_file,
        {
            "date": "2026-01-05", "hour": "09", "company_id": "rosh_import_demo",  # понедельник
            "event_type": "message_answered", "action": "answer", "count": 4,
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file, rollup_file=rollup_file)
    result = service.activity_by_weekday(company_id="rosh_import_demo", days=3650)

    by_label = {entry["label"]: entry["count"] for entry in result}
    assert by_label["Пн"] == 4


def test_activity_by_hour_and_weekday_skip_malformed_rollup_count_instead_of_crashing(tmp_path) -> None:
    """Живой баг (код-ревью, 2026-08-27): int(row["count"]) раньше жил вне try/except —
    одна битая строка (не число) в rollup валила весь /api/analytics/dashboard вместо того,
    чтобы просто быть пропущенной. Валидная строка рядом должна досчитаться как обычно."""

    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    rollup_file = tmp_path / "rollup.jsonl"
    append_jsonl(
        rollup_file,
        {
            "date": "2026-01-05", "hour": "09", "company_id": "rosh_import_demo",
            "event_type": "message_answered", "action": "answer", "count": "не число",
        },
    )
    append_jsonl(
        rollup_file,
        {
            "date": "2026-01-05", "hour": "09", "company_id": "rosh_import_demo",
            "event_type": "message_answered", "action": "answer", "count": 5,
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file, rollup_file=rollup_file)

    by_hour = {entry["hour"]: entry["count"] for entry in service.activity_by_hour(company_id="rosh_import_demo", days=3650)}
    assert by_hour[9] == 5

    by_label = {
        entry["label"]: entry["count"]
        for entry in service.activity_by_weekday(company_id="rosh_import_demo", days=3650)
    }
    assert by_label["Пн"] == 5


def test_all_leads_merges_hot_file_and_archive(tmp_path) -> None:
    """2026-08-27: лиды-агрегаты (по месяцам/услуге/типу) раньше молча теряли всё старше
    leads_retention_days, как только archive_old_leads реально переносил записи в архив."""

    leads_file = tmp_path / "leads.jsonl"
    archive_file = tmp_path / "leads_archive.jsonl"
    append_jsonl(leads_file, _lead(session_id="hot-1"))
    append_jsonl(
        archive_file,
        {
            "timestamp": datetime(2025, 1, 1).isoformat(), "company_id": "rosh_import_demo",
            "session_id": "archived-1", "name": "Старый", "phone": "+79990000000",
            "summary": "", "reason": "booking",
        },
    )

    service = AnalyticsService(analytics_file=tmp_path / "a.jsonl", leads_file=leads_file, leads_archive_file=archive_file)
    result = service.leads_by_reason()

    total = sum(entry["count"] for entry in result)
    assert total == 2


def _requested_event(*, session_id: str, timestamp: datetime, company_id: str = "rosh_import_demo") -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "company_id": company_id,
        "session_id": session_id,
        "event_type": "operator_requested",
        "message": "хочу оператора",
        "metadata": {},
    }


def test_queue_wait_stats_computes_average_from_request_to_claim(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(analytics_file, _requested_event(session_id="s-1", timestamp=t0))
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=t0 + timedelta(minutes=4)))
    append_jsonl(analytics_file, _requested_event(session_id="s-2", timestamp=t0))
    append_jsonl(analytics_file, _claim_event(session_id="s-2", claimed_by="petya", timestamp=t0 + timedelta(minutes=8)))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.queue_wait_stats()

    assert result["avg_wait_minutes"] == 6.0
    assert result["sample_size"] == 2


def test_queue_wait_stats_uses_earliest_request_when_client_asked_twice(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(analytics_file, _requested_event(session_id="s-1", timestamp=t0))
    append_jsonl(analytics_file, _requested_event(session_id="s-1", timestamp=t0 + timedelta(minutes=5)))
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=t0 + timedelta(minutes=10)))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.queue_wait_stats()

    assert result["avg_wait_minutes"] == 10.0  # от первой просьбы, не от второй


def test_queue_wait_stats_ignores_claim_without_matching_request(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=datetime.utcnow()))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.queue_wait_stats()

    assert result["avg_wait_minutes"] is None
    assert result["sample_size"] == 0


def test_period_comparison_splits_current_and_previous_equal_windows(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    now = datetime.utcnow()

    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=now - timedelta(days=2)) | {"session_id": "s-current"})
    append_jsonl(
        analytics_file,
        _event(event_type="message_answered", timestamp=now - timedelta(days=15)) | {"session_id": "s-previous-1"},
    )
    append_jsonl(
        analytics_file,
        _event(event_type="message_answered", timestamp=now - timedelta(days=16)) | {"session_id": "s-previous-2"},
    )
    append_jsonl(leads_file, _lead(session_id="lead-current"))
    old_lead = _lead(session_id="lead-previous")
    old_lead["timestamp"] = (now - timedelta(days=15)).isoformat()
    append_jsonl(leads_file, old_lead)

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.period_comparison(days=10)

    assert result["conversations"] == {"current": 1, "previous": 2}
    assert result["leads"] == {"current": 1, "previous": 1}

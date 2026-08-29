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
    """Живой баг (код-ревью, 2026-08-27): close/лид той же сессии не должны молча теряться
    из "лидов"/"закрыто" только из-за того, что days фильтрует ОТДЕЛЬНО СЧИТАННЫЙ список
    claim-событий — closes/leads всегда читаются из полной истории, независимо от days,
    только claim обязан попасть в days-окно, чтобы вообще "войти в отчёт".

    2026-08-29: даты сделаны причинно реалистичными (close/лид СТРОГО после claim) — старая
    версия теста ставила closed_at РАНЬШЕ claimed_at (закрытие на 10 дней раньше клейма),
    что математически не может произойти по-настоящему и с новой логикой (лид засчитывается
    оператору только если он попал реально между claim и close) не проходит не из-за
    регрессии, а потому что перевёрнутые во времени данные больше не изображают валидный
    сценарий, который эта логика должна поддерживать."""

    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    now = datetime.utcnow()
    claimed_at = now - timedelta(days=6, hours=23)  # внутри days=7, у самой границы
    lead_at = claimed_at + timedelta(minutes=5)
    closed_at = claimed_at + timedelta(minutes=10)
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=claimed_at))
    append_jsonl(analytics_file, _closed_event(session_id="s-1", claimed_by="masha", timestamp=closed_at))
    append_jsonl(leads_file, _lead(session_id="s-1", timestamp=lead_at))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary(days=7)

    assert result["operators"]["masha"]["claimed"] == 1
    assert result["operators"]["masha"]["closed"] == 1
    assert result["operators"]["masha"]["leads"] == 1


def test_operator_summary_lead_before_first_claim_goes_to_bot(tmp_path) -> None:
    """2026-08-29, живой баг (ручное тестирование пользователем): бот сам собрал контакт до
    того, как кто-либо вообще взял сессию в работу — засчитывать этот лид оператору,
    который позже заклеймил диалог по другому поводу, нечестно. Идёт в "Бот"."""

    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(leads_file, _lead(session_id="s-1", timestamp=t0))
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=t0 + timedelta(minutes=10)))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary()

    assert result["operators"]["masha"]["leads"] == 0
    assert result["operators"]["Бот"]["leads"] == 1
    assert result["operators"]["Бот"]["claimed"] == 0
    assert result["operators"]["Бот"]["avg_dialog_minutes"] is None


def test_operator_summary_lead_after_close_goes_to_bot(tmp_path) -> None:
    """Клиент вернулся к боту уже ПОСЛЕ того, как оператор закрыл диалог — это снова работа
    бота, не оператора, даже несмотря на общий session_id."""

    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=t0))
    append_jsonl(analytics_file, _closed_event(session_id="s-1", claimed_by="masha", timestamp=t0 + timedelta(minutes=10)))
    append_jsonl(leads_file, _lead(session_id="s-1", timestamp=t0 + timedelta(minutes=20)))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary()

    assert result["operators"]["masha"]["leads"] == 0
    assert result["operators"]["Бот"]["leads"] == 1


def test_operator_summary_lead_never_claimed_at_all_goes_to_bot(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    append_jsonl(leads_file, _lead(session_id="s-1", timestamp=datetime.utcnow()))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary()

    assert "operators" in result
    assert result["operators"]["Бот"]["leads"] == 1


def test_operator_summary_no_bot_entry_when_every_lead_is_attributed(tmp_path) -> None:
    """Не захламляем таблицу пустой строкой "Бот", если реально не на что её показывать."""

    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=t0))
    append_jsonl(leads_file, _lead(session_id="s-1", timestamp=t0 + timedelta(minutes=5)))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary()

    assert "Бот" not in result["operators"]


def test_operator_summary_reclaim_after_close_attributes_by_active_window(tmp_path) -> None:
    """Сессию заклеймили, закрыли, потом заклеймили СНОВА (уже другим оператором) — лид,
    случившийся во втором окне, должен достаться второму оператору, не первому и не боту,
    хотя session_id один и тот же на всю жизнь диалога."""

    analytics_file = tmp_path / "analytics.jsonl"
    leads_file = tmp_path / "leads.jsonl"
    t0 = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="masha", timestamp=t0))
    append_jsonl(analytics_file, _closed_event(session_id="s-1", claimed_by="masha", timestamp=t0 + timedelta(minutes=10)))
    append_jsonl(analytics_file, _claim_event(session_id="s-1", claimed_by="petya", timestamp=t0 + timedelta(hours=2)))
    append_jsonl(leads_file, _lead(session_id="s-1", timestamp=t0 + timedelta(hours=2, minutes=5)))

    service = AnalyticsService(analytics_file=analytics_file, leads_file=leads_file)
    result = service.operator_summary()

    assert result["operators"]["masha"]["leads"] == 0
    assert result["operators"]["petya"]["leads"] == 1
    assert "Бот" not in result["operators"]


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


def test_intent_breakdown_counts_message_answered_by_policy_reason(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    now = datetime.utcnow()
    append_jsonl(
        analytics_file,
        {
            "timestamp": now.isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-1",
            "event_type": "message_answered",
            "message": "сколько стоит?",
            "metadata": {"policy_reason": "price_question"},
        },
    )
    append_jsonl(
        analytics_file,
        {
            "timestamp": now.isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-2",
            "event_type": "message_answered",
            "message": "привет",
            "metadata": {"policy_reason": "small_talk"},
        },
    )
    append_jsonl(
        analytics_file,
        {
            "timestamp": now.isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-3",
            "event_type": "message_answered",
            "message": "сколько стоит ещё раз?",
            "metadata": {"policy_reason": "price_question"},
        },
    )
    # другой event_type — не должен попасть в разбивку
    append_jsonl(
        analytics_file,
        {
            "timestamp": now.isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-4",
            "event_type": "unknown_question",
            "message": "непонятный вопрос",
            "metadata": {"policy_reason": "unknown_service"},
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=tmp_path / "leads.jsonl")
    result = service.intent_breakdown()

    assert result == [
        {"reason": "price_question", "count": 2},
        {"reason": "small_talk", "count": 1},
    ]


def test_objection_breakdown_counts_objection_raised_by_topic(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    now = datetime.utcnow()
    for topic, session_id in [("price", "s-1"), ("price", "s-2"), ("hesitation", "s-3")]:
        append_jsonl(
            analytics_file,
            {
                "timestamp": now.isoformat(),
                "company_id": "rosh_import_demo",
                "session_id": session_id,
                "event_type": "objection_raised",
                "message": "дорого",
                "metadata": {"policy_reason": "objection_handled", "objection_topic": topic},
            },
        )
    # другой event_type — не должен попасть в разбивку
    append_jsonl(
        analytics_file,
        {
            "timestamp": now.isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-4",
            "event_type": "message_answered",
            "message": "привет",
            "metadata": {"policy_reason": "small_talk"},
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=tmp_path / "leads.jsonl")
    result = service.objection_breakdown()

    assert result == [
        {"topic": "price", "count": 2},
        {"topic": "hesitation", "count": 1},
    ]


def test_objection_breakdown_missing_topic_falls_back_to_unknown(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    append_jsonl(
        analytics_file,
        {
            "timestamp": datetime.utcnow().isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-1",
            "event_type": "objection_raised",
            "message": "не уверен",
            "metadata": {"policy_reason": "objection_handled", "objection_topic": None},
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=tmp_path / "leads.jsonl")
    result = service.objection_breakdown()

    assert result == [{"topic": "unknown", "count": 1}]


def test_objection_breakdown_empty_when_no_events(tmp_path) -> None:
    service = AnalyticsService(analytics_file=tmp_path / "analytics.jsonl", leads_file=tmp_path / "leads.jsonl")
    assert service.objection_breakdown() == []


def test_list_conversations_merges_live_and_archived_and_filters_by_scope(tmp_path) -> None:
    from app.models import Message, MessageRole, Session, SessionStatus

    analytics_file = tmp_path / "analytics.jsonl"
    archive_file = tmp_path / "conversations_archive.jsonl"

    live_bot_only = Session(
        session_id="live-bot",
        company_id="rosh_import_demo",
        messages=[Message(role=MessageRole.USER, text="привет")],
    )
    live_operator = Session(
        session_id="live-operator",
        company_id="rosh_import_demo",
        status=SessionStatus.HUMAN_ACTIVE,
        operator_requested=True,
        messages=[Message(role=MessageRole.USER, text="хочу оператора")],
    )
    append_jsonl(
        archive_file,
        {
            "session_id": "archived-lead",
            "company_id": "rosh_import_demo",
            "status": "CLOSED",
            "lead_requested": True,
            "operator_requested": False,
            "telegram_claimed_by": None,
            "created_at": datetime.utcnow().isoformat(),
            "closed_at": datetime.utcnow().isoformat(),
            "messages": [{"role": "user", "text": "запишите меня", "kind": None, "created_at": datetime.utcnow().isoformat()}],
        },
    )

    service = AnalyticsService(
        analytics_file=analytics_file,
        leads_file=tmp_path / "leads.jsonl",
        conversations_archive_file=archive_file,
    )

    all_items = service.list_conversations([live_bot_only, live_operator])
    assert {item["session_id"] for item in all_items} == {"live-bot", "live-operator", "archived-lead"}

    bot_only = service.list_conversations([live_bot_only, live_operator], scope="bot_only")
    assert {item["session_id"] for item in bot_only} == {"live-bot", "archived-lead"}

    operator_only = service.list_conversations([live_bot_only, live_operator], scope="operator")
    assert {item["session_id"] for item in operator_only} == {"live-operator"}

    lead_only = service.list_conversations([live_bot_only, live_operator], scope="lead")
    assert {item["session_id"] for item in lead_only} == {"archived-lead"}


def test_get_conversation_returns_live_session_then_falls_back_to_archive(tmp_path) -> None:
    from app.models import Message, MessageRole, Session

    analytics_file = tmp_path / "analytics.jsonl"
    archive_file = tmp_path / "conversations_archive.jsonl"
    live_session = Session(
        session_id="live-1",
        company_id="rosh_import_demo",
        messages=[Message(role=MessageRole.USER, text="привет")],
    )
    append_jsonl(
        archive_file,
        {
            "session_id": "archived-1",
            "company_id": "rosh_import_demo",
            "status": "CLOSED",
            "lead_requested": False,
            "operator_requested": False,
            "telegram_claimed_by": None,
            "created_at": datetime.utcnow().isoformat(),
            "closed_at": datetime.utcnow().isoformat(),
            "messages": [{"role": "user", "text": "старый вопрос", "kind": None, "created_at": datetime.utcnow().isoformat()}],
        },
    )
    service = AnalyticsService(
        analytics_file=analytics_file,
        leads_file=tmp_path / "leads.jsonl",
        conversations_archive_file=archive_file,
    )

    live_result = service.get_conversation("live-1", [live_session])
    assert live_result["source"] == "live"
    assert live_result["messages"][0]["text"] == "привет"

    archived_result = service.get_conversation("archived-1", [live_session])
    assert archived_result["source"] == "archive"
    assert archived_result["messages"][0]["text"] == "старый вопрос"

    assert service.get_conversation("does-not-exist", [live_session]) is None


def test_track_policy_result_records_objection_raised_with_topic(tmp_path) -> None:
    """2026-08-29: инструментация под будущий отчёт "топ возражений по теме" — раньше
    конкретная тема (price/hesitation/competitor/...) нигде долговечно не логировалась."""

    import anyio

    from app.models import PolicyAction, PolicyReason, PolicyResult

    analytics_file = tmp_path / "analytics.jsonl"
    service = AnalyticsService(analytics_file=analytics_file, leads_file=tmp_path / "leads.jsonl")

    policy_result = PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.OBJECTION_HANDLED,
        safe_context={"objection_topic": "price"},
    )

    anyio.run(
        lambda: service.track_policy_result(
            company_id="rosh_import_demo",
            session_id="s-1",
            message="это дорого",
            policy_result=policy_result,
        )
    )

    events = [e for e in read_jsonl(analytics_file) if e["event_type"] == "objection_raised"]
    assert len(events) == 1
    assert events[0]["metadata"]["objection_topic"] == "price"
    assert events[0]["message"] == "это дорого"


def test_track_policy_result_skips_objection_raised_for_non_objection_reasons(tmp_path) -> None:
    import anyio

    from app.models import PolicyAction, PolicyReason, PolicyResult

    analytics_file = tmp_path / "analytics.jsonl"
    service = AnalyticsService(analytics_file=analytics_file, leads_file=tmp_path / "leads.jsonl")

    policy_result = PolicyResult(action=PolicyAction.ANSWER, reason=PolicyReason.OK, safe_context={})

    anyio.run(
        lambda: service.track_policy_result(
            company_id="rosh_import_demo",
            session_id="s-1",
            message="привет",
            policy_result=policy_result,
        )
    )

    events = [e for e in read_jsonl(analytics_file) if e["event_type"] == "objection_raised"]
    assert events == []


def test_top_unanswered_questions_groups_by_normalized_text(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    now = datetime.utcnow()

    def _unknown(message: str) -> dict:
        return {
            "timestamp": now.isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-1",
            "event_type": "unknown_question",
            "message": message,
            "metadata": {},
        }

    append_jsonl(analytics_file, _unknown("Сколько стоит ботокс?"))
    append_jsonl(analytics_file, _unknown("сколько стоит ботокс?"))  # тот же вопрос, регистр
    append_jsonl(analytics_file, _unknown("  Сколько стоит ботокс?  "))  # пробелы
    append_jsonl(analytics_file, _unknown("А делаете ли скидки?"))
    # message_answered — не должен попасть в топ непонятых
    append_jsonl(
        analytics_file,
        {
            "timestamp": now.isoformat(),
            "company_id": "rosh_import_demo",
            "session_id": "s-1",
            "event_type": "message_answered",
            "message": "привет",
            "metadata": {},
        },
    )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=tmp_path / "leads.jsonl")
    result = service.top_unanswered_questions()

    assert result == [
        {"message": "Сколько стоит ботокс?", "count": 3},
        {"message": "А делаете ли скидки?", "count": 1},
    ]


def test_top_answered_questions_uses_message_answered_events(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    now = datetime.utcnow()
    for _ in range(2):
        append_jsonl(
            analytics_file,
            {
                "timestamp": now.isoformat(),
                "company_id": "rosh_import_demo",
                "session_id": "s-1",
                "event_type": "message_answered",
                "message": "какие есть услуги",
                "metadata": {},
            },
        )

    service = AnalyticsService(analytics_file=analytics_file, leads_file=tmp_path / "leads.jsonl")
    result = service.top_answered_questions()

    assert result == [{"message": "какие есть услуги", "count": 2}]

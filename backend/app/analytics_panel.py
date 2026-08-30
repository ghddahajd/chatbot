"""Веб-страница аналитики (2026-08-27) — сводка, операторы, лиды по месяцам, топ услуг.

Стиль чата (виджета), не оператор-панели: тёплый светлый фон, скруглённые карточки, мягкие
тени — тот же язык, что и в widget.js (--bg/--text/--accent-soft), но без копии шрифта
(широкий встроенный woff2 не тащим ради внутреннего инструмента). Цвета данных в графиках —
из проверенной валидатором дефолтной палитры дата-виз скилла (CVD-safe, не "на глаз")."""


def render_analytics_panel(
    *,
    default_company_id: str = "rosh_import_demo",
    show_company_selector: bool = True,
) -> str:
    """default_company_id/show_company_selector (2026-08-29) — /analytics (для клиента,
    когда/если отдадим доступ) вызывает это с show_company_selector=False: дропдаун скрыт,
    клиент никогда не узнает, что вообще существует второй company_id для тестового трафика
    (см. /backstage в main.py). Сам JS-код (loadChats/load/companySelect-listener) не тронут —
    он как читал .value у #companySelect, так и читает; при show_company_selector=False это
    просто select с одним вариантом и без визуального намёка на выбор."""

    if show_company_selector:
        # Живой баг (найден при живой проверке в браузере, 2026-08-29): раньше "rosh_test"
        # было захардкожено вторым вариантом безусловно — при default_company_id="rosh_test"
        # (ровно случай /backstage) это давало ДВЕ одинаковые опции и ни одной для
        # rosh_import_demo. Теперь берём оба известных id, ставим default_company_id первым/
        # выбранным, остальные — следом, без дублей независимо от того, что передали дефолтом.
        known_company_ids = ["rosh_test", "rosh_import_demo"]
        ordered_ids = [default_company_id] + [
            company_id for company_id in known_company_ids if company_id != default_company_id
        ]
        options_html = "".join(
            f'<option value="{company_id}"{" selected" if company_id == default_company_id else ""}>'
            f"{company_id}</option>"
            for company_id in ordered_ids
        )
        company_select_html = (
            f'<select class="company-select" id="companySelect">{options_html}'
            f'<option value="">Все компании</option>'
            f"</select>"
        )
    else:
        company_select_html = (
            f'<select id="companySelect" style="display:none">'
            f'<option value="{default_company_id}" selected>{default_company_id}</option>'
            f"</select>"
        )

    html = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Аналитика</title>
  <style>
    :root {
      --bg: #fbf9f7;
      --bg-page: #f6f4f3;
      --card: #ffffff;
      --text: #080e0d;
      --text-secondary: #5c6560;
      --text-muted: #97a098;
      --border: #ece8e4;
      --border-soft: #f1eeea;
      --accent: #080e0d;
      --accent-soft: #adce6d;
      --accent-deep: #5f7a35;
      --radius: 24px;
      --radius-sm: 16px;
      --shadow: 0 16px 40px rgba(8,14,13,.08), 0 4px 12px rgba(8,14,13,.04);
      /* категориальная палитра dataviz-скилла (валидирована, порядок фиксирован) */
      --series-1: #2a78d6;
      --series-2: #eb6834;
      --series-3: #1baf7a;
      --series-4: #eda100;
      --series-5: #e87ba4;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
      background: var(--bg-page);
      color: var(--text);
      -webkit-font-smoothing: antialiased;
    }
    main { max-width: 1180px; margin: 0 auto; padding: 32px 20px 64px; }
    header {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 28px;
      flex-wrap: wrap;
    }
    h1 { font-size: 28px; font-weight: 800; letter-spacing: -.02em; margin: 0; }
    .subtitle { color: var(--text-secondary); font-size: 14px; margin-top: 4px; }
    .header-controls { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .logout-btn {
      font: inherit;
      font-size: 13px;
      font-weight: 600;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--text-secondary);
      cursor: pointer;
    }
    .logout-btn:hover { background: var(--card); color: var(--text); }
    .company-select {
      font: inherit;
      font-size: 14px;
      font-weight: 600;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      cursor: pointer;
    }
    .custom-range-inputs { display: flex; align-items: center; gap: 6px; }
    .custom-range-inputs input[type="date"] {
      font: inherit; font-size: 13.5px; font-weight: 600; padding: 8px 10px; border-radius: 999px;
      border: 1px solid var(--border); background: var(--card); color: var(--text);
    }
    .custom-range-dash { color: var(--text-muted); }
    .leads-filters { margin-bottom: 14px; }
    .leads-table-empty { color: var(--text-muted); font-size: 13.5px; padding: 12px 2px; }

    .tiles {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }
    .tile {
      background: var(--card);
      border-radius: var(--radius-sm);
      box-shadow: var(--shadow);
      padding: 18px 20px;
    }
    .tile-label { font-size: 13px; color: var(--text-secondary); font-weight: 600; }
    .tile-value {
      font-size: 30px;
      font-weight: 800;
      letter-spacing: -.02em;
      margin-top: 6px;
      font-variant-numeric: proportional-nums;
    }
    .tile-value.accent { color: var(--accent-deep); }
    .tile-delta {
      display: inline-block;
      margin-left: 8px;
      font-size: 13px;
      font-weight: 700;
      vertical-align: middle;
    }
    .tile-delta.up { color: var(--accent-deep); }
    .tile-delta.down { color: #b3261e; }
    .tile-delta.flat { color: var(--text-muted); }

    .grid-2 {
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }
    @media (max-width: 860px) {
      .grid-2 { grid-template-columns: 1fr; }
      .chats-layout { flex-direction: column; }
      .chats-list-pane { flex-basis: auto; width: 100%; max-height: 320px; }
      .chats-detail-pane { max-height: none; }
    }

    .card {
      background: var(--card);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      margin-bottom: 16px;
      padding: 22px 24px;
    }
    .card h2 {
      font-size: 15px;
      font-weight: 800;
      margin: 0 0 4px;
      letter-spacing: -.01em;
    }
    .card .card-hint { font-size: 12.5px; color: var(--text-muted); margin: 0 0 18px; }

    /* ── лиды по месяцам: одна серия, столбцы ── */
    .month-chart { display: flex; align-items: flex-end; gap: 10px; height: 160px; padding-top: 8px; }
    /* min-width:0 — без него flex-колонка не сжимается уже своего контента (текста лейбла),
       на 24 колонках (часы суток) это пихало более широкие "21"/"18" вправо и рассыпало
       выравнивание лейблов под барами ("ось X полетела", живой баг 2026-08-27) */
    .month-col { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; gap: 8px; height: 100%; justify-content: flex-end; }
    .month-bar-wrap { width: 100%; display: flex; justify-content: center; align-items: flex-end; flex: 1; }
    .month-bar {
      width: 60%;
      max-width: 34px;
      background: var(--border);
      border-radius: 4px 4px 0 0;
      position: relative;
      transition: background .15s;
      min-height: 3px;
    }
    .month-bar.current { background: var(--accent-soft); }
    .month-bar:hover { background: var(--accent-deep); }
    .month-bar:hover .month-bar-value { opacity: 1; }
    .month-bar-value {
      position: absolute;
      top: -22px;
      left: 50%;
      transform: translateX(-50%);
      font-size: 12px;
      font-weight: 700;
      opacity: 0;
      transition: opacity .1s;
      white-space: nowrap;
    }
    .month-label { font-size: 11.5px; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: .02em; }

    /* ── операторы: таблица ── */
    table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
    thead th {
      text-align: left;
      font-size: 11.5px;
      text-transform: uppercase;
      letter-spacing: .03em;
      color: var(--text-muted);
      font-weight: 700;
      padding: 0 10px 10px;
      border-bottom: 1px solid var(--border);
    }
    thead th.num, tbody td.num { text-align: right; font-variant-numeric: tabular-nums; }
    tbody td { padding: 12px 10px; border-bottom: 1px solid var(--border-soft); }
    tbody tr:last-child td { border-bottom: 0; }
    .operator-name { font-weight: 700; }
    .empty-state { color: var(--text-muted); font-size: 13.5px; padding: 12px 2px; }
    .bot-banner {
      display: flex; align-items: center; gap: 10px; padding: 12px 16px; margin-bottom: 14px;
      border-radius: var(--radius-sm); background: color-mix(in srgb, var(--accent-soft) 22%, var(--card));
    }
    .bot-banner-icon { font-size: 18px; line-height: 1; }
    .bot-banner-text { font-size: 13.5px; color: var(--text-secondary); }
    .bot-banner-text strong { color: var(--text); font-size: 15px; }

    /* ── топ услуг: горизонтальные бары ── */
    .service-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
    .service-row:last-child { margin-bottom: 0; }
    .service-name { flex: 0 0 42%; font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .service-bar-track { flex: 1; background: var(--border-soft); border-radius: 4px; height: 10px; overflow: hidden; }
    .service-bar-fill { height: 100%; border-radius: 4px; }
    .service-count { flex: 0 0 26px; text-align: right; font-size: 12.5px; font-weight: 700; color: var(--text-secondary); font-variant-numeric: tabular-nums; }

    /* ── воронка конверсии ── */
    .funnel-stage { margin-bottom: 18px; }
    .funnel-stage:last-child { margin-bottom: 0; }
    .funnel-row { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
    .funnel-label { font-size: 13.5px; font-weight: 700; }
    .funnel-value { font-size: 13.5px; font-weight: 700; font-variant-numeric: tabular-nums; }
    .funnel-value .funnel-percent { color: var(--text-muted); font-weight: 600; margin-left: 4px; }
    .funnel-track { background: var(--border-soft); border-radius: 999px; height: 28px; overflow: hidden; }
    .funnel-fill {
      height: 100%;
      background: var(--accent-deep);
      border-radius: 999px;
      display: flex;
      align-items: center;
      padding-left: 12px;
      min-width: 40px;
      transition: width .3s ease;
    }
    .funnel-fill.dim { background: var(--border); }
    .funnel-fill-label { font-size: 11.5px; font-weight: 700; color: #fff; white-space: nowrap; }

    /* ── donut: лиды по типу ── */
    .donut-wrap { display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }
    .donut { width: 148px; height: 148px; border-radius: 50%; flex-shrink: 0; }
    .donut-legend { display: flex; flex-direction: column; gap: 10px; flex: 1; min-width: 160px; }
    .legend-row { display: flex; align-items: center; gap: 9px; font-size: 13px; }
    .legend-dot { width: 10px; height: 10px; border-radius: 3px; flex-shrink: 0; }
    .legend-label { flex: 1; font-weight: 600; }
    .legend-value { font-weight: 700; color: var(--text-secondary); font-variant-numeric: tabular-nums; }

    /* ── лента нераспознанных вопросов ── */
    .feed-item { padding: 12px 0; border-bottom: 1px solid var(--border-soft); }
    .feed-item:last-child { border-bottom: 0; }
    .feed-text { font-size: 13.5px; font-weight: 600; overflow-wrap: break-word; }
    .feed-meta { font-size: 11.5px; color: var(--text-muted); margin-top: 3px; }

    .loading, .error { text-align: center; padding: 60px 20px; color: var(--text-muted); font-size: 14px; }
    .error { color: #b3261e; }

    /* ── вкладки (2026-08-29, TSK-05) ── */
    .tabs { display: flex; gap: 6px; margin-bottom: 20px; border-bottom: 1px solid var(--border); }
    .tab-btn {
      font: inherit; font-size: 14px; font-weight: 700; padding: 10px 18px; border: none;
      background: transparent; color: var(--text-muted); cursor: pointer;
      border-bottom: 2px solid transparent; margin-bottom: -1px;
    }
    .tab-btn.active { color: var(--text); border-bottom-color: var(--accent-deep); }
    .tab-btn:hover { color: var(--text); }

    /* ── вкладка "Чаты" ── */
    .chat-filters { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
    .filter-btn {
      font: inherit; font-size: 13px; font-weight: 600; padding: 8px 14px; border-radius: 999px;
      border: 1px solid var(--border); background: var(--card); color: var(--text-secondary); cursor: pointer;
    }
    .filter-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .filter-btn:hover:not(.active) { background: var(--border-soft); }

    /* ── вкладка "Настройки" ──
       Единая система полей ниже (один padding/radius/border/focus на все input/select,
       что бы их ни отрисовывало — settings-field/hours-row/doctor-row) — раньше у каждого
       блока были свои чуть-чуть разные padding/radius (9px/12px/10px vs 7px/10px/8px vs
       8px/11px/8px) и ни у одного не было :focus — расползалось на глаз и подсвечивалось
       голым синим браузерным аутлайном при клике. 2026-08-29, по фидбеку "как будто из 1С". */
    .settings-field,
    .hours-row,
    .doctor-row {
      --field-radius: 10px;
    }
    .settings-field input[type="text"],
    .settings-field input[type="time"],
    .settings-field select,
    .hours-row input[type="time"],
    .doctor-row input[type="text"] {
      font: inherit; font-size: 14px; padding: 9px 12px; border-radius: var(--field-radius);
      border: 1px solid var(--border); background: var(--bg); color: var(--text);
      transition: border-color .15s, box-shadow .15s;
    }
    .settings-field input[type="text"]:focus,
    .settings-field input[type="time"]:focus,
    .settings-field select:focus,
    .hours-row input[type="time"]:focus,
    .doctor-row input[type="text"]:focus {
      outline: none; border-color: var(--accent-deep);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-deep) 18%, transparent);
    }
    .settings-field { display: flex; flex-direction: column; gap: 5px; margin-bottom: 16px; max-width: 420px; }
    .settings-field label { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
    .settings-checkbox {
      display: flex; align-items: center; gap: 8px; margin-bottom: 10px; font-size: 14px; cursor: pointer;
    }
    .settings-checkbox input[type="checkbox"] { accent-color: var(--accent-deep); width: 16px; height: 16px; cursor: pointer; }
    .hours-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
    .hours-day-label { width: 32px; font-weight: 700; font-size: 13px; color: var(--text-secondary); }
    .hours-closed-toggle {
      font-size: 13px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; cursor: pointer;
    }
    .hours-closed-toggle input[type="checkbox"] { accent-color: var(--accent-deep); width: 15px; height: 15px; cursor: pointer; }
    .settings-save-btn {
      font: inherit; font-size: 14px; font-weight: 700; padding: 11px 22px; border-radius: 999px;
      border: none; background: var(--accent-deep); color: #fff; cursor: pointer;
      transition: background .15s, transform .1s;
    }
    .settings-save-btn:hover:not(:disabled) { background: color-mix(in srgb, var(--accent-deep) 88%, black); }
    .settings-save-btn:active:not(:disabled) { transform: scale(.98); }
    .settings-save-btn:disabled { opacity: .6; cursor: default; }
    .settings-status { margin-left: 12px; font-size: 13px; font-weight: 600; }
    .settings-status.success { color: var(--accent-deep); }
    .settings-status.error { color: #c0392b; }
    .doctor-row {
      display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap;
    }
    .doctor-row .doctor-name { flex: 1 1 180px; min-width: 140px; }
    .doctor-row .doctor-specialty { flex: 1 1 160px; min-width: 130px; }
    .doctor-row .doctor-schedule { flex: 1 1 180px; min-width: 150px; }
    .doctor-remove-btn {
      flex: 0 0 auto; font: inherit; font-size: 16px; line-height: 1; width: 32px; height: 32px;
      border-radius: var(--field-radius); border: 1px solid var(--border); background: var(--bg);
      color: #c0392b; cursor: pointer; transition: background .15s, border-color .15s;
    }
    .doctor-remove-btn:hover { background: #fdeceb; border-color: #f3c6c2; }
    .doctor-add-btn {
      font: inherit; font-size: 13.5px; font-weight: 600; padding: 8px 16px; border-radius: 999px;
      border: 1px dashed var(--border); background: transparent; color: var(--accent-deep);
      cursor: pointer; margin-top: 4px; transition: background .15s, border-color .15s;
    }
    .doctor-add-btn:hover { background: var(--border-soft); border-color: var(--accent-deep); }
    .settings-card-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .settings-card-header h2 { margin: 0; }
    .settings-reset-btn {
      flex: 0 0 auto; font: inherit; font-size: 12.5px; font-weight: 600; padding: 5px 12px;
      border-radius: 999px; border: 1px solid var(--border); background: transparent;
      color: var(--text-secondary); cursor: pointer; transition: background .15s, color .15s;
    }
    .settings-reset-btn:hover:not(:disabled) { background: var(--border-soft); color: var(--text); }
    .settings-reset-btn:disabled { opacity: .45; cursor: default; }

    /* ── чаты: список слева / переписка справа (master-detail) ── */
    .chats-layout { display: flex; gap: 16px; align-items: flex-start; }
    .chats-list-pane { flex: 0 0 320px; max-height: 74vh; overflow-y: auto; padding: 8px; }
    .chats-detail-pane { flex: 1 1 auto; min-width: 0; max-height: 74vh; overflow-y: auto; }

    .chat-row { padding: 11px 12px; border-radius: 12px; cursor: pointer; }
    .chat-row + .chat-row { margin-top: 2px; }
    .chat-row:hover { background: var(--border-soft); }
    .chat-row.active { background: color-mix(in srgb, var(--accent-soft) 32%, var(--card)); }
    .chat-row-top { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
    .chat-id { font-size: 12px; font-weight: 700; color: var(--text-muted); font-variant-numeric: tabular-nums; }
    .chat-badge {
      font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
      background: var(--border-soft); color: var(--text-secondary);
    }
    .chat-badge.operator { background: #fdf0e5; color: #a65a1f; }
    .chat-badge.lead { background: #eaf4e0; color: var(--accent-deep); }
    .chat-time { font-size: 11.5px; color: var(--text-muted); margin-left: auto; }
    .chat-preview { font-size: 13.5px; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .chat-detail-header {
      display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
      padding: 2px 6px 16px; margin-bottom: 14px; border-bottom: 1px solid var(--border-soft);
    }
    .t-msg { max-width: 78%; padding: 9px 13px; border-radius: 14px; margin-bottom: 8px; font-size: 13.5px; line-height: 1.4; }
    .t-msg-role { font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; opacity: .6; margin-bottom: 2px; }
    .t-msg.user { background: var(--border-soft); margin-right: auto; }
    .t-msg.assistant { background: var(--card); border: 1px solid var(--border); margin-right: auto; }
    .t-msg.operator { background: var(--accent-soft); margin-left: auto; }
    .t-msg.system { background: transparent; border: 1px dashed var(--border); color: var(--text-muted); margin: 0 auto 8px; text-align: center; max-width: 90%; }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Аналитика</h1>
        <div class="subtitle">Операторы, лиды, конверсия</div>
      </div>
      <div class="header-controls">
        <select class="company-select" id="daysSelect">
          <option value="1">Сегодня</option>
          <option value="7">7 дней</option>
          <option value="30" selected>30 дней</option>
          <option value="90">90 дней</option>
          <option value="3650">Всё время</option>
          <option value="custom">Свой период</option>
        </select>
        <span class="custom-range-inputs" id="customRangeInputs" style="display:none">
          <input type="date" id="rangeStart" />
          <span class="custom-range-dash">—</span>
          <input type="date" id="rangeEnd" />
        </span>
        __COMPANY_SELECT_HTML__
        <form method="post" action="/logout">
          <button type="submit" class="logout-btn">Выйти</button>
        </form>
      </div>
    </header>

    <div class="tabs">
      <button type="button" class="tab-btn active" id="tabDashboardBtn">Дашборд</button>
      <button type="button" class="tab-btn" id="tabChatsBtn">Чаты</button>
      <button type="button" class="tab-btn" id="tabLeadsBtn">Лиды</button>
      <button type="button" class="tab-btn" id="tabSettingsBtn">Настройки</button>
    </div>

    <div id="content">
      <div class="loading">Загрузка…</div>
    </div>

    <div id="chatsContent" style="display:none">
      <div class="chat-filters">
        <button type="button" class="filter-btn active" data-scope="all">Все</button>
        <button type="button" class="filter-btn" data-scope="bot_only">Только бот</button>
        <button type="button" class="filter-btn" data-scope="operator">С оператором</button>
        <button type="button" class="filter-btn" data-scope="lead">Успешные лиды</button>
      </div>
      <div class="chats-layout">
        <div class="card chats-list-pane" id="chatsList">
          <div class="loading">Загрузка…</div>
        </div>
        <div class="card chats-detail-pane" id="chatDetail">
          <div class="empty-state">Выберите диалог слева</div>
        </div>
      </div>
    </div>

    <div id="leadsContent" style="display:none">
      <div class="card">
        <h2>Лиды</h2>
        <p class="card-hint">
          Без персональных данных — имя и телефон видны в Telegram-теме диалога, тут только метаданные заявки.
        </p>
        <div class="leads-filters">
          <select class="company-select" id="leadsReasonFilter">
            <option value="">Все типы</option>
            <option value="booking">Запись</option>
            <option value="price_question">Вопрос о цене</option>
            <option value="medical_risk">Консультация</option>
            <option value="commercial_interest">Интерес к услуге</option>
            <option value="unknown_service">Неизвестная услуга</option>
          </select>
        </div>
        <div id="leadsTableWrap"><div class="loading">Загрузка…</div></div>
      </div>
    </div>

    <div id="settingsContent" style="display:none">
      <div class="card">
        <div class="settings-card-header">
          <h2>Часы работы</h2>
          <button type="button" class="settings-reset-btn" data-block="hours" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
        </div>
        <p class="card-hint">Отметьте "выходной" для дней, когда клиника не работает</p>
        <div id="hoursGrid"><div class="loading">Загрузка…</div></div>
      </div>
      <div class="card">
        <div class="settings-card-header">
          <h2>Контакты</h2>
          <button type="button" class="settings-reset-btn" data-block="contacts" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
        </div>
        <div class="settings-field">
          <label for="settingsPhone">Телефон</label>
          <input type="text" id="settingsPhone" />
        </div>
        <div class="settings-field">
          <label for="settingsAddress">Адрес</label>
          <input type="text" id="settingsAddress" />
        </div>
        <div class="settings-field">
          <label for="settingsTelegram">Telegram</label>
          <input type="text" id="settingsTelegram" />
        </div>
        <div class="settings-field">
          <label for="settingsWebsite">Сайт</label>
          <input type="text" id="settingsWebsite" />
        </div>
      </div>
      <div class="card">
        <div class="settings-card-header">
          <h2>Виджет</h2>
          <button type="button" class="settings-reset-btn" data-block="widget" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
        </div>
        <div class="settings-field">
          <label for="settingsHeaderTitle">Заголовок чата</label>
          <input type="text" id="settingsHeaderTitle" />
        </div>
        <div class="settings-field">
          <label for="settingsHeaderSubtitle">Подсказка под заголовком</label>
          <input type="text" id="settingsHeaderSubtitle" />
        </div>
        <div class="settings-field">
          <label for="settingsPrimaryColor">Основной цвет</label>
          <input type="text" id="settingsPrimaryColor" placeholder="#1F7A5C" />
        </div>
        <div class="settings-field">
          <label for="settingsButtonColor">Цвет кнопки</label>
          <input type="text" id="settingsButtonColor" placeholder="#1F7A5C" />
        </div>
        <div class="settings-field">
          <label for="settingsPosition">Расположение</label>
          <select id="settingsPosition">
            <option value="bottom-right">Справа снизу</option>
            <option value="bottom-left">Слева снизу</option>
          </select>
        </div>
        <div class="settings-field">
          <label for="settingsAvatarEmoji">Эмодзи в чате</label>
          <input type="text" id="settingsAvatarEmoji" maxlength="4" />
        </div>
      </div>
      <div class="card">
        <div class="settings-card-header">
          <h2>Факты о клинике</h2>
          <button type="button" class="settings-reset-btn" data-block="facts" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
        </div>
        <label class="settings-checkbox"><input type="checkbox" id="factOms" /> Работаем по ОМС</label>
        <label class="settings-checkbox"><input type="checkbox" id="factDms" /> Работаем по ДМС</label>
        <label class="settings-checkbox"><input type="checkbox" id="factAmbulance" /> Скорая помощь привозит к нам</label>
        <label class="settings-checkbox"><input type="checkbox" id="factSells" /> Продаём товары/косметику</label>
        <label class="settings-checkbox"><input type="checkbox" id="factDoctorSchedule" /> Раскрываем расписание врачей</label>
      </div>
      <div class="card">
        <div class="settings-card-header">
          <h2>Врачи</h2>
          <button type="button" class="settings-reset-btn" data-block="doctors" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
        </div>
        <p class="card-hint">Имя обязательно, специализация и расписание — по желанию</p>
        <div id="doctorsList"></div>
        <button type="button" class="doctor-add-btn" id="doctorAddBtn">+ Добавить врача</button>
      </div>
      <div class="card">
        <button type="button" class="settings-save-btn" id="settingsSaveBtn">Сохранить</button>
        <span class="settings-status" id="settingsStatus"></span>
      </div>
    </div>
  </main>

  <script>
    const seriesColors = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)"];

    function escapeHtml(value) {
      return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function fmt(n) {
      return new Intl.NumberFormat("ru-RU").format(n ?? 0);
    }

    function monthLabel(key) {
      const [y, m] = key.split("-");
      const names = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"];
      return names[parseInt(m, 10) - 1] + " " + y.slice(2);
    }

    // Диапазон дат (2026-08-30) — пресеты (days) или "Свой период" (start_date/end_date),
    // общий для вкладок "Дашборд" и "Лиды", поэтому вынесен отдельно, а не зашит в fetchDashboard.
    function currentRangeParams() {
      const select = document.getElementById("daysSelect");
      if (select.value === "custom") {
        const start = document.getElementById("rangeStart").value;
        const end = document.getElementById("rangeEnd").value;
        // обе даты нужны вместе (см. _resolve_date_range на бэкенде) — пока не выбраны обе,
        // не шлём запрос с половиной диапазона (backend всё равно откажет 422-й)
        if (start && end) return { start_date: start, end_date: end };
        return null;
      }
      return { days: select.value };
    }

    function applyRangeParams(rangeParams, target) {
      if (rangeParams.days !== undefined) target.set("days", rangeParams.days);
      if (rangeParams.start_date !== undefined) target.set("start_date", rangeParams.start_date);
      if (rangeParams.end_date !== undefined) target.set("end_date", rangeParams.end_date);
    }

    async function fetchDashboard(companyId, rangeParams) {
      // Живой баг (код-ревью, 2026-08-27): токен раньше читался из ?token= в URL и
      // подставлялся в каждый запрос — та самая утечка (nginx-логи/история браузера),
      // ради ухода от которой и делался cookie-логин. Страница уже прошла проверку
      // verify_operator_token на сервере (иначе редирект на /login), а cookie — HttpOnly и
      // отправляется браузером сама на same-origin fetch, отдельно передавать больше нечего.
      const params = new URLSearchParams();
      if (companyId) params.set("company_id", companyId);
      applyRangeParams(rangeParams, params);
      const res = await fetch(`/api/analytics/dashboard?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }

    const LEAD_REASON_LABELS = {
      booking: "Запись", price_question: "Вопрос о цене", medical_risk: "Консультация",
      commercial_interest: "Интерес к услуге", unknown_service: "Неизвестная услуга",
    };
    const LEAD_TRIGGER_LABELS = {
      ask_contact: "Оставил контакт", booking_request: "Запись",
      regulated_advice: "Мед. вопрос", operator_handoff: "Передано оператору",
    };

    async function fetchLeads(companyId, rangeParams) {
      const params = new URLSearchParams();
      if (companyId) params.set("company_id", companyId);
      applyRangeParams(rangeParams, params);
      const reason = document.getElementById("leadsReasonFilter").value;
      if (reason) params.set("reason", reason);
      const res = await fetch(`/api/analytics/leads?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }

    function renderLeadsTable(leads) {
      if (!leads.length) return '<div class="leads-table-empty">Лидов за этот период не найдено</div>';
      const rows = leads.map((lead) => `
        <tr>
          <td>${escapeHtml((lead.timestamp || "").replace("T", " ").slice(0, 16))}</td>
          <td>${escapeHtml(lead.service_name || "—")}</td>
          <td>${escapeHtml(LEAD_REASON_LABELS[lead.reason] || lead.reason)}</td>
          <td>${escapeHtml(LEAD_TRIGGER_LABELS[lead.lead_trigger] || lead.lead_trigger)}</td>
          <td>${lead.needs_operator ? "да" : "—"}</td>
          <td class="chat-id">${escapeHtml((lead.session_id || "").slice(0, 8))}</td>
        </tr>
      `).join("");
      return `
        <table>
          <thead>
            <tr><th>Дата</th><th>Услуга</th><th>Тип</th><th>Как пришёл</th><th>Нужен оператор</th><th>Сессия</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    }

    async function loadLeads() {
      const wrap = document.getElementById("leadsTableWrap");
      const rangeParams = currentRangeParams();
      if (!rangeParams) {
        wrap.innerHTML = '<div class="leads-table-empty">Выберите обе даты периода сверху</div>';
        return;
      }
      wrap.innerHTML = '<div class="loading">Загрузка…</div>';
      try {
        const companyId = document.getElementById("companySelect").value;
        const data = await fetchLeads(companyId, rangeParams);
        wrap.innerHTML = renderLeadsTable(data.leads);
      } catch (error) {
        wrap.innerHTML = `<div class="error">Не удалось загрузить: ${escapeHtml(error.message)}</div>`;
      }
    }

    // signed % от previous->current; null, когда сравнивать не с чем (previous=0) — тогда
    // просто не показываем бейдж, а не рисуем деление на ноль как "+Infinity%"
    function deltaBadge(current, previous) {
      if (!previous) return "";
      const pct = Math.round(((current - previous) / previous) * 100);
      if (pct === 0) return `<span class="tile-delta flat">±0%</span>`;
      const cls = pct > 0 ? "up" : "down";
      const sign = pct > 0 ? "+" : "";
      return `<span class="tile-delta ${cls}">${sign}${pct}%</span>`;
    }

    function renderTiles(data) {
      const totalLeads = data.summary.leads.total;
      const thisMonth = data.leads_by_month[data.leads_by_month.length - 1];
      const operatorsCount = Object.keys(data.operators).length;

      // "Диалогов"/"Конверсия" И их дельты — всё из period_comparison (current и previous
      // одним расчётом, одно и то же окно по обе стороны). Живой баг (код-ревью,
      // 2026-08-27): раньше "текущее" брали из воронки (окно до 55 дней), а "предыдущее" —
      // отдельно из period_comparison (безопасно зажато до 30 дней ретеншном) — два разных
      // окна сравнивались друг с другом как одно, дельта могла быть технически неверной.
      //
      // Кастомный период (2026-08-30): period_comparison тут null — "N дней vs предыдущие N
      // дней от сегодня" не имеет смысла для произвольного диапазона, backend его сознательно
      // не считает (см. routes/analytics.py). Показываем тайлы без числа/дельты, а не врём.
      const pc = data.period_comparison;
      let conversationsTile, conversionTile;
      if (pc) {
        const windowDays = pc.conversations_days != null ? pc.conversations_days : pc.days;
        const conversations = pc.conversations.current;
        const conversationsDelta = deltaBadge(pc.conversations.current, pc.conversations.previous);
        const conversion = pc.conversations.current > 0
          ? Math.round((pc.leads.current / pc.conversations.current) * 1000) / 10
          : 0;
        const prevConversion = pc.conversations.previous > 0 ? (pc.leads.previous / pc.conversations.previous) * 100 : 0;
        const conversionDelta = deltaBadge(conversion, prevConversion);
        conversationsTile = `<div class="tile"><div class="tile-label">Диалогов (${windowDays} дн.)</div><div class="tile-value">${fmt(conversations)}${conversationsDelta}</div></div>`;
        conversionTile = `<div class="tile"><div class="tile-label">Конверсия (${windowDays} дн.)</div><div class="tile-value">${conversion}%${conversionDelta}</div></div>`;
      } else {
        conversationsTile = `<div class="tile"><div class="tile-label">Диалогов</div><div class="tile-value">—</div></div>`;
        conversionTile = `<div class="tile"><div class="tile-label">Конверсия</div><div class="tile-value">—</div></div>`;
      }

      const waitMinutes = data.queue_wait.avg_wait_minutes;

      return `
        <div class="tiles">
          <div class="tile"><div class="tile-label">Всего лидов</div><div class="tile-value">${fmt(totalLeads)}</div></div>
          <div class="tile"><div class="tile-label">Лидов за месяц</div><div class="tile-value accent">${fmt(thisMonth ? thisMonth.count : 0)}</div></div>
          ${conversationsTile}
          ${conversionTile}
          <div class="tile"><div class="tile-label">Ожидание оператора</div><div class="tile-value">${waitMinutes != null ? waitMinutes + " мин" : "—"}</div></div>
          <div class="tile"><div class="tile-label">Операторов</div><div class="tile-value">${fmt(operatorsCount)}</div></div>
        </div>
      `;
    }

    function renderFunnel(funnel) {
      const stages = funnel.stages;
      // Ширина бара — относительно САМОЙ БОЛЬШОЙ стадии, не обязательно первой. В норме
      // "виджет загружен" и есть максимум (воронка сужается), но пока impression/chat_opened
      // только начали считаться (мало истории), а "есть переписка"/"лид" копились уже давно —
      // с relative-to-stage[0] всё клампилось в 100% и таяло различие. Само выправится, когда
      // обе метрики накопят сопоставимую историю.
      const top = Math.max(...stages.map((s) => s.count), 1);
      const rows = stages.map((stage) => {
        // clamp на 100 — ранние дни жизни воронки (виджет только начал считать импрешны)
        // могут дать "переписок" больше, чем "загрузок виджета", пока обе метрики не
        // накопят сопоставимую историю; честное число остаётся в тексте (percent_of_previous),
        // клампим только ВИЗУАЛЬНУЮ ширину, чтобы бар не вылезал за карточку
        const widthPct = Math.min(Math.max(Math.round((stage.count / top) * 100), stage.count > 0 ? 4 : 0), 100);
        const percentText = stage.percent_of_previous != null
          ? `<span class="funnel-percent">(${stage.percent_of_previous}%)</span>` : "";
        return `
          <div class="funnel-stage">
            <div class="funnel-row">
              <span class="funnel-label">${escapeHtml(stage.label)}</span>
              <span class="funnel-value">${fmt(stage.count)} ${percentText}</span>
            </div>
            <div class="funnel-track">
              <div class="funnel-fill ${stage.count === 0 ? "dim" : ""}" style="width:${widthPct}%">
                <span class="funnel-fill-label">${widthPct >= 12 ? widthPct + "%" : ""}</span>
              </div>
            </div>
          </div>
        `;
      }).join("");
      return `
        <div class="card">
          <h2>Воронка конверсии</h2>
          <p class="card-hint">За последние ${funnel.days} дней · % — от предыдущей стадии</p>
          ${rows}
        </div>
      `;
    }

    const REASON_LABELS = {
      booking: "Запись",
      price_question: "Вопрос о цене",
      medical_risk: "Консультация",
      commercial_interest: "Интерес к услуге",
      unknown_service: "Неизвестная услуга",
    };

    function renderReasonDonut(items) {
      if (!items.length) {
        return `<div class="card"><h2>Лиды по типу</h2><p class="card-hint">За всё время</p><div class="empty-state">Пока нет лидов</div></div>`;
      }
      const total = items.reduce((sum, i) => sum + i.count, 0);
      let cumulative = 0;
      const segments = items.map((item, i) => {
        const color = seriesColors[i % seriesColors.length];
        const startPct = (cumulative / total) * 100;
        cumulative += item.count;
        const endPct = (cumulative / total) * 100;
        return `${color} ${startPct.toFixed(2)}% ${endPct.toFixed(2)}%`;
      }).join(", ");
      const legend = items.map((item, i) => `
        <div class="legend-row">
          <span class="legend-dot" style="background:${seriesColors[i % seriesColors.length]}"></span>
          <span class="legend-label">${escapeHtml(REASON_LABELS[item.reason] || item.reason)}</span>
          <span class="legend-value">${fmt(item.count)} · ${Math.round((item.count / total) * 100)}%</span>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Лиды по типу</h2>
          <p class="card-hint">За всё время</p>
          <div class="donut-wrap">
            <div class="donut" style="background: conic-gradient(${segments})"></div>
            <div class="donut-legend">${legend}</div>
          </div>
        </div>
      `;
    }

    function renderMonthChart(months) {
      const max = Math.max(1, ...months.map((m) => m.count));
      const currentKey = months[months.length - 1]?.month;
      const bars = months.map((m) => {
        const heightPct = Math.round((m.count / max) * 100);
        const isCurrent = m.month === currentKey;
        return `
          <div class="month-col">
            <div class="month-bar-wrap">
              <div class="month-bar ${isCurrent ? "current" : ""}" style="height:${Math.max(heightPct, 3)}%">
                <span class="month-bar-value">${fmt(m.count)}</span>
              </div>
            </div>
            <span class="month-label">${monthLabel(m.month)}</span>
          </div>
        `;
      }).join("");
      return `
        <div class="card">
          <h2>Лиды по месяцам</h2>
          <p class="card-hint">Последние ${months.length} месяцев</p>
          <div class="month-chart">${bars}</div>
        </div>
      `;
    }

    function pluralRu(n, one, few, many) {
      const mod10 = n % 10;
      const mod100 = n % 100;
      if (mod10 === 1 && mod100 !== 11) return one;
      if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
      return many;
    }

    function renderOperators(operators) {
      // "Бот" — синтетическая запись (см. analytics.py:operator_summary), не человек-оператор:
      // claimed/closed/avg_dialog_minutes для него всегда пустые, только leads осмысленный.
      // Раньше сидел строкой в общей таблице вперемешку с людьми — 0/0/"—" в трёх колонках
      // выглядело как мусор. Теперь отдельная плашка сверху, люди — в таблице ниже (2026-08-29).
      const botStats = operators["Бот"];
      const humanEntries = Object.entries(operators)
        .filter(([name]) => name !== "Бот")
        .sort((a, b) => (b[1].leads || 0) - (a[1].leads || 0));

      const botBanner = botStats
        ? `
          <div class="bot-banner">
            <span class="bot-banner-icon">🤖</span>
            <span class="bot-banner-text">
              Бот — <strong>${fmt(botStats.leads)}</strong> ${pluralRu(botStats.leads, "лид", "лида", "лидов")}
              самостоятельно, без оператора
            </span>
          </div>
        `
        : "";

      if (!humanEntries.length) {
        return `
          <div class="card">
            <h2>Операторы</h2>
            <p class="card-hint">За всё время</p>
            ${botBanner}
            <div class="empty-state">Пока нет ни одного взятого в работу диалога</div>
          </div>
        `;
      }
      const rows = humanEntries.map(([name, stats]) => `
        <tr>
          <td class="operator-name">${escapeHtml(name)}</td>
          <td class="num">${fmt(stats.claimed)}</td>
          <td class="num">${fmt(stats.closed)}</td>
          <td class="num">${fmt(stats.leads)}</td>
          <td class="num">${stats.avg_dialog_minutes != null ? stats.avg_dialog_minutes + " мин" : "—"}</td>
        </tr>
      `).join("");
      return `
        <div class="card">
          <h2>Операторы</h2>
          <p class="card-hint">За всё время</p>
          ${botBanner}
          <table>
            <thead><tr><th>Оператор</th><th class="num">Взято</th><th class="num">Закрыто</th><th class="num">Лидов</th><th class="num">Ср. время</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `;
    }

    function renderTopServices(services) {
      if (!services.length) {
        return `<div class="card"><h2>Топ услуг</h2><p class="card-hint">По числу лидов</p><div class="empty-state">Пока нет лидов с привязкой к услуге</div></div>`;
      }
      const max = Math.max(...services.map((s) => s.count));
      const rows = services.map((s, i) => `
        <div class="service-row">
          <div class="service-name" title="${escapeHtml(s.service_name)}">${escapeHtml(s.service_name)}</div>
          <div class="service-bar-track">
            <div class="service-bar-fill" style="width:${Math.round((s.count / max) * 100)}%; background:${seriesColors[i % seriesColors.length]}"></div>
          </div>
          <div class="service-count">${fmt(s.count)}</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Топ услуг</h2>
          <p class="card-hint">По числу лидов</p>
          ${rows}
        </div>
      `;
    }

    const INTENT_LABELS = {
      ok: "Обычный ответ", price_question: "Цена", price_question_no_service: "Цена (без услуги)",
      list_services: "Список услуг", small_talk: "Смолток", off_topic: "Офтоп",
      off_topic_body_redirect: "Офтоп (про тело)", operator_requested: "Просьба оператора",
      booking_request: "Запись", contact_provided: "Контакт получен", lead_request: "Лид",
      cosmetic_concern: "Косметический вопрос", medical_advice: "Мед. вопрос",
      regulated_advice: "Регулируемый мед. вопрос", unknown_service: "Неизвестная услуга",
      similar_services_found: "Похожие услуги", contact_link: "Ссылка/контакт",
      location_mismatch: "Несовпадение города", unsupported_city: "Город не обслуживаем",
      service_mention: "Упоминание услуги", service_explanation: "Объяснение услуги",
      duration_question: "Вопрос про сроки", faq_question: "Частый вопрос", quick_faq: "Быстрый ответ (FAQ)",
      objection_handled: "Возражение", objection_backoff: "Возражение (повтор)",
      // self_harm_crisis: нейтральная подпись в чарте намеренно — "Кризис (самоповреждение)"
      // рядом со "Смолток"/"Цена" в общем bar-чарте читалось как ещё одна маркетинговая
      // метрика, резало глаз (обсуждено с пользователем 2026-08-29). Сам intent-ключ и вся
      // обработка в policy/ не менялись, только отображаемая строка на дашборде.
      complaint: "Жалоба", self_harm_crisis: "Особое внимание", out_of_scope: "Вне зоны ответственности",
      unknown: "Неизвестно",
    };

    function renderIntentBreakdown(items) {
      if (!items.length) {
        return `<div class="card"><h2>Разбивка по темам</h2><p class="card-hint">За период</p><div class="empty-state">Пока нет данных</div></div>`;
      }
      const max = Math.max(...items.map((i) => i.count));
      const rows = items.map((item, i) => `
        <div class="service-row">
          <div class="service-name" title="${escapeHtml(item.reason)}">${escapeHtml(INTENT_LABELS[item.reason] || item.reason)}</div>
          <div class="service-bar-track">
            <div class="service-bar-fill" style="width:${Math.round((item.count / max) * 100)}%; background:${seriesColors[i % seriesColors.length]}"></div>
          </div>
          <div class="service-count">${fmt(item.count)}</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Разбивка по темам</h2>
          <p class="card-hint">О чём чаще всего спрашивают</p>
          ${rows}
        </div>
      `;
    }

    const OBJECTION_TOPIC_LABELS = {
      price: "Цена", hesitation: "Сомнение / надо подумать", competitor: "Сравнение с конкурентом",
      guarantee: "Вопрос про гарантию", pain_fear: "Страх боли / побочек", unknown: "Неизвестно",
    };

    function renderObjectionBreakdown(items) {
      if (!items.length) {
        return `<div class="card"><h2>Возражения по теме</h2><p class="card-hint">За период</p><div class="empty-state">Пока нет данных</div></div>`;
      }
      const max = Math.max(...items.map((i) => i.count));
      const rows = items.map((item, i) => `
        <div class="service-row">
          <div class="service-name" title="${escapeHtml(item.topic)}">${escapeHtml(OBJECTION_TOPIC_LABELS[item.topic] || item.topic)}</div>
          <div class="service-bar-track">
            <div class="service-bar-fill" style="width:${Math.round((item.count / max) * 100)}%; background:${seriesColors[i % seriesColors.length]}"></div>
          </div>
          <div class="service-count">${fmt(item.count)}</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Возражения по теме</h2>
          <p class="card-hint">С чем чаще всего спорят/сомневаются</p>
          ${rows}
        </div>
      `;
    }

    function renderUnansweredTrend(trend) {
      const max = Math.max(1, ...trend.map((d) => d.count));
      const bars = trend.map((d) => {
        const heightPct = Math.round((d.count / max) * 100);
        const dt = new Date(d.date + "T00:00:00");
        const label = dt.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
        return `
          <div class="month-col">
            <div class="month-bar-wrap">
              <div class="month-bar" style="height:${Math.max(heightPct, d.count > 0 ? 3 : 1)}%">
                <span class="month-bar-value">${fmt(d.count)}</span>
              </div>
            </div>
            <span class="month-label">${trend.length <= 16 ? label : "&nbsp;"}</span>
          </div>
        `;
      }).join("");
      return `
        <div class="card">
          <h2>Нераспознанные вопросы — тренд</h2>
          <p class="card-hint">Растёт или деградирует база знаний, по дням</p>
          <div class="month-chart">${bars}</div>
        </div>
      `;
    }

    function renderActivityByHour(hours) {
      const max = Math.max(1, ...hours.map((h) => h.count));
      const bars = hours.map((h) => `
        <div class="month-col">
          <div class="month-bar-wrap">
            <div class="month-bar" style="height:${Math.max(Math.round((h.count / max) * 100), h.count > 0 ? 3 : 1)}%">
              <span class="month-bar-value">${fmt(h.count)}</span>
            </div>
          </div>
          <span class="month-label">${h.hour % 3 === 0 ? h.hour : "&nbsp;"}</span>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Активность по часам</h2>
          <p class="card-hint">Сообщений по часу суток (UTC)</p>
          <div class="month-chart">${bars}</div>
        </div>
      `;
    }

    function renderActivityByWeekday(days) {
      const max = Math.max(1, ...days.map((d) => d.count));
      const bars = days.map((d) => `
        <div class="month-col">
          <div class="month-bar-wrap">
            <div class="month-bar" style="height:${Math.max(Math.round((d.count / max) * 100), d.count > 0 ? 3 : 1)}%">
              <span class="month-bar-value">${fmt(d.count)}</span>
            </div>
          </div>
          <span class="month-label">${d.label}</span>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Активность по дням недели</h2>
          <p class="card-hint">Сообщений по дню недели</p>
          <div class="month-chart">${bars}</div>
        </div>
      `;
    }

    function truncate(text, maxLen) {
      return text.length > maxLen ? text.slice(0, maxLen).trimEnd() + "…" : text;
    }

    function renderTopQuestions(title, hint, items) {
      // Группировка по точному тексту (см. top_unanswered_questions/top_answered_questions в
      // analytics.py) — разные формулировки одного вопроса считаются отдельно, это осознанное
      // упрощение первой версии, не баг.
      if (!items.length) {
        return `<div class="card"><h2>${escapeHtml(title)}</h2><p class="card-hint">${escapeHtml(hint)}</p><div class="empty-state">Пока нет данных</div></div>`;
      }
      // Живой баг (2026-08-29): реальное сообщение из смоук-теста оказалось на 3000+ символов
      // спама ("аааа…") — без обрезки такая строка разносила вёрстку карточки целиком.
      const rows = items.map((item) => `
        <div class="feed-item">
          <div class="feed-text">${escapeHtml(truncate(item.message, 80))}</div>
          <div class="feed-meta">${fmt(item.count)} раз${item.count === 1 ? "" : "а"}</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>${escapeHtml(title)}</h2>
          <p class="card-hint">${escapeHtml(hint)}</p>
          ${rows}
        </div>
      `;
    }

    function renderUnanswered(items) {
      if (!items.length) {
        return `<div class="card"><h2>Нераспознанные вопросы</h2><p class="card-hint">Последние ${items.length}</p><div class="empty-state">Ничего нет — база знаний покрывает все вопросы</div></div>`;
      }
      const rows = items.slice(0, 10).map((item) => `
        <div class="feed-item">
          <div class="feed-text">${escapeHtml(item.message || "—")}</div>
          <div class="feed-meta">${escapeHtml((item.timestamp || "").replace("T", " ").slice(0, 16))}</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Нераспознанные вопросы</h2>
          <p class="card-hint">Последние ${Math.min(items.length, 10)}</p>
          ${rows}
        </div>
      `;
    }

    let currentChatScope = "all";
    const chatTranscriptCache = {};

    async function fetchChats(companyId, scope) {
      const params = new URLSearchParams();
      if (companyId) params.set("company_id", companyId);
      params.set("scope", scope);
      const res = await fetch(`/api/analytics/chats?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }

    async function fetchChatDetail(sessionId) {
      const res = await fetch(`/api/analytics/chats/${encodeURIComponent(sessionId)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }

    function chatBadges(item) {
      const badges = [];
      if (item.operator_requested) badges.push('<span class="chat-badge operator">Оператор</span>');
      if (item.lead_requested) badges.push('<span class="chat-badge lead">Лид</span>');
      return badges.join("");
    }

    function renderTranscriptMessages(messages) {
      if (!messages.length) return '<div class="empty-state">Сообщений нет</div>';
      return messages.map((m) => `
        <div class="t-msg ${escapeHtml(m.role)}">
          <div class="t-msg-role">${escapeHtml(m.role)}</div>
          <div>${escapeHtml(m.text)}</div>
        </div>
      `).join("");
    }

    // Список слева / полная переписка справа (master-detail, "как в диалогах") — раньше
    // список раскрывался инлайн под каждой строкой, приходилось листать всю ленту. Теперь
    // строка только выделяется, транскрипт всегда справа. chatRowsById — метаданные строки
    // (бейджи/время) для шапки детали, отдельно от chatTranscriptCache (сами сообщения,
    // по-прежнему кэшируются, чтобы повторный клик на уже открытый чат не бил по сети).
    let chatRowsById = {};
    let activeChatSessionId = null;

    function renderChatRow(item) {
      const time = (item.updated_at || "").replace("T", " ").slice(0, 16);
      return `
        <div class="chat-row" data-session-id="${escapeHtml(item.session_id)}">
          <div class="chat-row-top">
            <span class="chat-id">${escapeHtml(item.session_id.slice(0, 8))}</span>
            ${chatBadges(item)}
            <span class="chat-time">${escapeHtml(time)}</span>
          </div>
          <div class="chat-preview">${escapeHtml(item.last_message || "—")}</div>
        </div>
      `;
    }

    function chatDetailHeader(sessionId) {
      const item = chatRowsById[sessionId];
      if (!item) return "";
      const time = (item.updated_at || "").replace("T", " ").slice(0, 16);
      return `
        <div class="chat-detail-header">
          <span class="chat-id">${escapeHtml(sessionId.slice(0, 8))}</span>
          ${chatBadges(item)}
          <span class="chat-time">${escapeHtml(time)}</span>
        </div>
      `;
    }

    async function selectChat(sessionId) {
      activeChatSessionId = sessionId;
      document.querySelectorAll(".chat-row").forEach((row) => {
        row.classList.toggle("active", row.dataset.sessionId === sessionId);
      });
      const detail = document.getElementById("chatDetail");
      const header = chatDetailHeader(sessionId);
      detail.innerHTML = header + '<div class="loading">Загрузка…</div>';
      try {
        const data = chatTranscriptCache[sessionId] || await fetchChatDetail(sessionId);
        chatTranscriptCache[sessionId] = data;
        // пока грузилось — могли кликнуть на другой чат, не перетираем чужой выбор
        if (activeChatSessionId !== sessionId) return;
        detail.innerHTML = header + renderTranscriptMessages(data.messages);
      } catch (error) {
        if (activeChatSessionId !== sessionId) return;
        detail.innerHTML = header + `<div class="error">Не удалось загрузить: ${escapeHtml(error.message)}</div>`;
      }
    }

    async function loadChats() {
      const list = document.getElementById("chatsList");
      const companyId = document.getElementById("companySelect").value;
      list.innerHTML = '<div class="loading">Загрузка…</div>';
      activeChatSessionId = null;
      document.getElementById("chatDetail").innerHTML = '<div class="empty-state">Выберите диалог слева</div>';
      try {
        const data = await fetchChats(companyId, currentChatScope);
        chatRowsById = {};
        data.conversations.forEach((item) => { chatRowsById[item.session_id] = item; });
        list.innerHTML = data.conversations.length
          ? data.conversations.map(renderChatRow).join("")
          : '<div class="empty-state">Диалогов не найдено</div>';
        if (data.conversations.length) selectChat(data.conversations[0].session_id);
      } catch (error) {
        list.innerHTML = `<div class="error">Не удалось загрузить: ${escapeHtml(error.message)}</div>`;
      }
    }

    function switchTab(tab) {
      document.getElementById("tabDashboardBtn").classList.toggle("active", tab === "dashboard");
      document.getElementById("tabChatsBtn").classList.toggle("active", tab === "chats");
      document.getElementById("tabLeadsBtn").classList.toggle("active", tab === "leads");
      document.getElementById("tabSettingsBtn").classList.toggle("active", tab === "settings");
      document.getElementById("content").style.display = tab === "dashboard" ? "" : "none";
      document.getElementById("chatsContent").style.display = tab === "chats" ? "" : "none";
      document.getElementById("leadsContent").style.display = tab === "leads" ? "" : "none";
      document.getElementById("settingsContent").style.display = tab === "settings" ? "" : "none";
      if (tab === "chats") loadChats();
      if (tab === "leads") loadLeads();
      if (tab === "settings") loadSettings();
    }

    const WEEKDAY_LABELS = { mon: "Пн", tue: "Вт", wed: "Ср", thu: "Чт", fri: "Пт", sat: "Сб", sun: "Вс" };
    const WEEKDAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

    function renderHoursGrid(schedule) {
      return WEEKDAY_ORDER.map((day) => {
        const entry = schedule[day];
        const closed = entry === null || entry === undefined;
        const open = entry ? entry.open : "10:00";
        const close = entry ? entry.close : "20:00";
        return `
          <div class="hours-row" data-day="${day}">
            <span class="hours-day-label">${WEEKDAY_LABELS[day]}</span>
            <input type="time" class="hours-open" value="${open}" ${closed ? "disabled" : ""} />
            <span>—</span>
            <input type="time" class="hours-close" value="${close}" ${closed ? "disabled" : ""} />
            <label class="hours-closed-toggle">
              <input type="checkbox" class="hours-closed" ${closed ? "checked" : ""} /> выходной
            </label>
          </div>
        `;
      }).join("");
    }

    function bindHoursClosedToggles() {
      document.querySelectorAll(".hours-closed").forEach((checkbox) => {
        checkbox.addEventListener("change", () => {
          const row = checkbox.closest(".hours-row");
          row.querySelector(".hours-open").disabled = checkbox.checked;
          row.querySelector(".hours-close").disabled = checkbox.checked;
        });
      });
    }

    function collectHoursSchedule() {
      const schedule = {};
      document.querySelectorAll(".hours-row").forEach((row) => {
        const day = row.dataset.day;
        const closed = row.querySelector(".hours-closed").checked;
        schedule[day] = closed
          ? null
          : {
              open: row.querySelector(".hours-open").value,
              close: row.querySelector(".hours-close").value,
            };
      });
      return schedule;
    }

    function doctorRowHtml(doctor) {
      const name = doctor && doctor.name ? doctor.name : "";
      const specialty = doctor && doctor.specialty ? doctor.specialty : "";
      const schedule = doctor && doctor.schedule ? doctor.schedule : "";
      return `
        <div class="doctor-row">
          <input type="text" class="doctor-name" placeholder="Имя" value="${escapeHtml(name)}" />
          <input type="text" class="doctor-specialty" placeholder="Специализация" value="${escapeHtml(specialty)}" />
          <input type="text" class="doctor-schedule" placeholder="Расписание" value="${escapeHtml(schedule)}" />
          <button type="button" class="doctor-remove-btn" title="Удалить">×</button>
        </div>
      `;
    }

    function renderDoctorsList(doctors) {
      return (doctors || []).map(doctorRowHtml).join("");
    }

    function addDoctorRow() {
      const list = document.getElementById("doctorsList");
      list.insertAdjacentHTML("beforeend", doctorRowHtml(null));
      const rows = list.querySelectorAll(".doctor-row");
      rows[rows.length - 1].querySelector(".doctor-name").focus();
    }

    function collectDoctors() {
      const doctors = [];
      document.querySelectorAll(".doctor-row").forEach((row) => {
        const name = row.querySelector(".doctor-name").value.trim();
        if (!name) return;
        doctors.push({
          name,
          specialty: row.querySelector(".doctor-specialty").value.trim(),
          schedule: row.querySelector(".doctor-schedule").value.trim(),
        });
      });
      return doctors;
    }

    document.getElementById("doctorsList").addEventListener("click", (event) => {
      if (event.target.classList.contains("doctor-remove-btn")) {
        event.target.closest(".doctor-row").remove();
      }
    });
    document.getElementById("doctorAddBtn").addEventListener("click", addDoctorRow);

    function settingsCompanyId() {
      return document.getElementById("companySelect").value || "rosh_import_demo";
    }

    async function loadSettings() {
      const status = document.getElementById("settingsStatus");
      status.textContent = "";
      status.className = "settings-status";
      try {
        const response = await fetch(
          `/api/settings/company?company_id=${encodeURIComponent(settingsCompanyId())}`
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        document.getElementById("hoursGrid").innerHTML = renderHoursGrid(data.working_hours_schedule);
        bindHoursClosedToggles();
        document.getElementById("settingsPhone").value = data.phone || "";
        document.getElementById("settingsAddress").value = data.address || "";
        document.getElementById("settingsTelegram").value = data.telegram_url || "";
        document.getElementById("settingsWebsite").value = data.website_url || "";
        document.getElementById("settingsHeaderTitle").value = data.widget.header_title || "";
        document.getElementById("settingsHeaderSubtitle").value = data.widget.header_subtitle || "";
        document.getElementById("settingsPrimaryColor").value = data.widget.primary_color || "";
        document.getElementById("settingsButtonColor").value = data.widget.button_color || "";
        document.getElementById("settingsPosition").value = data.widget.position || "bottom-right";
        document.getElementById("settingsAvatarEmoji").value = data.widget.avatar_emoji || "";
        document.getElementById("factOms").checked = Boolean(data.facts.oms);
        document.getElementById("factDms").checked = Boolean(data.facts.dms);
        document.getElementById("factAmbulance").checked = Boolean(data.facts.ambulance_brings);
        document.getElementById("factSells").checked = Boolean(data.facts.sells_products);
        document.getElementById("factDoctorSchedule").checked = Boolean(data.facts.discloses_doctor_schedule);
        document.getElementById("doctorsList").innerHTML = renderDoctorsList(data.doctors);
      } catch (error) {
        document.getElementById("hoursGrid").innerHTML = "";
        document.getElementById("doctorsList").innerHTML = "";
        status.textContent = `Не удалось загрузить: ${escapeHtml(error.message)}`;
        status.className = "settings-status error";
      }
    }

    async function saveSettings() {
      const status = document.getElementById("settingsStatus");
      const button = document.getElementById("settingsSaveBtn");
      button.disabled = true;
      status.textContent = "Сохраняю…";
      status.className = "settings-status";
      const payload = {
        phone: document.getElementById("settingsPhone").value,
        address: document.getElementById("settingsAddress").value,
        telegram_url: document.getElementById("settingsTelegram").value,
        website_url: document.getElementById("settingsWebsite").value,
        working_hours_schedule: collectHoursSchedule(),
        widget: {
          primary_color: document.getElementById("settingsPrimaryColor").value,
          button_color: document.getElementById("settingsButtonColor").value,
          header_title: document.getElementById("settingsHeaderTitle").value,
          header_subtitle: document.getElementById("settingsHeaderSubtitle").value,
          position: document.getElementById("settingsPosition").value,
          avatar_emoji: document.getElementById("settingsAvatarEmoji").value,
        },
        facts: {
          oms: document.getElementById("factOms").checked,
          dms: document.getElementById("factDms").checked,
          ambulance_brings: document.getElementById("factAmbulance").checked,
          sells_products: document.getElementById("factSells").checked,
          discloses_doctor_schedule: document.getElementById("factDoctorSchedule").checked,
        },
        doctors: collectDoctors(),
      };
      try {
        const response = await fetch(
          `/api/settings/company?company_id=${encodeURIComponent(settingsCompanyId())}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          }
        );
        if (!response.ok) {
          const errorBody = await response.json().catch(() => ({}));
          throw new Error(
            typeof errorBody.detail === "string" ? errorBody.detail : `HTTP ${response.status}`
          );
        }
        status.textContent = "Сохранено";
        status.className = "settings-status success";
      } catch (error) {
        status.textContent = `Ошибка: ${escapeHtml(error.message)}`;
        status.className = "settings-status error";
      } finally {
        button.disabled = false;
      }
    }

    async function resetSettingsBlock(block, button) {
      // Один уровень отмены на блок ("Часы работы", "Контакты" и т.д.) — откатывает
      // ИМЕННО этот блок к состоянию перед последним сохранением (бэкап пишет
      // save_overrides_atomic на каждое сохранение), остальные блоки не трогает, даже
      // если их сохраняли позже. Обсуждено с пользователем 2026-08-29 — полной истории
      // версий сознательно нет, только один шаг назад.
      const status = document.getElementById("settingsStatus");
      button.disabled = true;
      status.textContent = "Отменяю…";
      status.className = "settings-status";
      try {
        const response = await fetch(
          `/api/settings/company/reset-block?company_id=${encodeURIComponent(settingsCompanyId())}&block=${encodeURIComponent(block)}`,
          { method: "POST" }
        );
        if (!response.ok) {
          const errorBody = await response.json().catch(() => ({}));
          throw new Error(
            typeof errorBody.detail === "string" ? errorBody.detail : `HTTP ${response.status}`
          );
        }
        await loadSettings();
        status.textContent = "Отменено";
        status.className = "settings-status success";
      } catch (error) {
        status.textContent = `Ошибка: ${escapeHtml(error.message)}`;
        status.className = "settings-status error";
      } finally {
        button.disabled = false;
      }
    }

    async function load() {
      const content = document.getElementById("content");
      const companyId = document.getElementById("companySelect").value;
      const rangeParams = currentRangeParams();
      if (!rangeParams) {
        content.innerHTML = '<div class="empty-state">Выберите обе даты периода сверху</div>';
        return;
      }
      content.innerHTML = '<div class="loading">Загрузка…</div>';
      try {
        const data = await fetchDashboard(companyId, rangeParams);
        content.innerHTML = `
          ${renderTiles(data)}
          ${renderFunnel(data.funnel)}
          <div class="grid-2">
            ${renderMonthChart(data.leads_by_month)}
            ${renderOperators(data.operators)}
          </div>
          <div class="grid-2">
            ${renderTopServices(data.top_services)}
            ${renderReasonDonut(data.leads_by_reason)}
          </div>
          <div class="grid-2">
            ${renderActivityByHour(data.activity_by_hour)}
            ${renderActivityByWeekday(data.activity_by_weekday)}
          </div>
          ${renderIntentBreakdown(data.intent_breakdown)}
          ${renderObjectionBreakdown(data.objection_breakdown)}
          <div class="grid-2">
            ${renderTopQuestions("Топ непонятых вопросов", "По частоте точного текста", data.top_unanswered_questions)}
            ${renderTopQuestions("Топ частых вопросов", "По частоте точного текста", data.top_answered_questions)}
          </div>
          <!-- Тренд нераспознанных вопросов сознательно скрыт с публичной страницы
               (2026-08-27) — данные остаются в /api/analytics/dashboard (unanswered_trend),
               чтобы проверять вручную, не показывая возможную регрессию базы знаний всем,
               кто смотрит дашборд. См. renderUnansweredTrend, если понадобится вернуть. -->
          ${renderUnanswered(data.summary.unanswered)}
        `;
      } catch (error) {
        content.innerHTML = `<div class="error">Не удалось загрузить: ${escapeHtml(error.message)}</div>`;
      }
    }

    document.getElementById("companySelect").addEventListener("change", () => {
      load();
      if (document.getElementById("chatsContent").style.display !== "none") loadChats();
      if (document.getElementById("leadsContent").style.display !== "none") loadLeads();
      if (document.getElementById("settingsContent").style.display !== "none") loadSettings();
    });
    // "Свой период" (2026-08-30) — показывает/прячет поля дат, перезагружает и дашборд, и
    // "Лиды", если та сейчас открыта (общий диапазон для обеих вкладок).
    function reloadForActiveRangeTab() {
      load();
      if (document.getElementById("leadsContent").style.display !== "none") loadLeads();
    }
    document.getElementById("daysSelect").addEventListener("change", () => {
      const isCustom = document.getElementById("daysSelect").value === "custom";
      document.getElementById("customRangeInputs").style.display = isCustom ? "" : "none";
      if (!isCustom) reloadForActiveRangeTab();
    });
    document.getElementById("rangeStart").addEventListener("change", reloadForActiveRangeTab);
    document.getElementById("rangeEnd").addEventListener("change", reloadForActiveRangeTab);
    document.getElementById("leadsReasonFilter").addEventListener("change", loadLeads);

    document.getElementById("tabDashboardBtn").addEventListener("click", () => switchTab("dashboard"));
    document.getElementById("tabChatsBtn").addEventListener("click", () => switchTab("chats"));
    document.getElementById("tabLeadsBtn").addEventListener("click", () => switchTab("leads"));
    document.getElementById("tabSettingsBtn").addEventListener("click", () => switchTab("settings"));
    document.getElementById("settingsSaveBtn").addEventListener("click", saveSettings);
    document.querySelectorAll(".settings-reset-btn").forEach((btn) => {
      btn.addEventListener("click", () => resetSettingsBlock(btn.dataset.block, btn));
    });
    document.querySelectorAll(".filter-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        currentChatScope = btn.dataset.scope;
        loadChats();
      });
    });
    document.getElementById("chatsList").addEventListener("click", (event) => {
      const row = event.target.closest(".chat-row");
      if (row) selectChat(row.dataset.sessionId);
    });

    load();
  </script>
</body>
</html>
"""
    return html.replace("__COMPANY_SELECT_HTML__", company_select_html)

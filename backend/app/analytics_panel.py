"""Веб-страница аналитики (2026-08-27) — сводка, операторы, лиды по месяцам, топ услуг.

Стиль чата (виджета), не оператор-панели: тёплый светлый фон, скруглённые карточки, мягкие
тени — тот же язык, что и в widget.js (--bg/--text/--accent-soft), но без копии шрифта
(широкий встроенный woff2 не тащим ради внутреннего инструмента). Цвета данных в графиках —
из проверенной валидатором дефолтной палитры дата-виз скилла (CVD-safe, не "на глаз")."""


def render_analytics_panel(
    *,
    default_company_id: str = "rosh_import_demo",
    show_company_selector: bool = True,
    known_company_ids: list[str] | None = None,
) -> str:
    """default_company_id/show_company_selector (2026-08-29) — /analytics (для клиента,
    когда/если отдадим доступ) вызывает это с show_company_selector=False: дропдаун скрыт,
    клиент никогда не узнает, что вообще существует второй company_id для тестового трафика
    (см. /backstage в main.py). Сам JS-код (loadChats/load/companySelect-listener) не тронут —
    он как читал .value у #companySelect, так и читает; при show_company_selector=False это
    просто select с одним вариантом и без визуального намёка на выбор.

    known_company_ids (2026-09-22) — раньше список из двух id РОШ был вписан прямо сюда;
    теперь main.py передаёт его из настроек, дефолт здесь только на случай вызова без
    аргумента (сохраняет прежнее поведение)."""

    if show_company_selector:
        # Живой баг (найден при живой проверке в браузере, 2026-08-29): раньше "rosh_test"
        # было захардкожено вторым вариантом безусловно — при default_company_id="rosh_test"
        # (ровно случай /backstage) это давало ДВЕ одинаковые опции и ни одной для
        # rosh_import_demo. Теперь берём оба известных id, ставим default_company_id первым/
        # выбранным, остальные — следом, без дублей независимо от того, что передали дефолтом.
        known_company_ids = known_company_ids or ["rosh_test", "rosh_import_demo"]
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
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }
    @media (max-width: 860px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    .tile-caption { font-size: 12px; color: var(--text-muted); margin-top: 6px; }
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
    /* иначе широкая таблица или график распирают колонку и вылезают за край на телефоне */
    .grid-2 > * { min-width: 0; }
    .grid-2.even { grid-template-columns: 1fr 1fr; }
    @media (max-width: 860px) { .grid-2.even { grid-template-columns: 1fr; } }
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
    .card .card-hint.funnel-teaser { margin: 16px 0 0; }

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
      background: color-mix(in srgb, var(--accent-soft) 60%, var(--border));
      border-radius: 4px 4px 0 0;
      position: relative;
      transition: background .15s;
      min-height: 3px;
    }
    .month-bar.current { background: var(--accent-deep); }
    .month-chart.labeled { padding-top: 22px; }
    .month-chart.labeled .month-bar-value { opacity: 1; }
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
    /* пять колонок в половине экрана: компактная шапка без капса, иначе таблица уезжает вбок */
    .operators-table thead th { text-transform: none; letter-spacing: 0; font-size: 12px; padding: 0 6px 10px; }
    .operators-table tbody td { padding: 12px 6px; }
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
    .service-bar-fill { height: 100%; border-radius: 4px; background: var(--accent-deep); }
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
    .funnel-sub { font-size: 12px; font-weight: 500; color: var(--text-muted); margin-left: 6px; }
    .table-scroll { overflow-x: auto; }
    .pages-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .pages-table th { text-align: left; font-weight: 600; color: var(--text-muted); padding: 6px 8px; border-bottom: 1px solid var(--border-soft); }
    .pages-table td { padding: 7px 8px; border-bottom: 1px solid var(--border-soft); }
    .pages-table .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .page-path { max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; }
    .funnel-fill-label { font-size: 11.5px; font-weight: 700; color: #fff; white-space: nowrap; }

    .service-share { flex: 0 0 40px; text-align: right; font-size: 12px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
    .funnel-tier-note { font-size: 12px; color: var(--text-muted); margin: 4px 0 14px; }

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
    /* на телефоне четыре вкладки иначе не влезают и сдвигают всю страницу вбок */
    @media (max-width: 520px) {
      .tabs { gap: 0; }
      .tab-btn { padding: 10px 11px; white-space: nowrap; }
    }

    /* ── вкладка "Чаты" ── */
    .chat-filters { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
    .filter-btn {
      font: inherit; font-size: 13px; font-weight: 600; padding: 8px 14px; border-radius: 999px;
      border: 1px solid var(--border); background: var(--card); color: var(--text-secondary); cursor: pointer;
    }
    .filter-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .filter-btn:hover:not(.active) { background: var(--border-soft); }
    /* выгрузка диалогов (только /backstage) — свой класс, не .filter-btn: на .filter-btn висит
       переключение scope, иначе кнопка выгрузки перезагружала бы список */
    .chat-export { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-left: auto; }
    .export-btn {
      font: inherit; font-size: 13px; font-weight: 600; padding: 8px 14px; border-radius: 999px;
      border: 1px solid var(--border); background: var(--card); color: var(--text-secondary); cursor: pointer;
    }
    .export-btn:hover:not(:disabled) { background: var(--border-soft); }
    .export-btn:disabled { opacity: 0.5; cursor: default; }
    .export-status { font-size: 12px; color: var(--text-muted); }
    .chat-check { margin: 0; cursor: pointer; accent-color: var(--accent); }

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
    .settings-field input[type="number"] { font: inherit; font-size: 14px; padding: 9px 12px; border-radius: var(--field-radius); border: 1px solid var(--border); background: var(--bg); color: var(--text); width: 110px; }
    .text-group { border: 1px solid var(--border-soft); border-radius: 10px; margin-bottom: 10px; }
    .text-group > summary { cursor: pointer; padding: 11px 14px; font-weight: 700; font-size: 14px; list-style: none; }
    .text-group > summary::-webkit-details-marker { display: none; }
    .text-group > summary::before { content: "▸ "; color: var(--text-muted); }
    .text-group[open] > summary::before { content: "▾ "; }
    .text-group-count { color: var(--text-muted); font-weight: 500; margin-left: 6px; }
    .text-item { padding: 12px 14px; border-top: 1px solid var(--border-soft); }
    .text-item-head { display: flex; align-items: center; gap: 8px; }
    .text-item-label { font-weight: 600; font-size: 13.5px; }
    .text-badge { font-size: 11px; font-weight: 700; color: var(--accent-deep); background: var(--accent-soft, #e7f2ec); border-radius: 999px; padding: 1px 8px; }
    .text-item-where { font-size: 12.5px; color: var(--text-muted); margin: 3px 0 8px; }
    .text-variant { display: block; width: 100%; max-width: 680px; box-sizing: border-box; font: inherit; font-size: 13.5px; line-height: 1.45; padding: 8px 11px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg); color: var(--text); margin-bottom: 6px; resize: vertical; }
    .text-variant:focus { outline: none; border-color: var(--accent-deep); }
    .text-item-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12.5px; }
    .text-ph { color: var(--text-muted); }
    .text-item-actions button { font: inherit; font-size: 12.5px; border: 1px solid var(--border); background: var(--bg); color: var(--text-secondary); border-radius: 8px; padding: 4px 10px; cursor: pointer; }
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
    .doctor-row .doctor-name { flex: 1.4 1 230px; min-width: 140px; }
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

    /* раскладка «Настроек»: меню разделов слева, справа один раздел — без простыни вниз */
    .settings-layout { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 16px; align-items: start; }
    .settings-nav {
      position: sticky; top: 16px; display: flex; flex-direction: column; gap: 2px;
      background: var(--card); border-radius: var(--radius-sm); box-shadow: var(--shadow); padding: 8px;
    }
    .settings-nav-btn {
      position: relative; display: flex; align-items: center; gap: 10px; width: 100%;
      font: inherit; font-size: 14px; font-weight: 600; text-align: left; white-space: nowrap;
      padding: 10px 12px; border: 0; border-radius: 12px; background: transparent;
      color: var(--text-secondary); cursor: pointer; transition: background .15s, color .15s;
    }
    .settings-nav-btn svg {
      width: 18px; height: 18px; flex: none; fill: none; stroke: currentColor;
      stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round;
    }
    .settings-nav-btn:hover { background: var(--border-soft); color: var(--text); }
    .settings-nav-btn.active { background: color-mix(in srgb, var(--accent-soft) 32%, var(--card)); color: var(--text); }
    /* точка — в разделе есть несохранённые правки: видно, даже когда открыт другой */
    .settings-nav-btn.changed::after {
      content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--accent-deep); margin-left: auto;
    }
    .settings-section { display: none; }
    .settings-section.active { display: block; }
    /* end: подпись в две строки не сдвигает поле — поля в ряду стоят на одной линии */
    .settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 20px; align-items: end; }
    .settings-grid .settings-field { max-width: none; margin-bottom: 0; }
    .settings-grid .settings-checkbox { margin-bottom: 0; }
    .settings-grid .span-2 { grid-column: 1 / -1; }
    .settings-grid .settings-field input[type="text"], .settings-grid .settings-field select { width: 100%; }
    .color-field { display: flex; gap: 8px; align-items: center; }
    .color-field input[type="text"] { flex: 1; min-width: 0; }
    .color-swatch {
      flex: none; width: 40px; height: 40px; padding: 3px; border: 1px solid var(--border);
      border-radius: 10px; background: var(--bg); cursor: pointer;
    }
    .color-swatch::-webkit-color-swatch-wrapper { padding: 0; }
    .color-swatch::-webkit-color-swatch { border: 0; border-radius: 7px; }
    .color-swatch::-moz-color-swatch { border: 0; border-radius: 7px; }
    .color-swatch.empty { background: repeating-linear-gradient(45deg, var(--border-soft) 0 6px, var(--bg) 6px 12px); }
    .color-swatch.empty::-webkit-color-swatch { opacity: 0; }
    .color-swatch.empty::-moz-color-swatch { opacity: 0; }

    .widget-settings { display: grid; grid-template-columns: minmax(0, 1fr) 270px; gap: 24px; align-items: start; }
    .widget-preview {
      position: sticky; top: 16px; background: var(--bg-page); border-radius: var(--radius-sm); padding: 14px;
      --wp-primary: var(--accent); --wp-primary-fg: var(--bg); --wp-launcher: var(--accent); --wp-launcher-fg: var(--bg);
    }
    .wp-caption { font-size: 12px; font-weight: 600; color: var(--text-muted); margin-bottom: 10px; }
    .wp-chat { background: var(--card); border-radius: 16px; box-shadow: var(--shadow); overflow: hidden; font-size: 12px; }
    .wp-header { padding: 10px 12px; border-bottom: 1px solid var(--border-soft); }
    .wp-title { display: flex; align-items: center; gap: 6px; font-weight: 700; font-size: 13px; }
    .wp-ai { font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-secondary); }
    .wp-status { display: flex; align-items: center; gap: 5px; color: var(--text-secondary); margin-top: 2px; font-size: 11px; }
    .wp-dot { width: 6px; height: 6px; border-radius: 50%; background: #22c55e; }
    .wp-body { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; background: var(--bg-page); }
    .wp-card { align-self: stretch; background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 7px 9px; font-weight: 700; }
    .wp-label { font-size: 9.5px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: var(--text-muted); margin-top: 2px; }
    .wp-msg { max-width: 88%; padding: 7px 9px; border-radius: 12px; line-height: 1.35; }
    .wp-bot { align-self: flex-start; background: var(--bg); border: 1px solid var(--border); }
    .wp-user { align-self: flex-end; background: var(--wp-primary); color: var(--wp-primary-fg); }
    .wp-op { align-self: flex-start; background: var(--accent-soft); }
    .wp-input { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-top: 1px solid var(--border-soft); }
    .wp-placeholder { flex: 1; min-width: 0; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 6px 8px; border: 1px solid var(--border); border-radius: 10px; }
    .wp-send { flex: none; background: var(--wp-primary); color: var(--wp-primary-fg); font-weight: 700; padding: 6px 9px; border-radius: 10px; }
    .wp-launcher-row { display: flex; justify-content: flex-end; margin-top: 12px; }
    .wp-launcher-row.left { justify-content: flex-start; }
    .wp-launcher {
      display: inline-flex; align-items: center; gap: 7px; height: 38px; padding: 0 14px 0 11px; border-radius: 999px;
      background: var(--wp-launcher); color: var(--wp-launcher-fg); font-weight: 600; font-size: 12.5px; box-shadow: var(--shadow);
    }
    .wp-launcher svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linejoin: round; }

    /* сохранение всегда на виду: раньше кнопка была одна, в самом низу длинной страницы */
    .settings-savebar {
      position: sticky; bottom: 16px; z-index: 5; display: flex; align-items: center; gap: 10px;
      margin-top: 4px; padding: 10px 10px 10px 20px; border-radius: 999px; border: 1px solid var(--border);
      background: var(--card); box-shadow: var(--shadow); transition: border-color .2s, box-shadow .2s;
    }
    .settings-savebar.dirty {
      border-color: var(--accent-soft);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-soft) 40%, transparent), var(--shadow);
    }
    .settings-savebar .settings-status { flex: 1; margin-left: 0; color: var(--text-muted); }
    .settings-savebar.dirty .settings-status { color: var(--text); }
    .settings-savebar .settings-status.success { color: var(--accent-deep); }
    .settings-savebar .settings-status.error { color: #c0392b; }
    .settings-savebar:not(.dirty) .settings-save-btn { background: var(--border-soft); color: var(--text-secondary); }
    .settings-discard-btn {
      font: inherit; font-size: 13.5px; font-weight: 600; padding: 10px 16px; border-radius: 999px;
      border: 1px solid var(--border); background: transparent; color: var(--text-secondary); cursor: pointer;
    }
    .settings-discard-btn:hover { background: var(--border-soft); color: var(--text); }
    @media (max-width: 1000px) {
      .widget-settings { grid-template-columns: 1fr; }
      .widget-preview { position: static; max-width: 340px; }
    }
    @media (max-width: 860px) {
      .settings-layout { grid-template-columns: 1fr; }
      .settings-nav {
        position: static; flex-direction: row; overflow-x: auto; gap: 4px;
        scrollbar-width: none; -webkit-overflow-scrolling: touch;
      }
      .settings-nav-btn { width: auto; flex: none; padding: 9px 12px; }
      .settings-grid { grid-template-columns: 1fr; }
      .settings-savebar { bottom: 8px; border-radius: 18px; padding: 8px 8px 8px 14px; }
      .settings-savebar .settings-status { font-size: 12.5px; }
    }

    /* ── чаты: список слева / переписка справа (master-detail) ── */
    .chats-layout { display: flex; gap: 16px; align-items: flex-start; }
    .chats-list-pane { flex: 0 0 320px; max-height: 74vh; overflow-y: auto; padding: 8px; }
    .chats-detail-pane { flex: 1 1 auto; min-width: 0; max-height: 74vh; overflow-y: auto; }

    .chat-row { padding: 11px 12px; border-radius: 12px; cursor: pointer; }
    .chat-row + .chat-row { margin-top: 2px; }
    .chat-row:hover { background: var(--border-soft); }
    .chat-row.active { background: color-mix(in srgb, var(--accent-soft) 32%, var(--card)); }
    .chat-row-top { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
    .chat-title { flex: 1; min-width: 0; font-size: 13.5px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .chat-row-meta { display: flex; align-items: center; gap: 6px; min-width: 0; }
    .chat-row-meta .chat-preview { flex: 1; min-width: 0; }
    .chat-id { font-size: 12px; font-weight: 700; color: var(--text-muted); font-variant-numeric: tabular-nums; }
    .chat-badge {
      font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
      background: var(--border-soft); color: var(--text-secondary);
    }
    .chat-badge.operator { background: #fdf0e5; color: #a65a1f; }
    .chat-badge.lead { background: #eaf4e0; color: var(--accent-deep); }
    .chat-time { font-size: 11.5px; color: var(--text-muted); margin-left: auto; }
    .chat-preview { font-size: 13px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .chat-detail-header {
      display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
      padding: 2px 6px 16px; margin-bottom: 14px; border-bottom: 1px solid var(--border-soft);
    }
    .t-msg { max-width: 78%; padding: 9px 13px; border-radius: 14px; margin-bottom: 8px; font-size: 13.5px; line-height: 1.4; }
    .t-msg-role { font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; opacity: .6; margin-bottom: 2px; }
    /* переносы как в виджете: иначе список цен «• … • …» слипается в одну строку */
    .t-msg-text { white-space: pre-wrap; overflow-wrap: anywhere; }
    .lead-chat-link {
      font: inherit; font-size: 12.5px; font-weight: 600; padding: 4px 10px; border-radius: 999px;
      border: 1px solid var(--border); background: transparent; color: var(--accent-deep); cursor: pointer; white-space: nowrap;
    }
    .lead-chat-link:hover { background: var(--border-soft); }
    .leads-date { white-space: nowrap; font-variant-numeric: tabular-nums; }
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
        <button type="button" class="filter-btn" data-scope="operator">С администратором</button>
        <button type="button" class="filter-btn" data-scope="lead">С лидом</button>
        __CHAT_EXPORT_HTML__
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
          Без персональных данных — имя и телефон видны в Telegram-теме диалога, тут только метаданные заявки. «Переписка» открывает сам диалог.
        </p>
        <div class="leads-filters">
          <select class="company-select" id="leadsReasonFilter">
            <option value="">Все типы</option>
            <option value="booking">Запись</option>
            <option value="price_question">Вопрос о цене</option>
            <option value="medical_risk">Консультация</option>
            <option value="commercial_interest">Интерес к услуге</option>
            <option value="unknown_service">Неизвестная услуга</option>
            <option value="booking_change">Перенос / отмена</option>
          </select>
        </div>
        <div id="leadsTableWrap"><div class="loading">Загрузка…</div></div>
      </div>
    </div>

    <div id="settingsContent" style="display:none">
      <div class="settings-layout">
        <nav class="settings-nav" id="settingsNav" aria-label="Разделы настроек">
          <button type="button" class="settings-nav-btn active" data-section="hours"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Часы работы</button>
          <button type="button" class="settings-nav-btn" data-section="contacts"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/></svg>Контакты</button>
          <button type="button" class="settings-nav-btn" data-section="widget"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/></svg>Виджет</button>
          <button type="button" class="settings-nav-btn" data-section="facts"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 6h10M10 12h10M10 18h10"/><path d="m3.5 6 1.5 1.5L7.5 5M3.5 12l1.5 1.5 2.5-2.5M3.5 18l1.5 1.5 2.5-2.5"/></svg>Факты о клинике</button>
          <button type="button" class="settings-nav-btn" data-section="doctors"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>Врачи</button>
          <button type="button" class="settings-nav-btn" data-section="buttons"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="7" width="18" height="10" rx="5"/></svg>Кнопки в ответах</button>
          <button type="button" class="settings-nav-btn" data-section="behavior"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></svg>Поведение</button>
          <button type="button" class="settings-nav-btn" data-section="texts"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Тексты бота</button>
        </nav>
        <div class="settings-main">
          <section class="card settings-section active" data-section="hours">
            <div class="settings-card-header">
              <h2>Часы работы</h2>
              <button type="button" class="settings-reset-btn" data-block="hours" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
            </div>
            <p class="card-hint">Отметьте "выходной" для дней, когда клиника не работает</p>
            <div id="hoursGrid"><div class="loading">Загрузка…</div></div>
          </section>
          <section class="card settings-section" data-section="contacts">
            <div class="settings-card-header">
              <h2>Контакты</h2>
              <button type="button" class="settings-reset-btn" data-block="contacts" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
            </div>
            <p class="card-hint">Бот называет их посетителям и ставит в кнопки «Позвонить» и «Написать в Telegram»</p>
            <div class="settings-grid">
              <div class="settings-field">
                <label for="settingsPhone">Телефон</label>
                <input type="text" id="settingsPhone" />
              </div>
              <div class="settings-field">
                <label for="settingsTelegram">Telegram</label>
                <input type="text" id="settingsTelegram" />
              </div>
              <div class="settings-field span-2">
                <label for="settingsAddress">Адрес</label>
                <input type="text" id="settingsAddress" />
              </div>
              <div class="settings-field">
                <label for="settingsWebsite">Сайт</label>
                <input type="text" id="settingsWebsite" />
              </div>
              <div class="settings-field">
                <label for="settingsPrivacyUrl">Ссылка на политику обработки данных</label>
                <input type="text" id="settingsPrivacyUrl" placeholder="https://…" />
              </div>
            </div>
          </section>
          <section class="card settings-section" data-section="widget">
            <div class="settings-card-header">
              <h2>Виджет</h2>
              <button type="button" class="settings-reset-btn" data-block="widget" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
            </div>
            <p class="card-hint">Как чат выглядит на сайте — справа видно сразу, до сохранения</p>
            <div class="widget-settings">
              <div class="settings-grid">
                <div class="settings-field">
                  <label for="settingsHeaderTitle">Заголовок чата</label>
                  <input type="text" id="settingsHeaderTitle" />
                </div>
                <div class="settings-field">
                  <label for="settingsPrimaryColor">Основной цвет</label>
                  <div class="color-field">
                    <input type="color" class="color-swatch" data-for="settingsPrimaryColor" aria-label="Выбрать основной цвет" />
                    <input type="text" id="settingsPrimaryColor" placeholder="#1F7A5C" />
                  </div>
                </div>
                <div class="settings-field">
                  <label for="settingsButtonColor">Цвет кнопки чата</label>
                  <div class="color-field">
                    <input type="color" class="color-swatch" data-for="settingsButtonColor" aria-label="Выбрать цвет кнопки чата" />
                    <input type="text" id="settingsButtonColor" placeholder="#1F7A5C" />
                  </div>
                </div>
                <div class="settings-field">
                  <label for="settingsLauncherLabel">Надпись на кнопке чата</label>
                  <input type="text" id="settingsLauncherLabel" maxlength="30" placeholder="Задать вопрос" />
                </div>
                <div class="settings-field">
                  <label for="settingsPosition">Расположение</label>
                  <select id="settingsPosition">
                    <option value="bottom-right">Справа снизу</option>
                    <option value="bottom-left">Слева снизу</option>
                  </select>
                </div>
                <div class="settings-field">
                  <label for="settingsStatusOnline">Статус под заголовком</label>
                  <input type="text" id="settingsStatusOnline" maxlength="30" placeholder="на связи" />
                </div>
                <div class="settings-field">
                  <label for="settingsInputPlaceholder">Подсказка в поле ввода</label>
                  <input type="text" id="settingsInputPlaceholder" maxlength="60" placeholder="Напишите вопрос…" />
                </div>
                <div class="settings-field">
                  <label for="settingsAssistantLabel">Подпись над ответами бота</label>
                  <input type="text" id="settingsAssistantLabel" maxlength="30" placeholder="Ассистент" />
                </div>
                <div class="settings-field">
                  <label for="settingsOperatorLabel">Подпись над ответами администратора</label>
                  <input type="text" id="settingsOperatorLabel" maxlength="30" placeholder="Специалист" />
                </div>
                <div class="settings-field">
                  <label for="settingsAvatarEmoji">Эмодзи в чате</label>
                  <input type="text" id="settingsAvatarEmoji" maxlength="4" />
                </div>
                <div class="settings-field">
                  <label for="settingsHighlightColor">Рамка у «Записаться на приём»</label>
                  <div class="color-field">
                    <input type="color" class="color-swatch" data-for="settingsHighlightColor" aria-label="Выбрать цвет рамки" />
                    <input type="text" id="settingsHighlightColor" placeholder="пусто — без рамки" />
                  </div>
                </div>
                <label class="settings-checkbox span-2"><input type="checkbox" id="settingsAiBadge" /> Показывать в шапке кнопку «с ИИ»</label>
                <label class="settings-checkbox span-2"><input type="checkbox" id="settingsQuickBooking" /> Быстрая запись: карточка «день + номер» и кнопка «Записаться» у поля ввода</label>
              </div>
              <aside class="widget-preview" aria-label="Как чат выглядит на сайте">
                <div class="wp-caption">Как увидят на сайте</div>
                <div class="wp-chat">
                  <div class="wp-header">
                    <div class="wp-title"><span id="wpTitle"></span><span class="wp-ai" id="wpAi">с ИИ</span></div>
                    <div class="wp-status"><span class="wp-dot"></span><span id="wpStatus"></span></div>
                  </div>
                  <div class="wp-body">
                    <div class="wp-card" id="wpBookingCard">Записаться на приём</div>
                    <div class="wp-label" id="wpBotLabel"></div>
                    <div class="wp-msg wp-bot">Здравствуйте! Подскажу цену и запишу на приём.</div>
                    <div class="wp-msg wp-user" id="wpUser">Сколько стоит чистка?</div>
                    <div class="wp-label" id="wpOpLabel"></div>
                    <div class="wp-msg wp-op">Добрый день! Чистка от 800 ₽, могу записать на завтра.</div>
                  </div>
                  <div class="wp-input"><span class="wp-placeholder" id="wpPlaceholder"></span><span class="wp-send" id="wpSend">Отправить</span></div>
                </div>
                <div class="wp-launcher-row" id="wpLauncherRow">
                  <span class="wp-launcher" id="wpLauncher"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/></svg><span id="wpLauncherLabel"></span></span>
                </div>
              </aside>
            </div>
          </section>
          <section class="card settings-section" data-section="facts">
            <div class="settings-card-header">
              <h2>Факты о клинике</h2>
              <button type="button" class="settings-reset-btn" data-block="facts" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
            </div>
            <p class="card-hint">Бот опирается на них, когда спрашивают про ОМС, ДМС, скорую и расписание</p>
            <div class="settings-grid">
              <label class="settings-checkbox"><input type="checkbox" id="factOms" /> Работаем по ОМС</label>
              <label class="settings-checkbox"><input type="checkbox" id="factDms" /> Работаем по ДМС</label>
              <label class="settings-checkbox"><input type="checkbox" id="factAmbulance" /> Скорая помощь привозит к нам</label>
              <label class="settings-checkbox"><input type="checkbox" id="factSells" /> Продаём товары/косметику</label>
              <label class="settings-checkbox"><input type="checkbox" id="factDoctorSchedule" /> Раскрываем расписание врачей</label>
            </div>
          </section>
          <section class="card settings-section" data-section="doctors">
            <div class="settings-card-header">
              <h2>Врачи</h2>
              <button type="button" class="settings-reset-btn" data-block="doctors" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
            </div>
            <p class="card-hint">Имя обязательно, специализация и расписание — по желанию</p>
            <div id="doctorsList"></div>
            <button type="button" class="doctor-add-btn" id="doctorAddBtn">+ Добавить врача</button>
          </section>
          <section class="card settings-section" data-section="buttons">
            <div class="settings-card-header">
              <h2>Кнопки в ответах бота</h2>
              <button type="button" class="settings-reset-btn" data-block="buttons" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
            </div>
            <p class="card-hint">Меняется только подпись — бот понимает нажатие как раньше. Пусто — как есть.</p>
            <div id="buttonLabelsList" class="settings-grid"></div>
          </section>
          <section class="card settings-section" data-section="behavior">
            <div class="settings-card-header">
              <h2>Поведение</h2>
              <button type="button" class="settings-reset-btn" data-block="behavior" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
            </div>
            <div class="settings-field">
              <label for="settingsWaitMinutes">Через сколько минут предлагать «Администратор пока не подключился — продолжим с ботом?»</label>
              <input type="number" id="settingsWaitMinutes" min="1" max="60" />
            </div>
            <div class="settings-field">
              <label class="settings-checkbox"><input type="checkbox" id="settingsConnectOnRequest" /> Сразу соединять с администратором по просьбе «позовите менеджера» (выключено — бот сначала предлагает помочь сам)</label>
            </div>
          </section>
          <section class="card settings-section" data-section="texts">
            <div class="settings-card-header">
              <h2>Тексты бота</h2>
              <button type="button" class="settings-reset-btn" data-block="texts" title="Отменить последнее сохранение этого блока">↺ Отменить</button>
            </div>
            <p class="card-hint">Что бот отвечает в разных ситуациях. Несколько вариантов — бот чередует их. Медицинские, кризисные и ценовые тексты здесь не меняются.</p>
            <div id="textsEditor"></div>
          </section>
        </div>
      </div>
      <div class="settings-savebar" id="settingsSavebar">
        <span class="settings-status" id="settingsStatus" aria-live="polite"></span>
        <button type="button" class="settings-discard-btn" id="settingsDiscardBtn" hidden>Сбросить</button>
        <button type="button" class="settings-save-btn" id="settingsSaveBtn">Сохранить</button>
      </div>
    </div>
  </main>

  <script>

    function escapeHtml(value) {
      return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function fmt(n) {
      return new Intl.NumberFormat("ru-RU").format(n ?? 0);
    }

    // сервер пишет время в UTC без пометки — показываем по часам клиники, иначе всё на 3 часа раньше
    let clinicTimezone = "Europe/Moscow";
    function clinicTime(iso) {
      if (!iso) return "";
      const raw = String(iso);
      const moment = new Date(/[zZ]$|[+-][0-9][0-9]:?[0-9][0-9]$/.test(raw) ? raw : raw + "Z");
      if (isNaN(moment.getTime())) return raw.replace("T", " ").slice(0, 16);
      try {
        const part = (opts, value) => new Intl.DateTimeFormat("ru-RU", { timeZone: clinicTimezone, ...opts }).format(value);
        const sameYear = part({ year: "numeric" }, moment) === part({ year: "numeric" }, new Date());
        const date = part(sameYear ? { day: "2-digit", month: "2-digit" } : { day: "2-digit", month: "2-digit", year: "2-digit" }, moment);
        return date + " " + part({ hour: "2-digit", minute: "2-digit" }, moment);
      } catch (_) {
        return raw.replace("T", " ").slice(0, 16);
      }
    }

    // «1760.9 мин» не читается — переводим в часы и дни
    function formatMinutes(minutes) {
      if (minutes == null) return "—";
      if (minutes < 1) return "меньше минуты";
      if (minutes < 60) return Math.round(minutes) + " мин";
      if (minutes < 60 * 24) {
        const hours = Math.floor(minutes / 60);
        const rest = Math.round(minutes - hours * 60);
        return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
      }
      return "≈ " + Math.round(minutes / 60 / 24) + " дн";
    }

    // подписи карточек: данные считаются за выбранный сверху период, а не «за всё время»
    function periodHint(data) {
      const label = data.range_label || "";
      return label === "всё время" ? "За всё время" : "За " + label;
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
      booking_change: "Перенос / отмена",
    };
    const LEAD_TRIGGER_LABELS = {
      ask_contact: "Оставил контакт", booking_request: "Запись",
      regulated_advice: "Мед. вопрос", operator_handoff: "Передано администратору",
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
          <td class="leads-date">${escapeHtml(clinicTime(lead.timestamp))}</td>
          <td>${escapeHtml(lead.service_name || "—")}</td>
          <td>${escapeHtml(LEAD_REASON_LABELS[lead.reason] || lead.reason)}</td>
          <td>${escapeHtml(LEAD_TRIGGER_LABELS[lead.lead_trigger] || lead.lead_trigger)}</td>
          <td>${lead.needs_operator ? "да" : "—"}</td>
          <td>${escapeHtml(lead.preferred_time || "—")}</td>
          <td class="page-path" title="${escapeHtml(lead.page || "")}">${escapeHtml(lead.page || "—")}</td>
          <td>${lead.session_id ? `<button type="button" class="lead-chat-link" data-session-id="${escapeHtml(lead.session_id)}">Переписка</button>` : "—"}</td>
        </tr>
      `).join("");
      return `
        <div class="table-scroll">
          <table>
            <thead>
              <tr><th>Дата</th><th>Услуга</th><th>Тип</th><th>Как пришёл</th><th>Нужен администратор</th><th>Когда удобно</th><th>Страница</th><th></th></tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
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
      // «Диалогов», «Лидов» и «Конверсия» с дельтами — из period_comparison: текущее и прошлое
      // окно посчитаны одним способом. Для своего периода его нет — честнее прочерк, чем сравнение
      // «N дней от сегодня» с произвольными датами.
      const pc = data.period_comparison;
      const tile = (label, value, caption) =>
        `<div class="tile"><div class="tile-label">${label}</div><div class="tile-value">${value}</div>${caption ? `<div class="tile-caption">${caption}</div>` : ""}</div>`;
      let tiles;
      if (pc) {
        const windowDays = pc.conversations_days != null ? pc.conversations_days : pc.days;
        const versus = `к прошлым ${windowDays} дн.`;
        const conversion = pc.conversations.current > 0
          ? Math.round((pc.leads.current / pc.conversations.current) * 1000) / 10
          : 0;
        const prevConversion = pc.conversations.previous > 0 ? (pc.leads.previous / pc.conversations.previous) * 100 : 0;
        tiles = [
          tile(`Диалогов за ${windowDays} дн.`, fmt(pc.conversations.current) + deltaBadge(pc.conversations.current, pc.conversations.previous), pc.conversations.previous ? versus : ""),
          tile(`Лидов за ${windowDays} дн.`, fmt(pc.leads.current) + deltaBadge(pc.leads.current, pc.leads.previous), `всего за всё время: ${fmt(totalLeads)}`),
          tile("Диалог → лид", conversion + "%" + deltaBadge(conversion, prevConversion), prevConversion ? versus : "сколько диалогов стали лидами"),
        ];
      } else {
        tiles = [
          tile("Диалогов", "—", "для своего периода не считаем"),
          tile("Лидов", "—", `всего за всё время: ${fmt(totalLeads)}`),
          tile("Диалог → лид", "—", ""),
        ];
      }
      tiles.push(tile("Ожидание администратора", formatMinutes(data.queue_wait.avg_wait_minutes), "в среднем от просьбы до «Взять»"));
      return `<div class="tiles">${tiles.join("")}</div>`;
    }

    function renderFunnel(funnel) {
      // Две пары стадий считаются по-разному: посетителей и открытия видит браузер (часть режут
      // блокировщики рекламы), переписки и лиды — сервер. Ширину полос считаем внутри каждой пары,
      // иначе «переписок» больше, чем «открытий», и полоса упирается в 100% — выглядит как ошибка.
      const stages = funnel.stages;
      const stageRow = (stage, top) => {
        const widthPct = Math.min(Math.max(Math.round((stage.count / top) * 100), stage.count > 0 ? 4 : 0), 100);
        const percentText = stage.percent_of_previous != null
          ? `<span class="funnel-percent">(${stage.percent_of_previous}%)</span>` : "";
        const loadsText = stage.page_loads != null
          ? `<span class="funnel-sub">загрузок страниц: ${fmt(stage.page_loads)}</span>` : "";
        return `
          <div class="funnel-stage">
            <div class="funnel-row">
              <span class="funnel-label">${escapeHtml(stage.label)} ${loadsText}</span>
              <span class="funnel-value">${fmt(stage.count)} ${percentText}</span>
            </div>
            <div class="funnel-track">
              <div class="funnel-fill ${stage.count === 0 ? "dim" : ""}" style="width:${widthPct}%">
                <span class="funnel-fill-label">${widthPct >= 12 ? widthPct + "%" : ""}</span>
              </div>
            </div>
          </div>
        `;
      };
      const tier = (items) => {
        const top = Math.max(...items.map((stage) => stage.count), 1);
        return items.map((stage) => stageRow(stage, top)).join("");
      };
      const teaser = funnel.teaser || {};
      const teaserLine = teaser.shown
        ? `<p class="card-hint funnel-teaser">Приглашение у кнопки чата: показали ${fmt(teaser.shown)} · «Узнать цену» ${fmt(teaser.price)} · «Записаться» ${fmt(teaser.booking)}</p>`
        : "";
      return `
        <div class="card">
          <h2>Воронка конверсии</h2>
          <p class="card-hint">За последние ${funnel.days} дн. · посетитель считается один раз за период · % — от предыдущей строки</p>
          ${tier(stages.slice(0, 2))}
          <p class="funnel-tier-note">Посетителей и открытия чата считает браузер — часть из них прячут блокировщики рекламы. Переписки и лиды ниже считает сервер, их может оказаться больше, чем открытий.</p>
          ${tier(stages.slice(2))}
          ${teaserLine}
        </div>
      `;
    }

    function renderWidgetPages(pages) {
      if (!pages || !pages.length) {
        return `<div class="card"><h2>Где открывают чат</h2><p class="card-hint">По страницам сайта</p><div class="empty-state">Данные появятся после обновления виджета на сайте</div></div>`;
      }
      const rows = pages.map((row) => `
        <tr>
          <td class="page-path" title="${escapeHtml(row.page)}">${escapeHtml(row.page)}</td>
          <td class="num">${fmt(row.loads)}</td>
          <td class="num">${fmt(row.opens)}</td>
          <td class="num">${row.open_rate != null ? row.open_rate + "%" : "—"}</td>
          <td class="num">${fmt(row.dialogs || 0)}</td>
        </tr>
      `).join("");
      return `
        <div class="card">
          <h2>Где открывают чат</h2>
          <p class="card-hint">По страницам сайта · топ-20 по загрузкам · доля — открытий от загрузок · диалоги — где человек написал первым</p>
          <div class="table-scroll">
            <table class="pages-table">
              <thead><tr><th>Страница</th><th class="num">Загрузок</th><th class="num">Открытий</th><th class="num">Доля</th><th class="num">Диалогов</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
        </div>
      `;
    }

    const REASON_LABELS = {
      booking: "Запись",
      price_question: "Вопрос о цене",
      medical_risk: "Консультация",
      commercial_interest: "Интерес к услуге",
      unknown_service: "Неизвестная услуга",
      booking_change: "Перенос / отмена",
    };

    function renderLeadReasons(items, hint) {
      if (!items.length) {
        return `<div class="card"><h2>Лиды по типу</h2><p class="card-hint">${hint}</p><div class="empty-state">Пока нет лидов</div></div>`;
      }
      const total = items.reduce((sum, i) => sum + i.count, 0);
      const max = Math.max(...items.map((i) => i.count));
      const rows = items.map((item) => `
        <div class="service-row">
          <div class="service-name">${escapeHtml(REASON_LABELS[item.reason] || item.reason)}</div>
          <div class="service-bar-track">
            <div class="service-bar-fill" style="width:${Math.round((item.count / max) * 100)}%"></div>
          </div>
          <div class="service-count">${fmt(item.count)}</div>
          <div class="service-share">${Math.round((item.count / total) * 100)}%</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Лиды по типу</h2>
          <p class="card-hint">${hint}</p>
          ${rows}
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
          <p class="card-hint">Последние ${months.length} месяцев · текущий выделен</p>
          <div class="month-chart labeled">${bars}</div>
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

    function renderOperators(operators, hint) {
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
              сам, без администратора
            </span>
          </div>
        `
        : "";

      if (!humanEntries.length) {
        return `
          <div class="card">
            <h2>Администраторы</h2>
            <p class="card-hint">${hint}</p>
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
          <td class="num">${formatMinutes(stats.avg_dialog_minutes)}</td>
        </tr>
      `).join("");
      return `
        <div class="card">
          <h2>Администраторы</h2>
          <p class="card-hint">${hint} · длительность — от «Взять» до закрытия диалога</p>
          ${botBanner}
          <div class="table-scroll">
            <table class="operators-table">
              <thead><tr><th>Администратор</th><th class="num">Взято</th><th class="num">Закрыто</th><th class="num">Лидов</th><th class="num" title="Средняя длительность диалога">Ср. длит.</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
        </div>
      `;
    }

    function renderTopServices(services, hint) {
      if (!services.length) {
        return `<div class="card"><h2>Топ услуг</h2><p class="card-hint">${hint} · по числу лидов</p><div class="empty-state">Пока нет лидов с привязкой к услуге</div></div>`;
      }
      const max = Math.max(...services.map((s) => s.count));
      const rows = services.map((s) => `
        <div class="service-row">
          <div class="service-name" title="${escapeHtml(s.service_name)}">${escapeHtml(s.service_name)}</div>
          <div class="service-bar-track">
            <div class="service-bar-fill" style="width:${Math.round((s.count / max) * 100)}%"></div>
          </div>
          <div class="service-count">${fmt(s.count)}</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Топ услуг</h2>
          <p class="card-hint">${hint} · по числу лидов</p>
          ${rows}
        </div>
      `;
    }

    const INTENT_LABELS = {
      ok: "Ответил по базе знаний", price_question: "Цена услуги", price_question_no_service: "Цены в целом",
      list_services: "Какие есть услуги", small_talk: "Приветствие, благодарность", off_topic: "Не по теме клиники",
      off_topic_body_redirect: "Не по теме, про здоровье", operator_requested: "Позвали администратора",
      booking_request: "Запись", booking_change: "Перенос / отмена", contact_provided: "Оставили телефон", lead_request: "Заявка",
      cosmetic_concern: "Косметическая проблема", medical_advice: "Медицинский вопрос",
      regulated_advice: "Медицинский вопрос — к врачу", unknown_service: "Услуги нет в базе",
      similar_services_found: "Предложил похожие услуги", contact_link: "Спросили контакты",
      location_mismatch: "Другой город", unsupported_city: "Город не обслуживаем",
      service_mention: "Назвали услугу", service_explanation: "Что это за услуга",
      duration_question: "Сколько длится", faq_question: "Частый вопрос", quick_faq: "Частый вопрос (кнопка)",
      objection_handled: "Сомнение, возражение", objection_backoff: "Сомнение, повторно",
      // нейтрально намеренно: рядом с «Цена» и «Запись» прямое название читалось бы как ещё одна метрика
      complaint: "Жалоба", self_harm_crisis: "Особое внимание", out_of_scope: "Не по профилю клиники",
      unknown: "Не распознано",
    };

    function renderIntentBreakdown(items, hint) {
      if (!items.length) {
        return `<div class="card"><h2>О чём спрашивают</h2><p class="card-hint">${hint}</p><div class="empty-state">Пока нет данных</div></div>`;
      }
      const max = Math.max(...items.map((i) => i.count));
      const rows = items.map((item) => `
        <div class="service-row">
          <div class="service-name" title="${escapeHtml(INTENT_LABELS[item.reason] || item.reason)}">${escapeHtml(INTENT_LABELS[item.reason] || item.reason)}</div>
          <div class="service-bar-track">
            <div class="service-bar-fill" style="width:${Math.round((item.count / max) * 100)}%"></div>
          </div>
          <div class="service-count">${fmt(item.count)}</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>О чём спрашивают</h2>
          <p class="card-hint">${hint} · как бот понял сообщение</p>
          ${rows}
        </div>
      `;
    }

    const OBJECTION_TOPIC_LABELS = {
      price: "Цена", hesitation: "Сомнение / надо подумать", competitor: "Сравнение с конкурентом",
      guarantee: "Вопрос про гарантию", pain_fear: "Страх боли / побочек", unknown: "Неизвестно",
    };

    function renderObjectionBreakdown(items, hint) {
      // пустая карточка только занимает место — появится, когда будут данные
      if (!items.length) return "";
      const max = Math.max(...items.map((i) => i.count));
      const rows = items.map((item) => `
        <div class="service-row">
          <div class="service-name" title="${escapeHtml(item.topic)}">${escapeHtml(OBJECTION_TOPIC_LABELS[item.topic] || item.topic)}</div>
          <div class="service-bar-track">
            <div class="service-bar-fill" style="width:${Math.round((item.count / max) * 100)}%"></div>
          </div>
          <div class="service-count">${fmt(item.count)}</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Сомнения и возражения</h2>
          <p class="card-hint">${hint} · в чём чаще сомневаются</p>
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

    function renderActivityByHour(hours, timezone) {
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
          <p class="card-hint">Сообщений по часу суток, ${timezone === "Europe/Moscow" ? "по Москве" : "по времени клиники"}</p>
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
          <div class="month-chart labeled">${bars}</div>
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
        return `<div class="card"><h2>Последние непонятые вопросы</h2><p class="card-hint">Что добавить в базу знаний</p><div class="empty-state">Ничего нет — база знаний покрывает все вопросы</div></div>`;
      }
      const rows = items.slice(0, 10).map((item) => `
        <div class="feed-item">
          <div class="feed-text">${escapeHtml(item.message || "—")}</div>
          <div class="feed-meta">${escapeHtml(clinicTime(item.timestamp))}</div>
        </div>
      `).join("");
      return `
        <div class="card">
          <h2>Последние непонятые вопросы</h2>
          <p class="card-hint">Последние ${Math.min(items.length, 10)} — что добавить в базу знаний</p>
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
      if (item.operator_requested) badges.push('<span class="chat-badge operator">Администратор</span>');
      if (item.lead_requested) badges.push('<span class="chat-badge lead">Лид</span>');
      return badges.join("");
    }

    const ROLE_LABELS = { user: "Посетитель", assistant: "Бот", operator: "Администратор", system: "Событие" };

    function renderTranscriptMessages(messages) {
      if (!messages.length) return '<div class="empty-state">Сообщений нет</div>';
      return messages.map((m) => `
        <div class="t-msg ${escapeHtml(m.role)}">
          <div class="t-msg-role">${escapeHtml(ROLE_LABELS[m.role] || m.role)}</div>
          <div class="t-msg-text">${escapeHtml(m.text)}</div>
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

    // Выгрузка диалогов в JSON (2026-09-22) — только на /backstage. Отмеченные живут, пока
    // список не перезагрузили (смена фильтра/компании), телефоны маскирует сервер.
    const CHAT_EXPORT_ENABLED = __CHAT_EXPORT_ENABLED__;
    const exportSelection = new Set();

    function updateExportControls() {
      const button = document.getElementById("exportSelectedBtn");
      if (!button) return;
      button.disabled = exportSelection.size === 0;
      button.textContent = exportSelection.size ? `Скачать выбранные (${exportSelection.size})` : "Скачать выбранные";
    }

    async function exportChats(onlySelected) {
      const status = document.getElementById("exportStatus");
      const body = {
        company_id: document.getElementById("companySelect").value || null,
        scope: currentChatScope,
        session_ids: onlySelected ? Array.from(exportSelection) : [],
      };
      status.textContent = "Готовлю файл…";
      try {
        const res = await fetch("/api/analytics/chats/export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const stamp = (data.exported_at || "").slice(0, 16).replace("T", "_").replace(":", "");
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `chats_${body.scope}_${stamp || "export"}.json`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        status.textContent = `Скачано диалогов: ${data.count}, телефоны скрыты`;
      } catch (error) {
        status.textContent = `Не удалось выгрузить: ${error.message}`;
      }
    }

    function renderChatRow(item) {
      // чат узнают по первой фразе посетителя; последнее сообщение — второй строкой, если оно другое
      const title = item.first_message || item.last_message || "Без сообщений";
      const preview = item.last_message && item.last_message !== title ? item.last_message : "";
      return `
        <div class="chat-row" data-session-id="${escapeHtml(item.session_id)}">
          <div class="chat-row-top">
            ${CHAT_EXPORT_ENABLED ? `<input type="checkbox" class="chat-check" aria-label="Отметить для выгрузки" data-session-id="${escapeHtml(item.session_id)}"${exportSelection.has(item.session_id) ? " checked" : ""}>` : ""}
            <span class="chat-title">${escapeHtml(truncate(title, 80))}</span>
            <span class="chat-time">${escapeHtml(clinicTime(item.updated_at))}</span>
          </div>
          <div class="chat-row-meta">
            ${chatBadges(item)}
            <span class="chat-preview">${escapeHtml(preview)}</span>
          </div>
        </div>
      `;
    }

    function chatDetailHeader(sessionId) {
      const item = chatRowsById[sessionId];
      if (!item) return "";
      return `
        <div class="chat-detail-header">
          ${chatBadges(item)}
          <span class="chat-time">${escapeHtml(clinicTime(item.updated_at))}</span>
          <span class="chat-id" title="Код диалога">#${escapeHtml(sessionId.slice(0, 8))}</span>
        </div>
      `;
    }

    async function selectChat(sessionId, byClick) {
      activeChatSessionId = sessionId;
      document.querySelectorAll(".chat-row").forEach((row) => {
        row.classList.toggle("active", row.dataset.sessionId === sessionId);
      });
      const detail = document.getElementById("chatDetail");
      const header = chatDetailHeader(sessionId);
      // на телефоне переписка стоит под списком — после нажатия её иначе не видно; крутим, когда
      // она уже нарисована, иначе страница ещё короткая и до неё не докручивается
      const revealDetail = () => {
        if (byClick && window.matchMedia("(max-width: 860px)").matches) detail.scrollIntoView({ behavior: "smooth", block: "start" });
      };
      detail.innerHTML = header + '<div class="loading">Загрузка…</div>';
      try {
        const data = chatTranscriptCache[sessionId] || await fetchChatDetail(sessionId);
        chatTranscriptCache[sessionId] = data;
        // пока грузилось — могли кликнуть на другой чат, не перетираем чужой выбор
        if (activeChatSessionId !== sessionId) return;
        detail.innerHTML = header + renderTranscriptMessages(data.messages);
        revealDetail();
      } catch (error) {
        if (activeChatSessionId !== sessionId) return;
        detail.innerHTML = header + `<div class="error">Не удалось загрузить: ${escapeHtml(error.message)}</div>`;
        revealDetail();
      }
    }

    // из «Лидов»: открыть именно этот диалог, а не первый в списке после загрузки
    let pendingChatSessionId = null;

    async function loadChats() {
      const preferredSessionId = pendingChatSessionId;
      pendingChatSessionId = null;
      const list = document.getElementById("chatsList");
      const companyId = document.getElementById("companySelect").value;
      list.innerHTML = '<div class="loading">Загрузка…</div>';
      activeChatSessionId = null;
      exportSelection.clear();
      updateExportControls();
      document.getElementById("chatDetail").innerHTML = '<div class="empty-state">Выберите диалог слева</div>';
      try {
        const data = await fetchChats(companyId, currentChatScope);
        chatRowsById = {};
        data.conversations.forEach((item) => { chatRowsById[item.session_id] = item; });
        list.innerHTML = data.conversations.length
          ? data.conversations.map(renderChatRow).join("")
          : '<div class="empty-state">Диалогов не найдено</div>';
        if (preferredSessionId) selectChat(preferredSessionId);
        else if (data.conversations.length) selectChat(data.conversations[0].session_id);
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
        document.getElementById("settingsPrimaryColor").value = data.widget.primary_color || "";
        document.getElementById("settingsButtonColor").value = data.widget.button_color || "";
        document.getElementById("settingsPosition").value = data.widget.position || "bottom-right";
        document.getElementById("settingsAvatarEmoji").value = data.widget.avatar_emoji || "";
        document.getElementById("settingsAssistantLabel").value = data.widget.assistant_label || "";
        document.getElementById("settingsHighlightColor").value = data.widget.booking_highlight_color || "";
        document.getElementById("settingsAiBadge").checked = data.widget.ai_badge === "show";
        document.getElementById("settingsQuickBooking").checked = data.widget.quick_booking === "on";
        document.getElementById("factOms").checked = Boolean(data.facts.oms);
        document.getElementById("factDms").checked = Boolean(data.facts.dms);
        document.getElementById("factAmbulance").checked = Boolean(data.facts.ambulance_brings);
        document.getElementById("factSells").checked = Boolean(data.facts.sells_products);
        document.getElementById("factDoctorSchedule").checked = Boolean(data.facts.discloses_doctor_schedule);
        document.getElementById("doctorsList").innerHTML = renderDoctorsList(data.doctors);
        document.getElementById("settingsPrivacyUrl").value = data.privacy_policy_url || "";
        document.getElementById("settingsLauncherLabel").value = data.widget.launcher_label || "";
        document.getElementById("settingsStatusOnline").value = data.widget.status_online || "";
        document.getElementById("settingsInputPlaceholder").value = data.widget.input_placeholder || "";
        document.getElementById("settingsOperatorLabel").value = data.widget.operator_label || "";
        document.getElementById("settingsWaitMinutes").value = data.operator_wait_offer_minutes || 5;
        document.getElementById("settingsConnectOnRequest").checked = data.operator_connect_on_request !== false;
        document.getElementById("buttonLabelsList").innerHTML = renderButtonLabels(data.button_labels || []);
        document.getElementById("textsEditor").innerHTML = renderTextsEditor(data.texts || []);
        syncColorSwatches();
        updateWidgetPreview();
        setSettingsDirty(false);
        scrollActiveSettingsNav();
        status.textContent = "Изменений нет";
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
          position: document.getElementById("settingsPosition").value,
          avatar_emoji: document.getElementById("settingsAvatarEmoji").value,
          assistant_label: document.getElementById("settingsAssistantLabel").value.trim() || "Ассистент",
          ai_badge: document.getElementById("settingsAiBadge").checked ? "show" : "",
          quick_booking: document.getElementById("settingsQuickBooking").checked ? "on" : "",
          booking_highlight_color: document.getElementById("settingsHighlightColor").value.trim(),
          launcher_label: document.getElementById("settingsLauncherLabel").value.trim() || "Задать вопрос",
          status_online: document.getElementById("settingsStatusOnline").value.trim() || "на связи",
          input_placeholder: document.getElementById("settingsInputPlaceholder").value.trim() || "Напишите вопрос…",
          operator_label: document.getElementById("settingsOperatorLabel").value.trim() || "Специалист",
        },
        privacy_policy_url: document.getElementById("settingsPrivacyUrl").value.trim(),
        button_labels: collectButtonLabels(),
        operator_wait_offer_minutes: parseInt(document.getElementById("settingsWaitMinutes").value, 10) || 5,
        operator_connect_on_request: document.getElementById("settingsConnectOnRequest").checked,
        texts: collectTexts(),
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
        setSettingsDirty(false);
        status.textContent = "Сохранено";
        status.className = "settings-status success";
      } catch (error) {
        status.textContent = `Ошибка: ${escapeHtml(error.message)}`;
        status.className = "settings-status error";
      } finally {
        button.disabled = false;
      }
    }

    // «Тексты бота»: по умолчанию — то, что задано в данных клиента или в коде; хранится только отличие
    const textDefaults = {};

    function renderTextsEditor(groups) {
      return groups.map((group) => {
        const changed = group.items.filter((item) => item.customized).length;
        const items = group.items.map((item) => {
          textDefaults[item.key] = item.default;
          const variants = (item.value.length ? item.value : [""]).map(
            (text) => `<textarea class="text-variant" rows="2">${escapeHtml(text)}</textarea>`
          ).join("");
          const hint = item.placeholders.length
            ? `<span class="text-ph">Можно подставить: ${item.placeholders.map((name) => "{" + escapeHtml(name) + "}").join(", ")}</span>` : "";
          return `
            <div class="text-item" data-key="${escapeHtml(item.key)}">
              <div class="text-item-head">
                <span class="text-item-label">${escapeHtml(item.label)}</span>
                ${item.customized ? '<span class="text-badge">изменён</span>' : ""}
              </div>
              <div class="text-item-where">${escapeHtml(item.where)}</div>
              <div class="text-variants">${variants}</div>
              <div class="text-item-actions">
                ${hint}
                <button type="button" class="text-add-variant"${item.value.length >= 3 ? " hidden" : ""}>+ вариант</button>
                <button type="button" class="text-reset">Вернуть по умолчанию</button>
              </div>
            </div>
          `;
        }).join("");
        return `
          <details class="text-group">
            <summary>${escapeHtml(group.title)}<span class="text-group-count">${group.items.length}${changed ? " · изменено: " + changed : ""}</span></summary>
            ${items}
          </details>
        `;
      }).join("");
    }

    function collectTexts() {
      const texts = {};
      document.querySelectorAll("#textsEditor .text-item").forEach((item) => {
        texts[item.dataset.key] = Array.from(item.querySelectorAll(".text-variant"))
          .map((area) => area.value.trim())
          .filter(Boolean);
      });
      return texts;
    }

    function renderButtonLabels(buttons) {
      return buttons.map((button) => `
        <div class="settings-field">
          <label>«${escapeHtml(button.original)}»</label>
          <input type="text" class="button-label-input" data-original="${escapeHtml(button.original)}" maxlength="30" placeholder="${escapeHtml(button.original)}" value="${escapeHtml(button.label)}" />
        </div>
      `).join("");
    }

    function collectButtonLabels() {
      const labels = {};
      document.querySelectorAll("#buttonLabelsList .button-label-input").forEach((input) => {
        labels[input.dataset.original] = input.value.trim();
      });
      return labels;
    }

    async function resetSettingsBlock(block, button) {
      // Один уровень отмены на блок ("Часы работы", "Контакты" и т.д.) — откатывает
      // ИМЕННО этот блок к состоянию перед последним сохранением (бэкап пишет
      // save_overrides_atomic на каждое сохранение), остальные блоки не трогает, даже
      // если их сохраняли позже. Обсуждено с пользователем 2026-08-29 — полной истории
      // версий сознательно нет, только один шаг назад.
      if (settingsDirty && !window.confirm("Несохранённые изменения в других разделах пропадут. Продолжить?")) return;
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

    // ── «Настройки»: разделы, несохранённые правки, превью виджета ──
    const SETTINGS_SECTION_KEY = "settings-section";
    const HEX_COLOR = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;
    let settingsDirty = false;

    function showSettingsSection(name) {
      const target = document.querySelector(`.settings-section[data-section="${name}"]`) ? name : "hours";
      document.querySelectorAll(".settings-nav-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.section === target));
      document.querySelectorAll(".settings-section").forEach((section) => section.classList.toggle("active", section.dataset.section === target));
      try { localStorage.setItem(SETTINGS_SECTION_KEY, target); } catch (_) {}
      scrollActiveSettingsNav();
    }

    // на телефоне меню — лента вбок: открытый раздел не должен прятаться за краем
    function scrollActiveSettingsNav() {
      const nav = document.getElementById("settingsNav");
      const active = nav.querySelector(".settings-nav-btn.active");
      if (active && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = active.offsetLeft - 8;
    }

    function setSettingsDirty(dirty, section) {
      settingsDirty = dirty;
      document.getElementById("settingsSavebar").classList.toggle("dirty", dirty);
      document.getElementById("settingsDiscardBtn").hidden = !dirty;
      if (!dirty) {
        document.querySelectorAll(".settings-nav-btn.changed").forEach((btn) => btn.classList.remove("changed"));
        return;
      }
      const navBtn = section && document.querySelector(`.settings-nav-btn[data-section="${section}"]`);
      if (navBtn) navBtn.classList.add("changed");
      const status = document.getElementById("settingsStatus");
      status.textContent = "Есть несохранённые изменения";
      status.className = "settings-status";
    }

    function markSettingsChanged(target) {
      const section = target.closest(".settings-section");
      setSettingsDirty(true, section ? section.dataset.section : null);
      if (section && section.dataset.section === "widget") updateWidgetPreview();
    }

    function fullHex(value) {
      return value.length === 4 ? "#" + value[1] + value[1] + value[2] + value[2] + value[3] + value[3] : value;
    }

    function syncColorSwatches() {
      document.querySelectorAll(".color-swatch").forEach((swatch) => {
        const value = document.getElementById(swatch.dataset.for).value.trim();
        const valid = HEX_COLOR.test(value);
        swatch.classList.toggle("empty", !valid);
        if (valid) swatch.value = fullHex(value).toLowerCase();
      });
    }

    // значок на кнопке чата — как в widget.js: тем из двух цветов, что контрастнее на её фоне
    function readableOn(hex) {
      const n = parseInt(fullHex(hex).slice(1), 16);
      const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      const lum = 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
      return (lum + 0.05) / 0.054 > 1.05 / (lum + 0.05) ? "var(--text)" : "var(--bg)";
    }

    function updateWidgetPreview() {
      const value = (id) => document.getElementById(id).value.trim();
      const primary = HEX_COLOR.test(value("settingsPrimaryColor")) ? value("settingsPrimaryColor") : "#080E0D";
      const launcher = HEX_COLOR.test(value("settingsButtonColor")) ? value("settingsButtonColor") : primary;
      const highlight = value("settingsHighlightColor");
      const preview = document.querySelector(".widget-preview");
      // текст на основном цвете в виджете всегда светлый — превью не подправляет, а показывает как есть
      preview.style.setProperty("--wp-primary", primary);
      preview.style.setProperty("--wp-launcher", launcher);
      preview.style.setProperty("--wp-launcher-fg", readableOn(launcher));
      document.getElementById("wpTitle").textContent = value("settingsHeaderTitle") || "Консультант";
      document.getElementById("wpAi").hidden = !document.getElementById("settingsAiBadge").checked;
      document.getElementById("wpStatus").textContent = value("settingsStatusOnline") || "на связи";
      document.getElementById("wpBotLabel").textContent = [value("settingsAvatarEmoji"), value("settingsAssistantLabel") || "Ассистент"].filter(Boolean).join(" ");
      document.getElementById("wpOpLabel").textContent = value("settingsOperatorLabel") || "Специалист";
      document.getElementById("wpPlaceholder").textContent = value("settingsInputPlaceholder") || "Напишите вопрос…";
      document.getElementById("wpLauncherLabel").textContent = value("settingsLauncherLabel") || "Задать вопрос";
      document.getElementById("wpLauncherRow").classList.toggle("left", value("settingsPosition") === "bottom-left");
      const card = document.getElementById("wpBookingCard");
      const framed = HEX_COLOR.test(highlight);
      card.style.borderColor = framed ? highlight : "";
      card.style.boxShadow = framed ? `0 0 0 1px ${highlight}` : "";
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
        if (data.timezone) clinicTimezone = data.timezone;
        const hint = periodHint(data);
        content.innerHTML = `
          ${renderTiles(data)}
          ${renderFunnel(data.funnel)}
          ${renderWidgetPages(data.funnel.pages)}
          <div class="grid-2 even">
            ${renderMonthChart(data.leads_by_month)}
            ${renderOperators(data.operators, hint)}
          </div>
          <div class="grid-2">
            ${renderTopServices(data.top_services, hint)}
            ${renderLeadReasons(data.leads_by_reason, hint)}
          </div>
          <div class="grid-2">
            ${renderActivityByHour(data.activity_by_hour, data.timezone)}
            ${renderActivityByWeekday(data.activity_by_weekday)}
          </div>
          ${renderIntentBreakdown(data.intent_breakdown, hint)}
          ${renderObjectionBreakdown(data.objection_breakdown, hint)}
          <div class="grid-2">
            ${renderTopQuestions("Чаще всего не понял", hint + " · одинаковые сообщения считаются вместе", data.top_unanswered_questions)}
            ${renderTopQuestions("Чаще всего спрашивают", hint + " · одинаковые сообщения считаются вместе", data.top_answered_questions)}
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
    document.getElementById("leadsTableWrap").addEventListener("click", (event) => {
      const link = event.target.closest(".lead-chat-link");
      if (!link) return;
      pendingChatSessionId = link.dataset.sessionId;
      switchTab("chats");
    });

    document.getElementById("tabDashboardBtn").addEventListener("click", () => switchTab("dashboard"));
    document.getElementById("tabChatsBtn").addEventListener("click", () => switchTab("chats"));
    document.getElementById("tabLeadsBtn").addEventListener("click", () => switchTab("leads"));
    document.getElementById("tabSettingsBtn").addEventListener("click", () => switchTab("settings"));
    document.getElementById("settingsSaveBtn").addEventListener("click", saveSettings);
    document.querySelectorAll(".settings-reset-btn").forEach((btn) => {
      btn.addEventListener("click", () => resetSettingsBlock(btn.dataset.block, btn));
    });
    document.getElementById("textsEditor").addEventListener("click", (event) => {
      const item = event.target.closest(".text-item");
      if (!item) return;
      const variants = item.querySelector(".text-variants");
      if (event.target.closest(".text-add-variant")) {
        variants.insertAdjacentHTML("beforeend", '<textarea class="text-variant" rows="2"></textarea>');
        if (variants.children.length >= 3) event.target.closest(".text-add-variant").hidden = true;
      } else if (event.target.closest(".text-reset")) {
        const defaults = textDefaults[item.dataset.key] || [""];
        variants.innerHTML = defaults.map((text) => `<textarea class="text-variant" rows="2">${escapeHtml(text)}</textarea>`).join("");
        item.querySelector(".text-add-variant").hidden = defaults.length >= 3;
      }
    });
    document.getElementById("settingsNav").addEventListener("click", (event) => {
      const btn = event.target.closest(".settings-nav-btn");
      if (btn) showSettingsSection(btn.dataset.section);
    });
    const settingsContent = document.getElementById("settingsContent");
    settingsContent.addEventListener("input", (event) => {
      if (!event.target.closest(".settings-section")) return;
      if (event.target.classList.contains("color-swatch")) {
        document.getElementById(event.target.dataset.for).value = event.target.value.toUpperCase();
      }
      if (event.target.closest(".color-field")) syncColorSwatches();
      markSettingsChanged(event.target);
    });
    settingsContent.addEventListener("change", (event) => {
      if (event.target.matches('input[type="checkbox"], select')) markSettingsChanged(event.target);
    });
    // capture: «удалить врача» убирает строку раньше, чем клик всплывёт, — раздел ищем до этого
    settingsContent.addEventListener("click", (event) => {
      if (event.target.closest(".doctor-remove-btn, #doctorAddBtn, .text-add-variant, .text-reset")) markSettingsChanged(event.target);
    }, true);
    document.getElementById("settingsDiscardBtn").addEventListener("click", async () => {
      await loadSettings();
      document.getElementById("settingsStatus").textContent = "Изменения сброшены";
    });
    window.addEventListener("beforeunload", (event) => {
      if (!settingsDirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
    let savedSettingsSection = null;
    try { savedSettingsSection = localStorage.getItem(SETTINGS_SECTION_KEY); } catch (_) {}
    showSettingsSection(savedSettingsSection || "hours");
    document.querySelectorAll(".filter-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        currentChatScope = btn.dataset.scope;
        loadChats();
      });
    });
    document.getElementById("chatsList").addEventListener("click", (event) => {
      // клик по галочке только отмечает чат для выгрузки, а не открывает его
      if (event.target.closest(".chat-check")) return;
      const row = event.target.closest(".chat-row");
      if (row) selectChat(row.dataset.sessionId, true);
    });
    document.getElementById("chatsList").addEventListener("change", (event) => {
      const box = event.target.closest(".chat-check");
      if (!box) return;
      if (box.checked) exportSelection.add(box.dataset.sessionId);
      else exportSelection.delete(box.dataset.sessionId);
      updateExportControls();
    });
    if (CHAT_EXPORT_ENABLED) {
      document.getElementById("exportSelectedBtn").addEventListener("click", () => exportChats(true));
      document.getElementById("exportAllBtn").addEventListener("click", () => exportChats(false));
    }

    load();
  </script>
</body>
</html>
"""
    # выгрузка диалогов — только внутренняя страница (/backstage, с переключателем компаний);
    # на клиентской /analytics ни кнопок, ни галочек
    chat_export_html = (
        '<div class="chat-export">'
        '<button type="button" class="export-btn" id="exportSelectedBtn" disabled>Скачать выбранные</button>'
        '<button type="button" class="export-btn" id="exportAllBtn">Скачать все по фильтру (до 500)</button>'
        '<span class="export-status" id="exportStatus" aria-live="polite"></span>'
        "</div>"
        if show_company_selector
        else ""
    )
    return (
        html.replace("__COMPANY_SELECT_HTML__", company_select_html)
        .replace("__CHAT_EXPORT_HTML__", chat_export_html)
        .replace("__CHAT_EXPORT_ENABLED__", "true" if show_company_selector else "false")
    )

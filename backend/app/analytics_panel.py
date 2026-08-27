"""Веб-страница аналитики (2026-08-27) — сводка, операторы, лиды по месяцам, топ услуг.

Стиль чата (виджета), не оператор-панели: тёплый светлый фон, скруглённые карточки, мягкие
тени — тот же язык, что и в widget.js (--bg/--text/--accent-soft), но без копии шрифта
(широкий встроенный woff2 не тащим ради внутреннего инструмента). Цвета данных в графиках —
из проверенной валидатором дефолтной палитры дата-виз скилла (CVD-safe, не "на глаз")."""


def render_analytics_panel() -> str:
    return """
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
    @media (max-width: 860px) { .grid-2 { grid-template-columns: 1fr; } }

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
    .feed-text { font-size: 13.5px; font-weight: 600; }
    .feed-meta { font-size: 11.5px; color: var(--text-muted); margin-top: 3px; }

    .loading, .error { text-align: center; padding: 60px 20px; color: var(--text-muted); font-size: 14px; }
    .error { color: #b3261e; }
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
        </select>
        <select class="company-select" id="companySelect">
          <option value="rosh_import_demo">rosh_import_demo</option>
          <option value="">Все компании</option>
        </select>
        <form method="post" action="/logout">
          <button type="submit" class="logout-btn">Выйти</button>
        </form>
      </div>
    </header>

    <div id="content">
      <div class="loading">Загрузка…</div>
    </div>
  </main>

  <script>
    const token = new URLSearchParams(window.location.search).get("token") || "";
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

    async function fetchDashboard(companyId, days) {
      const params = new URLSearchParams();
      if (companyId) params.set("company_id", companyId);
      params.set("days", days);
      params.set("token", token);
      const res = await fetch(`/api/analytics/dashboard?${params.toString()}`, {
        headers: { "x-operator-token": token },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
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
      // "Всего сессий"/"Конверсия" раньше брались из session_store.list_all() — это ЖИВЫЕ
      // сессии в памяти (TTL 24-48ч), а не история. С лидами за всё время в числителе это
      // рисовало абсурдные проценты (3300%+). Берём знаменатель из той же воронки (стадия
      // "Есть переписка", 30-дневное окно) — оба числа из одного и того же честного окна.
      const conversationsStage = data.funnel.stages.find((s) => s.label === "Есть переписка");
      const conversations = conversationsStage ? conversationsStage.count : 0;
      const leadStage = data.funnel.stages[data.funnel.stages.length - 1];
      const conversion = leadStage.percent_of_previous != null ? leadStage.percent_of_previous : 0;

      // Дельта — текущий выбранный период vs такой же по длине предыдущий (period_comparison,
      // отдельный от воронки расчёт — той нельзя доверять дальше её safety-окна)
      const pc = data.period_comparison;
      const conversationsDelta = deltaBadge(pc.conversations.current, pc.conversations.previous);
      const prevConversion = pc.conversations.previous > 0 ? (pc.leads.previous / pc.conversations.previous) * 100 : 0;
      const conversionDelta = deltaBadge(conversion, prevConversion);

      const waitMinutes = data.queue_wait.avg_wait_minutes;

      return `
        <div class="tiles">
          <div class="tile"><div class="tile-label">Всего лидов</div><div class="tile-value">${fmt(totalLeads)}</div></div>
          <div class="tile"><div class="tile-label">Лидов за месяц</div><div class="tile-value accent">${fmt(thisMonth ? thisMonth.count : 0)}</div></div>
          <div class="tile"><div class="tile-label">Диалогов (${data.funnel.days} дн.)</div><div class="tile-value">${fmt(conversations)}${conversationsDelta}</div></div>
          <div class="tile"><div class="tile-label">Конверсия (${data.funnel.days} дн.)</div><div class="tile-value">${conversion}%${conversionDelta}</div></div>
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

    function renderOperators(operators) {
      const entries = Object.entries(operators).sort((a, b) => (b[1].leads || 0) - (a[1].leads || 0));
      if (!entries.length) {
        return `<div class="card"><h2>Операторы</h2><p class="card-hint">За всё время</p><div class="empty-state">Пока нет ни одного взятого в работу диалога</div></div>`;
      }
      const rows = entries.map(([name, stats]) => `
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
            ${trend.length <= 16 ? `<span class="month-label">${label}</span>` : ""}
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
          ${h.hour % 3 === 0 ? `<span class="month-label">${h.hour}</span>` : ""}
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

    async function load() {
      const content = document.getElementById("content");
      const companyId = document.getElementById("companySelect").value;
      const days = document.getElementById("daysSelect").value;
      content.innerHTML = '<div class="loading">Загрузка…</div>';
      try {
        const data = await fetchDashboard(companyId, days);
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

    document.getElementById("companySelect").addEventListener("change", load);
    document.getElementById("daysSelect").addEventListener("change", load);
    load();
  </script>
</body>
</html>
"""

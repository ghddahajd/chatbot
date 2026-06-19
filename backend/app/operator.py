"""Operator panel rendering helpers."""


def render_operator_panel() -> str:
    """Return a minimal operator HTML page for the MVP."""

    return """<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Operator Panel</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #eef3fb;
        --panel: #ffffff;
        --panel-soft: #f8fbff;
        --ink: #263041;
        --muted: #738199;
        --line: #dce5f2;
        --accent: #4d86e6;
        --accent-soft: #eaf2ff;
        --accent-strong: #3f77d7;
        --user: #e8f0ff;
        --assistant: #f3f6fb;
        --operator: #edf6ff;
        --system: #f8f9fc;
      }
      * { box-sizing: border-box; }
      html {
        height: 100%;
        overflow: hidden;
      }
      body {
        margin: 0;
        height: 100%;
        overflow: hidden;
        font-family: "Inter", "Segoe UI", "Helvetica Neue", sans-serif;
        background:
          radial-gradient(circle at top left, rgba(215, 230, 255, 0.8) 0%, transparent 32%),
          linear-gradient(135deg, #f5f8fe 0%, #edf2fb 100%);
        color: var(--ink);
      }
      .layout {
        height: 100vh;
        overflow: hidden;
        display: grid;
        grid-template-columns: 320px 1fr;
      }
      .sidebar, .main {
        padding: 18px;
        min-height: 0;
      }
      .sidebar {
        display: grid;
        grid-template-rows: auto minmax(0, 1fr);
        gap: 16px;
        overflow: hidden;
        border-right: 1px solid var(--line);
        background: rgba(248, 251, 255, 0.88);
        backdrop-filter: blur(8px);
      }
      .main {
        display: grid;
        grid-template-rows: auto minmax(0, 1fr) auto;
        gap: 14px;
        height: 100%;
        overflow: hidden;
      }
      .panel {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 22px;
        padding: 18px;
        box-shadow: 0 18px 40px rgba(72, 95, 130, 0.1);
      }
      .sessions-panel {
        min-height: 0;
        overflow: auto;
      }
      h1, h2 {
        margin: 0 0 10px;
        font-weight: 700;
        letter-spacing: -0.02em;
      }
      p, li, button, input {
        font: inherit;
      }
      ul {
        list-style: none;
        padding: 0;
        margin: 0;
      }
      .eyebrow {
        margin: 0 0 8px;
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.14em;
        font-size: 11px;
      }
      .subtle {
        color: var(--muted);
        font-size: 13px;
        line-height: 1.45;
      }
      .session-item {
        padding: 14px;
        border: 1px solid var(--line);
        border-radius: 16px;
        margin-bottom: 10px;
        cursor: pointer;
        background: var(--panel-soft);
        transition: 120ms ease;
      }
      .session-item.active {
        border-color: rgba(77, 134, 230, 0.45);
        background: var(--accent-soft);
        box-shadow: inset 0 0 0 1px rgba(77, 134, 230, 0.1);
      }
      .session-item:hover {
        transform: translateY(-1px);
      }
      .status {
        color: var(--muted);
        font-size: 13px;
      }
      .status-chip {
        display: inline-flex;
        align-items: center;
        gap: 7px;
        padding: 7px 10px;
        border-radius: 999px;
        background: var(--accent-soft);
        color: #587196;
        font-size: 12px;
      }
      .status-chip.waiting {
        background: #eef5ff;
        color: #587196;
      }
      .status-chip.active {
        background: #ecf8f1;
        color: #4f8063;
      }
      .status-chip.closed {
        background: #f3f5f8;
        color: #7c8898;
      }
      .status-dot {
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: #78a9ff;
      }
      .status-chip.active .status-dot {
        background: #61b17a;
      }
      .status-chip.closed .status-dot {
        background: #a6b0bf;
      }
      .toolbar {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
      }
      .chat-meta {
        display: grid;
        gap: 12px;
      }
      .chat-callout {
        display: none;
        padding: 14px 16px;
        border-radius: 16px;
        background: #eef5ff;
        border: 1px solid #d7e6fb;
        color: #587196;
        font-size: 13px;
        line-height: 1.5;
      }
      .chat-callout.visible {
        display: block;
      }
      .history {
        overflow: auto;
        min-height: 0;
        height: 100%;
        display: flex;
        flex-direction: column;
        gap: 12px;
        background: linear-gradient(180deg, #ffffff 0%, #fbfcff 100%);
      }
      .msg {
        max-width: 76%;
        padding: 11px 13px;
        border-radius: 16px;
        border: 1px solid var(--line);
        line-height: 1.45;
        font-size: 14px;
        white-space: pre-wrap;
        word-break: break-word;
      }
      .msg.user {
        align-self: flex-start;
        background: #f5f8fd;
      }
      .msg.assistant {
        align-self: flex-start;
        background: var(--assistant);
      }
      .msg.operator {
        align-self: flex-end;
        background: var(--user);
      }
      .msg.system {
        align-self: center;
        max-width: 62%;
        background: var(--system);
        color: #647288;
        font-size: 13px;
      }
      .handoff-action {
        align-self: center;
        display: flex;
        justify-content: center;
        width: min(62%, 520px);
        margin-top: -6px;
      }
      .handoff-action button {
        padding: 10px 14px;
        border-radius: 12px;
        font-size: 13px;
      }
      .msg-head {
        display: inline-flex;
        margin-bottom: 6px;
        padding: 3px 8px;
        border-radius: 999px;
        background: rgba(77, 134, 230, 0.08);
        color: #6883ad;
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.12em;
      }
      .msg.system .msg-head {
        background: rgba(126, 141, 166, 0.12);
        color: #8390a4;
      }
      .divider {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 2px 0 6px;
        color: #9aa6b8;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      .divider::before,
      .divider::after {
        content: "";
        height: 1px;
        flex: 1;
        background: linear-gradient(90deg, rgba(197, 207, 223, 0) 0%, rgba(197, 207, 223, 0.9) 50%, rgba(197, 207, 223, 0) 100%);
      }
      .row {
        display: flex;
        gap: 10px;
      }
      input {
        flex: 1;
        padding: 14px 16px;
        border-radius: 14px;
        border: 1px solid var(--line);
        background: #fff;
        color: var(--ink);
      }
      button {
        padding: 12px 16px;
        border: 0;
        border-radius: 14px;
        background: linear-gradient(180deg, var(--accent) 0%, var(--accent-strong) 100%);
        color: #fff;
        cursor: pointer;
        font-weight: 600;
        box-shadow: 0 10px 18px rgba(77, 134, 230, 0.18);
      }
      button.secondary {
        background: linear-gradient(180deg, #8593a8 0%, #6f7d91 100%);
        box-shadow: none;
      }
      button:disabled {
        opacity: 0.55;
        cursor: not-allowed;
        box-shadow: none;
      }
      @media (max-width: 900px) {
        .layout {
          grid-template-columns: 1fr;
        }
        .sidebar {
          border-right: 0;
          border-bottom: 1px solid var(--line);
        }
      }
    </style>
  </head>
  <body>
    <div class="layout">
      <aside class="sidebar">
        <div class="panel">
          <p class="eyebrow">Operator panel</p>
          <h1>Чаты поддержки</h1>
          <p class="subtle">Сессии со статусами Ждет оператора и Оператор в диалоге.</p>
          <button id="refreshSessions" class="secondary">Обновить список</button>
        </div>
        <div class="panel sessions-panel">
          <ul id="sessions"></ul>
        </div>
      </aside>
      <main class="main">
        <div class="panel">
          <p class="eyebrow">Active chat</p>
          <h2 id="sessionTitle">Чат не выбран</h2>
          <div class="chat-meta">
            <div id="sessionStatusChip" class="status-chip"><span class="status-dot"></span><span id="sessionStatus">Выберите сессию слева</span></div>
            <div id="chatCallout" class="chat-callout"></div>
            <div class="toolbar">
              <button id="closeChat" class="secondary">Закрыть чат</button>
            </div>
          </div>
        </div>
        <div class="panel history" id="history"></div>
        <div class="panel">
          <div class="row">
            <input id="messageInput" placeholder="Сообщение клиенту" />
            <button id="sendMessage">Отправить</button>
          </div>
        </div>
      </main>
    </div>
    <script>
      const token = new URLSearchParams(window.location.search).get("token");
      let currentSessionId = null;
      let ws = null;
      let wsSessionId = null;
      let currentStatus = null;

      function humanStatus(status) {
        const labels = {
          WAITING_OPERATOR: "Ждёт оператора",
          HUMAN_ACTIVE: "Оператор в диалоге",
          CLOSED: "Диалог завершён",
          AI_ACTIVE: "AI активен",
        };
        return labels[status] || "Выберите сессию слева";
      }

      function authFetch(url, options = {}) {
        const headers = new Headers(options.headers || {});
        headers.set("x-operator-token", token || "");
        return fetch(url, { ...options, headers });
      }

      function updateControls() {
        const closeButton = document.getElementById("closeChat");
        const sendButton = document.getElementById("sendMessage");
        const input = document.getElementById("messageInput");
        const statusChip = document.getElementById("sessionStatusChip");
        const callout = document.getElementById("chatCallout");

        const hasSession = Boolean(currentSessionId);
        const isHumanActive = currentStatus === "HUMAN_ACTIVE";
        const isWaiting = currentStatus === "WAITING_OPERATOR";
        const isClosed = currentStatus === "CLOSED";

        closeButton.disabled = !hasSession || isClosed;
        sendButton.disabled = !hasSession || !isHumanActive || !ws || ws.readyState !== WebSocket.OPEN;
        input.disabled = !hasSession || !isHumanActive || !ws || ws.readyState !== WebSocket.OPEN;

        statusChip.className = "status-chip";
        if (isWaiting) statusChip.classList.add("waiting");
        if (isHumanActive) statusChip.classList.add("active");
        if (isClosed) statusChip.classList.add("closed");

        if (isWaiting) {
          callout.classList.add("visible");
          callout.textContent = "Клиент ждёт подключения специалиста. Взять чат можно в истории рядом с событием ожидания.";
        } else if (isHumanActive) {
          callout.classList.add("visible");
          callout.textContent = "Чат уже в работе. Сообщения отсюда сразу уходят в клиентский виджет.";
        } else if (isClosed) {
          callout.classList.add("visible");
          callout.textContent = "Диалог завершён. Если нужно, клиент может начать новый чат со своей стороны.";
        } else {
          callout.classList.remove("visible");
          callout.textContent = "";
        }
      }

      function isWaitingSystemMessage(item) {
        return (
          currentStatus === "WAITING_OPERATOR" &&
          item.role === "system" &&
          item.text === "Ожидаем подключения специалиста. Ваше сообщение сохранено в истории диалога."
        );
      }

      function isHandoffMessage(item) {
        return (
          item.role === "assistant" &&
          typeof item.text === "string" &&
          item.text.startsWith("Передаю диалог специалисту.")
        );
      }

      function displayRole(item) {
        return isHandoffMessage(item) ? "system" : item.role;
      }

      function appendTakeChatAction(history) {
        const action = document.createElement("div");
        action.className = "handoff-action";
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Взять чат";
        button.onclick = takeChat;
        action.appendChild(button);
        history.appendChild(action);
      }

      function shouldShowTakeAction(item, index, messages) {
        if (currentStatus !== "WAITING_OPERATOR") return false;
        if (isWaitingSystemMessage(item)) return true;
        return index === messages.length - 1;
      }

      function renderHistory(messages) {
        const history = document.getElementById("history");
        history.innerHTML = "";
        if (!messages.length) {
          history.innerHTML = '<div class="divider">История</div><div class="msg system"><div class="msg-head">система</div><div>Сообщений пока нет.</div></div>';
          return;
        }
        const divider = document.createElement("div");
        divider.className = "divider";
        divider.textContent = "История";
        history.appendChild(divider);
        let takeActionAdded = false;
        for (let index = 0; index < messages.length; index += 1) {
          const item = messages[index];
          const role = displayRole(item);
          const el = document.createElement("div");
          el.className = `msg ${role}`;
          el.innerHTML = `<div class="msg-head">${role}</div><div>${item.text}</div>`;
          if (!takeActionAdded && shouldShowTakeAction(item, index, messages)) {
            history.appendChild(el);
            appendTakeChatAction(history);
            takeActionAdded = true;
            continue;
          }
          history.appendChild(el);
        }
        history.scrollTop = history.scrollHeight;
      }

      async function loadSessions() {
        const res = await authFetch("/api/operator/sessions");
        if (!res.ok) return;
        const sessions = await res.json();
        const list = document.getElementById("sessions");
        list.innerHTML = "";
        for (const item of sessions) {
          const el = document.createElement("li");
          el.className = "session-item" + (item.session_id === currentSessionId ? " active" : "");
          el.innerHTML = `<strong>${item.session_id}</strong><div class="status" style="margin-top:6px;">${humanStatus(item.status)}</div><div class="subtle" style="margin-top:8px;">${item.last_message || "Без сообщений"}</div>`;
          el.onclick = () => loadSession(item.session_id);
          list.appendChild(el);
        }
      }

      async function loadSession(sessionId) {
        currentSessionId = sessionId;
        const res = await authFetch(`/api/operator/sessions/${sessionId}`);
        if (!res.ok) return;
        const data = await res.json();
        currentStatus = data.status;
        document.getElementById("sessionTitle").textContent = `Чат ${data.session_id}`;
        document.getElementById("sessionStatus").textContent = humanStatus(data.status);
        renderHistory(data.messages);
        updateControls();
        if (currentStatus === "HUMAN_ACTIVE") {
          connectWs();
        }
        await loadSessions();
      }

      async function takeChat() {
        if (!currentSessionId || currentStatus === "HUMAN_ACTIVE") return;
        const res = await authFetch(`/api/operator/sessions/${currentSessionId}/take`, { method: "POST" });
        if (!res.ok) return;
        await loadSession(currentSessionId);
        connectWs();
      }

      async function closeChat() {
        if (!currentSessionId) return;
        await authFetch(`/api/operator/sessions/${currentSessionId}/close`, { method: "POST" });
        if (ws) {
          ws.close();
          ws = null;
          wsSessionId = null;
        }
        await loadSession(currentSessionId);
        await loadSessions();
      }

      function connectWs() {
        if (!currentSessionId) return;
        if (ws && wsSessionId === currentSessionId && ws.readyState <= 1) {
          updateControls();
          return;
        }
        if (ws) {
          ws.close();
        }
        const proto = window.location.protocol === "https:" ? "wss" : "ws";
        wsSessionId = currentSessionId;
        ws = new WebSocket(`${proto}://${window.location.host}/ws/operator?token=${encodeURIComponent(token || "")}&session_id=${encodeURIComponent(currentSessionId)}`);
        ws.onopen = () => {
          currentStatus = "HUMAN_ACTIVE";
          document.getElementById("sessionStatus").textContent = humanStatus(currentStatus);
          updateControls();
        };
        ws.onmessage = async (event) => {
          const payload = JSON.parse(event.data);
          if (payload.type === "message" || payload.type === "history_update") {
            await loadSession(currentSessionId);
          }
        };
        ws.onclose = () => {
          ws = null;
          wsSessionId = null;
          updateControls();
        };
      }

      async function sendMessage() {
        if (!ws || !currentSessionId || ws.readyState !== WebSocket.OPEN) return;
        const input = document.getElementById("messageInput");
        const text = input.value.trim();
        if (!text) return;
        ws.send(JSON.stringify({ session_id: currentSessionId, text }));
        input.value = "";
      }

      document.getElementById("refreshSessions").onclick = loadSessions;
      document.getElementById("closeChat").onclick = closeChat;
      document.getElementById("sendMessage").onclick = sendMessage;
      document.getElementById("messageInput").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          sendMessage();
        }
      });
      updateControls();
      loadSessions();
    </script>
  </body>
</html>
"""

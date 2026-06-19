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
        --bg: #f3efe7;
        --panel: #fffdf8;
        --ink: #1f1c18;
        --muted: #6c6255;
        --line: #d8ccbc;
        --accent: #b85c38;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        background:
          radial-gradient(circle at top left, #fff7eb 0%, transparent 35%),
          linear-gradient(135deg, #f5efe5 0%, #ece2d2 100%);
        color: var(--ink);
      }
      .layout {
        min-height: 100vh;
        display: grid;
        grid-template-columns: 320px 1fr;
      }
      .sidebar, .main {
        padding: 20px;
      }
      .sidebar {
        border-right: 1px solid var(--line);
        background: rgba(255, 253, 248, 0.82);
        backdrop-filter: blur(8px);
      }
      .main {
        display: grid;
        grid-template-rows: auto 1fr auto;
        gap: 16px;
      }
      .panel {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 16px;
        box-shadow: 0 12px 30px rgba(87, 59, 33, 0.08);
      }
      h1, h2 {
        margin: 0 0 12px;
      }
      p, li, button, input {
        font: inherit;
      }
      ul {
        list-style: none;
        padding: 0;
        margin: 0;
      }
      .session-item {
        padding: 12px;
        border: 1px solid var(--line);
        border-radius: 12px;
        margin-bottom: 10px;
        cursor: pointer;
        background: #fffaf2;
      }
      .session-item.active {
        border-color: var(--accent);
      }
      .status {
        color: var(--muted);
        font-size: 14px;
      }
      .history {
        overflow: auto;
        min-height: 280px;
        max-height: 55vh;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .msg {
        padding: 10px 12px;
        border-radius: 12px;
        background: #fffaf2;
        border: 1px solid var(--line);
      }
      .msg strong {
        display: block;
        margin-bottom: 4px;
      }
      .row {
        display: flex;
        gap: 10px;
      }
      input {
        flex: 1;
        padding: 12px;
        border-radius: 12px;
        border: 1px solid var(--line);
        background: #fff;
      }
      button {
        padding: 12px 16px;
        border: 0;
        border-radius: 12px;
        background: var(--accent);
        color: #fff;
        cursor: pointer;
      }
      button.secondary {
        background: #6c6255;
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
          <h1>Operator</h1>
          <p class="status">Сессии со статусами WAITING_OPERATOR и HUMAN_ACTIVE</p>
          <button id="refreshSessions" class="secondary">Обновить</button>
        </div>
        <div class="panel" style="margin-top: 16px;">
          <ul id="sessions"></ul>
        </div>
      </aside>
      <main class="main">
        <div class="panel">
          <h2 id="sessionTitle">Чат не выбран</h2>
          <p class="status" id="sessionStatus">Выберите сессию слева</p>
          <div class="row">
            <button id="takeChat">Взять чат</button>
            <button id="closeChat" class="secondary">Закрыть чат</button>
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

      function authFetch(url, options = {}) {
        const headers = new Headers(options.headers || {});
        headers.set("x-operator-token", token || "");
        return fetch(url, { ...options, headers });
      }

      function renderHistory(messages) {
        const history = document.getElementById("history");
        history.innerHTML = "";
        for (const item of messages) {
          const el = document.createElement("div");
          el.className = "msg";
          el.innerHTML = `<strong>${item.role}</strong><span>${item.text}</span>`;
          history.appendChild(el);
        }
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
          el.innerHTML = `<strong>${item.session_id}</strong><div class="status">${item.status}</div><div>${item.last_message || ""}</div>`;
          el.onclick = () => loadSession(item.session_id);
          list.appendChild(el);
        }
      }

      async function loadSession(sessionId) {
        currentSessionId = sessionId;
        const res = await authFetch(`/api/operator/sessions/${sessionId}`);
        if (!res.ok) return;
        const data = await res.json();
        document.getElementById("sessionTitle").textContent = `Чат ${data.session_id}`;
        document.getElementById("sessionStatus").textContent = data.status;
        renderHistory(data.messages);
        await loadSessions();
      }

      async function takeChat() {
        if (!currentSessionId) return;
        const res = await authFetch(`/api/operator/sessions/${currentSessionId}/take`, { method: "POST" });
        if (!res.ok) return;
        connectWs();
        await loadSession(currentSessionId);
      }

      async function closeChat() {
        if (!currentSessionId) return;
        await authFetch(`/api/operator/sessions/${currentSessionId}/close`, { method: "POST" });
        if (ws) {
          ws.close();
          ws = null;
        }
        await loadSession(currentSessionId);
        await loadSessions();
      }

      function connectWs() {
        if (!currentSessionId) return;
        if (ws) ws.close();
        const proto = window.location.protocol === "https:" ? "wss" : "ws";
        ws = new WebSocket(`${proto}://${window.location.host}/ws/operator?token=${encodeURIComponent(token || "")}&session_id=${encodeURIComponent(currentSessionId)}`);
        ws.onmessage = async (event) => {
          const payload = JSON.parse(event.data);
          if (payload.type === "message" || payload.type === "history_update") {
            await loadSession(currentSessionId);
          }
        };
      }

      async function sendMessage() {
        if (!ws || !currentSessionId) return;
        const input = document.getElementById("messageInput");
        const text = input.value.trim();
        if (!text) return;
        ws.send(JSON.stringify({ session_id: currentSessionId, text }));
        input.value = "";
      }

      document.getElementById("refreshSessions").onclick = loadSessions;
      document.getElementById("takeChat").onclick = takeChat;
      document.getElementById("closeChat").onclick = closeChat;
      document.getElementById("sendMessage").onclick = sendMessage;
      loadSessions();
    </script>
  </body>
</html>
"""

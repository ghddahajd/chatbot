(function () {
  const SCRIPT = document.currentScript;
  const COMPANY_ID = (SCRIPT && SCRIPT.dataset.companyId) || "rosh_demo";
  const API_BASE =
    (SCRIPT && SCRIPT.dataset.apiBase) ||
    (SCRIPT && new URL(SCRIPT.src, window.location.href).origin) ||
    window.location.origin;

  const STATUS = {
    AI_ACTIVE: "AI_ACTIVE",
    WAITING_OPERATOR: "WAITING_OPERATOR",
    HUMAN_ACTIVE: "HUMAN_ACTIVE",
    CLOSED: "CLOSED",
  };

  const STORAGE_KEY = "ai-chat-widget-session-id";

  const template = document.createElement("template");
  template.innerHTML = `
    <style>
      :host { all: initial; }
      .shell {
        position: fixed;
        right: 24px;
        bottom: 24px;
        z-index: 2147483647;
        font-family: "Inter", "Segoe UI", "Helvetica Neue", sans-serif;
        color: #263041;
      }
      .launcher {
        width: 58px;
        height: 58px;
        border: 0;
        border-radius: 18px;
        background: linear-gradient(180deg, #f7f9fd 0%, #eef3fb 100%);
        color: #627089;
        font: inherit;
        font-size: 26px;
        font-weight: 500;
        cursor: pointer;
        box-shadow: 0 16px 38px rgba(50, 73, 107, 0.16);
      }
      .panel {
        width: min(500px, calc(100vw - 32px));
        height: min(650px, calc(100vh - 110px));
        display: none;
        grid-template-rows: auto auto 1fr auto;
        overflow: hidden;
        margin-top: 14px;
        border: 1px solid rgba(121, 138, 166, 0.16);
        border-radius: 26px;
        background: linear-gradient(180deg, rgba(255, 255, 255, 0.98) 0%, rgba(249, 251, 255, 0.98) 100%);
        box-shadow: 0 28px 70px rgba(46, 66, 99, 0.18);
        backdrop-filter: blur(10px);
      }
      .panel.open { display: grid; }
      .header {
        position: relative;
        padding: 20px 24px 18px;
        background: linear-gradient(180deg, #ffffff 0%, #f7f9fd 100%);
        border-bottom: 1px solid rgba(121, 138, 166, 0.12);
      }
      .close {
        position: absolute;
        top: 16px;
        right: 16px;
        width: 40px;
        height: 40px;
        border: 0;
        border-radius: 999px;
        background: linear-gradient(180deg, #f4f7fc 0%, #ebf1f9 100%);
        color: #8b96aa;
        font: inherit;
        font-size: 26px;
        line-height: 1;
        cursor: pointer;
        display: grid;
        place-items: center;
        box-shadow: inset 0 0 0 1px rgba(121, 138, 166, 0.12);
      }
      .eyebrow {
        display: none;
      }
      .title {
        margin: 0;
        font-size: 18px;
        font-weight: 700;
        text-align: center;
        color: #3a475d;
      }
      .subtitle {
        margin: 10px auto 0;
        max-width: 390px;
        font-size: 13px;
        line-height: 1.5;
        text-align: center;
        color: #7a869a;
      }
      .statusbar {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 12px 20px;
        background: #fbfcff;
        font-size: 12px;
        color: #7b8799;
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: #6ea8ff;
        box-shadow: 0 0 0 5px rgba(110, 168, 255, 0.14);
      }
      .banner {
        display: none;
        margin: 0;
        padding: 13px 15px;
        border-radius: 14px;
        background: #eef5ff;
        border: 1px solid #d7e6fb;
        color: #587196;
        font-size: 13px;
        line-height: 1.5;
      }
      .banner.visible { display: block; }
      .actions {
        display: none;
        padding: 0;
      }
      .actions.visible { display: block; }
      .service {
        display: grid;
        gap: 10px;
        padding: 12px 18px 14px;
        border-top: 1px solid rgba(121, 138, 166, 0.1);
        border-bottom: 1px solid rgba(121, 138, 166, 0.1);
        background: linear-gradient(180deg, #fcfdff 0%, #f8fbff 100%);
      }
      .service.hidden {
        display: none;
      }
      .ghost {
        border: 1px solid rgba(121, 138, 166, 0.22);
        border-radius: 14px;
        background: rgba(255,255,255,0.9);
        color: #556378;
        font: inherit;
        padding: 10px 12px;
        cursor: pointer;
      }
      .messages {
        padding: 14px 18px;
        overflow: auto;
        display: flex;
        flex-direction: column;
        gap: 12px;
        background: #ffffff;
      }
      .messages.with-service {
        padding-top: 14px;
      }
      .divider {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 2px 0 4px;
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
      .empty {
        padding: 16px 18px;
        border-radius: 16px;
        background: #f8fbff;
        border: 1px dashed rgba(121, 138, 166, 0.3);
        color: #657389;
        font-size: 14px;
        line-height: 1.5;
      }
      .message {
        max-width: 82%;
        padding: 11px 13px;
        border-radius: 14px;
        line-height: 1.45;
        font-size: 13px;
        white-space: pre-wrap;
        word-break: break-word;
      }
      .message.user {
        align-self: flex-end;
        background: linear-gradient(180deg, #ebf2ff 0%, #dce8ff 100%);
        color: #2f405d;
        border: 1px solid #d0def8;
        border-bottom-right-radius: 8px;
      }
      .message.assistant,
      .message.system {
        align-self: flex-start;
        background: #f7f9fc;
        color: #5f6f87;
        border: 1px solid #e6ecf5;
        border-bottom-left-radius: 8px;
        max-width: 68%;
        font-size: 12px;
      }
      .message.operator {
        align-self: flex-start;
        background: #eef6ff;
        color: #294566;
        border: 1px solid #d4e6fb;
        border-bottom-left-radius: 8px;
      }
      .badge {
        display: inline-block;
        margin-bottom: 5px;
        padding: 2px 7px;
        border-radius: 999px;
        background: #dfeafe;
        color: #5a78a4;
        font-size: 9px;
        text-transform: uppercase;
        letter-spacing: 0.12em;
      }
      .message.system .badge {
        background: #edf1f7;
        color: #8190a6;
      }
      .composer {
        display: grid;
        grid-template-columns: 1fr auto;
        align-items: end;
        gap: 10px;
        padding: 12px 18px 16px;
        border-top: 1px solid rgba(121, 138, 166, 0.12);
        background: linear-gradient(180deg, #ffffff 0%, #fbfcff 100%);
      }
      .composer.hidden {
        display: none;
      }
      .closed-note {
        display: none;
        padding: 12px 18px 16px;
        border-top: 1px solid rgba(121, 138, 166, 0.12);
        background: linear-gradient(180deg, #ffffff 0%, #fbfcff 100%);
      }
      .closed-note.visible {
        display: block;
      }
      .closed-note-inner {
        padding: 13px 15px;
        border-radius: 16px;
        background: #f7f9fc;
        border: 1px solid #e3eaf5;
        color: #657389;
        font-size: 13px;
        line-height: 1.45;
      }
      .input {
        min-height: 52px;
        max-height: 104px;
        padding: 14px 16px;
        border: 1px solid rgba(121, 138, 166, 0.22);
        border-radius: 16px;
        background: #fff;
        color: #223047;
        font: inherit;
        font-size: 14px;
        resize: none;
        outline: none;
      }
      .input:focus {
        border-color: rgba(110, 168, 255, 0.7);
        box-shadow: 0 0 0 4px rgba(110, 168, 255, 0.14);
      }
      .send {
        min-width: 118px;
        height: 46px;
        border: 0;
        border-radius: 14px;
        background: linear-gradient(180deg, #5f98f4 0%, #467fdd 100%);
        color: #fefefe;
        font: inherit;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
        padding: 0 16px;
        box-shadow: 0 10px 18px rgba(70, 127, 221, 0.16);
      }
      .send[disabled], .input[disabled] {
        opacity: 0.6;
        cursor: not-allowed;
      }
      @media (max-width: 640px) {
        .shell { right: 12px; bottom: 12px; }
        .panel { width: calc(100vw - 24px); height: min(78vh, 680px); }
        .composer {
          grid-template-columns: 1fr;
        }
        .send {
          width: 100%;
        }
      }
    </style>
    <div class="shell">
      <button class="launcher" type="button" aria-label="Открыть чат">+</button>
      <section class="panel" aria-live="polite">
        <header class="header">
          <p class="eyebrow">Medical concierge</p>
          <button class="close" type="button" aria-label="Закрыть чат">×</button>
          <h2 class="title">Чат с поддержкой</h2>
          <p class="subtitle">Подскажу по услугам, ценам и записи. Если нужно, передам диалог специалисту.</p>
        </header>
        <div class="statusbar"><span class="dot"></span><span class="status-text">AI-консультант на связи</span></div>
        <div class="service hidden">
          <div class="banner"></div>
          <div class="actions"><button class="ghost" type="button">Начать новый диалог</button></div>
        </div>
        <div class="messages"></div>
        <div class="composer">
          <textarea class="input" rows="1" placeholder="Напишите ваш вопрос"></textarea>
          <button class="send" type="button">Отправить</button>
        </div>
        <div class="closed-note">
          <div class="closed-note-inner">Чтобы продолжить, начните новый диалог сверху.</div>
        </div>
      </section>
    </div>
  `;

  class AIChatWidget extends HTMLElement {
    constructor() {
      super();
      this.state = {
        open: false,
        sending: false,
        status: STATUS.AI_ACTIVE,
        sessionId: window.localStorage.getItem(STORAGE_KEY) || "",
        ws: null,
      };
      this.shadow = this.attachShadow({ mode: "closed" });
      this.shadow.appendChild(template.content.cloneNode(true));
      this.elements = {
        launcher: this.shadow.querySelector(".launcher"),
        panel: this.shadow.querySelector(".panel"),
        messages: this.shadow.querySelector(".messages"),
        input: this.shadow.querySelector(".input"),
        send: this.shadow.querySelector(".send"),
        statusText: this.shadow.querySelector(".status-text"),
        banner: this.shadow.querySelector(".banner"),
        actions: this.shadow.querySelector(".actions"),
        reset: this.shadow.querySelector(".ghost"),
        close: this.shadow.querySelector(".close"),
        service: this.shadow.querySelector(".service"),
        composer: this.shadow.querySelector(".composer"),
        closedNote: this.shadow.querySelector(".closed-note"),
      };
    }

    connectedCallback() {
      this.bindEvents();
      this.pushEmptyMessage();
      this.restoreSession();
    }

    bindEvents() {
      this.elements.launcher.addEventListener("click", () => this.toggle());
      this.elements.close.addEventListener("click", () => this.toggle());
      this.elements.send.addEventListener("click", () => this.handleSubmit());
      this.elements.reset.addEventListener("click", () => this.startNewDialog());
      this.elements.input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          this.handleSubmit();
        }
      });
    }

    async restoreSession() {
      if (!this.state.sessionId) {
        this.applyState(STATUS.AI_ACTIVE);
        return;
      }

      try {
        const response = await fetch(API_BASE + "/api/chat/session/" + this.state.sessionId);
        if (!response.ok) {
          this.resetLocalSession();
          this.applyState(STATUS.AI_ACTIVE);
          return;
        }
        const payload = await response.json();
        this.renderHistory(payload.messages || []);
        this.applyState(payload.status);
        if (payload.status === STATUS.HUMAN_ACTIVE) {
          this.connectWebSocket();
        }
      } catch (error) {
        this.applyState(STATUS.AI_ACTIVE);
      }
    }

    toggle() {
      this.state.open = !this.state.open;
      this.elements.panel.classList.toggle("open", this.state.open);
      this.elements.launcher.textContent = this.state.open ? "×" : "+";
      if (this.state.open) {
        this.scrollToBottom();
        this.elements.input.focus();
      }
    }

    pushEmptyMessage() {
      if (this.elements.messages.childElementCount > 0) return;
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent =
        "Я отвечаю только по услугам центра и не даю медицинских рекомендаций. Спросите про услугу, цену, запись или попросите оператора.";
      this.elements.messages.appendChild(empty);
    }

    clearEmptyState() {
      const empty = this.elements.messages.querySelector(".empty");
      if (empty) empty.remove();
    }

    addDivider(label) {
      const divider = document.createElement("div");
      divider.className = "divider";
      divider.textContent = label;
      this.elements.messages.appendChild(divider);
    }

    shouldHideSystemMessage(text) {
      const compact = String(text || "").trim();
      return (
        compact === "Ожидаем подключения специалиста. Ваше сообщение сохранено в истории диалога." ||
        compact === "Специалист подключился к диалогу" ||
        compact === "Диалог завершён. Если остались вопросы — напишите снова."
      );
    }

    renderHistory(messages) {
      this.elements.messages.innerHTML = "";
      if (!messages.length) {
        this.pushEmptyMessage();
        return;
      }
      this.addDivider("История");
      for (const item of messages) {
        if (item.role === "system" && this.shouldHideSystemMessage(item.text)) {
          continue;
        }
        this.addMessage(item.role, item.text, true);
      }
      this.scrollToBottom();
    }

    addMessage(role, text, silent) {
      this.clearEmptyState();
      const node = document.createElement("article");
      node.className = "message " + role;

      if (role === "operator") {
        const badge = document.createElement("div");
        badge.className = "badge";
        badge.textContent = "Специалист";
        node.appendChild(badge);
      } else if (role === "system") {
        if (this.shouldHideSystemMessage(text)) {
          return;
        }
        const badge = document.createElement("div");
        badge.className = "badge";
        badge.textContent = "Система";
        node.appendChild(badge);
      }

      const body = document.createElement("div");
      body.textContent = text;
      node.appendChild(body);
      this.elements.messages.appendChild(node);
      if (!silent) {
        if (!this.elements.messages.querySelector(".divider")) {
          this.addDivider("История");
        }
        this.scrollToBottom();
      }
    }

    applyState(status) {
      this.state.status = status;
      const labels = {
        AI_ACTIVE: "AI-консультант на связи",
        WAITING_OPERATOR: "Ожидаем подключения специалиста",
        HUMAN_ACTIVE: "В диалоге специалист",
        CLOSED: "Диалог завершён",
      };
      const banners = {
        AI_ACTIVE: "",
        WAITING_OPERATOR:
          "Ожидаем подключения специалиста. Вы можете дописать детали, они сохранятся в истории диалога.",
        HUMAN_ACTIVE: "Специалист подключён к диалогу.",
        CLOSED: "",
      };

      this.elements.statusText.textContent = labels[status] || labels.AI_ACTIVE;
      this.elements.banner.textContent = banners[status] || "";
      this.elements.banner.classList.toggle("visible", Boolean(banners[status]));
      this.elements.actions.classList.toggle(
        "visible",
        status === STATUS.WAITING_OPERATOR || status === STATUS.CLOSED
      );
      this.elements.service.classList.toggle("hidden", !banners[status] && status !== STATUS.WAITING_OPERATOR && status !== STATUS.CLOSED);
      this.elements.messages.classList.toggle("with-service", !this.elements.service.classList.contains("hidden"));
      this.elements.composer.classList.toggle("hidden", status === STATUS.CLOSED);
      this.elements.closedNote.classList.toggle("visible", status === STATUS.CLOSED);
      this.elements.input.disabled = status === STATUS.CLOSED;
      this.elements.send.disabled = status === STATUS.CLOSED;
    }

    async handleSubmit() {
      const text = this.elements.input.value.trim();
      if (!text || this.state.sending || this.state.status === STATUS.CLOSED) return;

      this.elements.input.value = "";
      this.addMessage("user", text);

      if (this.state.status === STATUS.HUMAN_ACTIVE && this.state.ws) {
        this.state.ws.send(text);
        return;
      }

      this.state.sending = true;
      this.elements.send.disabled = true;

      try {
        const response = await fetch(API_BASE + "/api/chat/message", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: this.state.sessionId || null,
            company_id: COMPANY_ID,
            message: text,
          }),
        });
        if (!response.ok) throw new Error("Chat request failed");

        const payload = await response.json();
        if (payload.session_id) {
          this.state.sessionId = payload.session_id;
          window.localStorage.setItem(STORAGE_KEY, payload.session_id);
        }
        this.applyState(payload.status);
        if (payload.answer) {
          const role =
            payload.status === STATUS.HUMAN_ACTIVE || payload.action === "reject" ? "system" : "assistant";
          this.addMessage(role, payload.answer);
        }
        if (payload.status === STATUS.WAITING_OPERATOR || payload.status === STATUS.HUMAN_ACTIVE) {
          this.connectWebSocket();
        }
      } catch (error) {
        this.addMessage("system", "Не удалось отправить сообщение. Попробуйте ещё раз.");
      } finally {
        this.state.sending = false;
        this.elements.send.disabled = this.state.status === STATUS.CLOSED;
      }
    }

    async startNewDialog() {
      const previousSessionId = this.state.sessionId;
      if (previousSessionId && this.state.status === STATUS.WAITING_OPERATOR) {
        try {
          await fetch(API_BASE + "/api/chat/session/" + previousSessionId + "/cancel", {
            method: "POST",
          });
        } catch (error) {
          // Ignore best-effort cancel failures for the MVP.
        }
      }

      if (this.state.ws) {
        this.state.ws.close();
        this.state.ws = null;
      }
      this.resetLocalSession();
      this.elements.messages.innerHTML = "";
      this.pushEmptyMessage();
      this.applyState(STATUS.AI_ACTIVE);
      this.addMessage("system", "Начат новый диалог.");
    }

    resetLocalSession() {
      this.state.sessionId = "";
      this.state.status = STATUS.AI_ACTIVE;
      window.localStorage.removeItem(STORAGE_KEY);
    }

    connectWebSocket() {
      if (!this.state.sessionId) return;
      if (this.state.ws && this.state.ws.readyState <= 1) return;

      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      this.state.ws = new WebSocket(protocol + "//" + new URL(API_BASE).host + "/ws/chat/" + this.state.sessionId);

      this.state.ws.addEventListener("message", (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.type === "operator_joined") {
            this.applyState(STATUS.HUMAN_ACTIVE);
            this.addMessage("system", payload.text);
            return;
          }
          if (payload.type === "operator_left") {
            this.applyState(STATUS.CLOSED);
            this.addMessage("system", payload.text);
            return;
          }
          if (payload.type === "message" && payload.role === "operator") {
            this.applyState(STATUS.HUMAN_ACTIVE);
            this.addMessage("operator", payload.text);
          }
        } catch (error) {
          this.addMessage("system", "Ошибка обработки сообщения оператора.");
        }
      });

      this.state.ws.addEventListener("close", () => {
        if (this.state.status === STATUS.HUMAN_ACTIVE) {
          this.applyState(STATUS.CLOSED);
        }
      });
    }

    scrollToBottom() {
      requestAnimationFrame(() => {
        this.elements.messages.scrollTop = this.elements.messages.scrollHeight;
      });
    }
  }

  if (!window.customElements.get("ai-chat-widget")) {
    window.customElements.define("ai-chat-widget", AIChatWidget);
  }

  function initWidget() {
    if (document.querySelector("ai-chat-widget")) return;
    const widget = document.createElement("ai-chat-widget");
    document.body.appendChild(widget);
  }

  window.addEventListener("load", function () {
    window.setTimeout(initWidget, 5000);
  });
})();

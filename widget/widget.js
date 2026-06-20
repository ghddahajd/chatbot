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
  const MIN_TYPING_VISIBLE_MS = 450;

  const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  const nextFrame = () => new Promise((resolve) => window.requestAnimationFrame(resolve));

  const STORAGE_KEY = "ai-chat-widget-session-id";

  const template = document.createElement("template");
  template.innerHTML = `
    <style>
      @import url("https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&family=Work+Sans:wght@400;500;600;700&display=swap");

      :host {
        all: initial;
        --bg-primary: #FAF6F0;
        --bg-primary-deep: #EFE7DA;
        --bg-secondary: #FFFFFF;
        --accent: #1F7A5C;
        --accent-strong: #234C3F;
        --accent-soft: #E8F0EC;
        --text-primary: #1F2922;
        --text-secondary: #6B7670;
        --border-subtle: #E5DFD5;
        --shadow-soft: 0 2px 12px rgba(45, 95, 79, 0.08);
        --shadow-panel: 0 28px 70px rgba(45, 95, 79, 0.16);
        --radius-message: 16px;
        --radius-control: 12px;
      }
      .shell {
        position: fixed;
        right: 24px;
        bottom: 24px;
        z-index: 2147483647;
        font-family: "Plus Jakarta Sans", "Work Sans", "Avenir Next", sans-serif;
        color: var(--text-primary);
      }
      .launcher {
        width: 58px;
        height: 58px;
        border: 0;
        border-radius: 20px;
        background: linear-gradient(180deg, var(--bg-secondary) 0%, var(--bg-primary) 100%);
        color: var(--accent);
        font: inherit;
        font-size: 26px;
        font-weight: 600;
        cursor: pointer;
        box-shadow: var(--shadow-panel);
      }
      .launcher.hidden {
        display: none;
      }
      .panel {
        width: min(500px, calc(100vw - 32px));
        height: min(650px, calc(100vh - 110px));
        display: none;
        grid-template-rows: auto auto 1fr auto;
        overflow: hidden;
        margin-top: 14px;
        border: 1px solid var(--border-subtle);
        border-radius: 26px;
        background: linear-gradient(180deg, rgba(255, 255, 255, 0.98) 0%, rgba(250, 246, 240, 0.98) 100%);
        box-shadow: var(--shadow-panel);
        backdrop-filter: blur(10px);
      }
      .panel.open { display: grid; }
      .header {
        position: relative;
        padding: 23px 24px 21px;
        background: linear-gradient(180deg, var(--bg-primary) 0%, var(--bg-primary-deep) 100%);
        border-bottom: 1px solid var(--border-subtle);
      }
      .close {
        position: absolute;
        top: 16px;
        right: 16px;
        width: 40px;
        height: 40px;
        border: 0;
        border-radius: 999px;
        background: linear-gradient(180deg, var(--bg-secondary) 0%, var(--accent-soft) 100%);
        color: var(--text-secondary);
        font: inherit;
        font-size: 26px;
        line-height: 1;
        cursor: pointer;
        display: grid;
        place-items: center;
        box-shadow: inset 0 0 0 1px var(--border-subtle);
      }
      .eyebrow {
        display: none;
      }
      .title {
        margin: 0;
        font-size: 20px;
        font-weight: 700;
        letter-spacing: -0.02em;
        text-align: center;
        color: var(--text-primary);
      }
      .statusbar {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 12px 20px;
        background: rgba(250, 246, 240, 0.72);
        border-bottom: 1px solid var(--border-subtle);
        font-size: 12px;
        font-weight: 500;
        color: var(--text-secondary);
      }
      .statusbar.status-waiting {
        color: #8A6B22;
      }
      .statusbar.status-human {
        color: var(--accent);
      }
      .statusbar.status-closed {
        color: var(--text-secondary);
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: var(--accent);
        box-shadow: 0 0 0 4px rgba(45, 95, 79, 0.12);
        animation: pulse-ai 2.2s ease-in-out infinite;
      }
      .status-waiting .dot {
        background: #D6A844;
        box-shadow: 0 0 0 4px rgba(214, 168, 68, 0.14);
        animation: pulse-waiting 1.4s ease-in-out infinite;
      }
      .status-human .dot {
        background: var(--accent);
        box-shadow: 0 0 0 4px rgba(45, 95, 79, 0.12);
        animation: none;
      }
      .status-closed .dot {
        background: #B9B3A8;
        box-shadow: 0 0 0 4px rgba(185, 179, 168, 0.14);
        animation: none;
      }
      @keyframes pulse-ai {
        0%, 100% {
          box-shadow: 0 0 0 4px rgba(45, 95, 79, 0.1);
        }
        50% {
          box-shadow: 0 0 0 7px rgba(45, 95, 79, 0.04);
        }
      }
      @keyframes pulse-waiting {
        0%, 100% {
          transform: scale(1);
          box-shadow: 0 0 0 4px rgba(214, 168, 68, 0.14);
        }
        50% {
          transform: scale(1.12);
          box-shadow: 0 0 0 7px rgba(214, 168, 68, 0.06);
        }
      }
      .messages {
        padding: 14px 18px;
        overflow: auto;
        display: flex;
        flex-direction: column;
        gap: 12px;
        background: linear-gradient(180deg, var(--bg-secondary) 0%, var(--bg-primary) 100%);
      }
      .empty {
        padding: 16px 18px;
        border-radius: var(--radius-message);
        background: var(--bg-secondary);
        border: 1px dashed var(--border-subtle);
        color: var(--text-secondary);
        font-size: 14px;
        line-height: 1.5;
        box-shadow: var(--shadow-soft);
      }
      .message {
        max-width: 82%;
        padding: 13px 15px;
        border-radius: var(--radius-message);
        line-height: 1.5;
        font-size: 15px;
        font-weight: 400;
        white-space: pre-wrap;
        word-break: break-word;
        box-shadow: var(--shadow-soft);
      }
      .message.user {
        align-self: flex-end;
        background: linear-gradient(180deg, var(--accent) 0%, var(--accent-strong) 100%);
        color: #FFFDF8;
        border: 1px solid rgba(45, 95, 79, 0.24);
        border-bottom-right-radius: 8px;
      }
      .message.assistant,
      .message.system {
        align-self: flex-start;
        background: var(--bg-secondary);
        color: var(--text-primary);
        border: 1px solid var(--border-subtle);
        border-bottom-left-radius: 8px;
        max-width: 68%;
      }
      .message.operator {
        align-self: flex-start;
        background: var(--accent-soft);
        color: var(--accent-strong);
        border: 1px solid rgba(45, 95, 79, 0.14);
        border-bottom-left-radius: 8px;
      }
      .message.typing {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        color: var(--text-secondary);
      }
      .typing-dots {
        display: inline-flex;
        gap: 4px;
      }
      .typing-dots span {
        width: 5px;
        height: 5px;
        border-radius: 999px;
        background: var(--accent);
        animation: typing-pulse 1s ease-in-out infinite;
      }
      .typing-dots span:nth-child(2) {
        animation-delay: 0.15s;
      }
      .typing-dots span:nth-child(3) {
        animation-delay: 0.3s;
      }
      @keyframes typing-pulse {
        0%, 80%, 100% { opacity: 0.35; transform: translateY(0); }
        40% { opacity: 1; transform: translateY(-2px); }
      }
      .badge {
        display: inline-block;
        margin-bottom: 5px;
        padding: 2px 7px;
        border-radius: 999px;
        background: var(--accent-soft);
        color: var(--accent);
        font-size: 9px;
        text-transform: uppercase;
        letter-spacing: 0.12em;
      }
      .message.system .badge {
        background: var(--bg-primary);
        color: var(--text-secondary);
      }
      .quick-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: -4px;
      }
      .quick-action {
        border: 1px solid rgba(45, 95, 79, 0.34);
        border-radius: 999px;
        background: var(--bg-secondary);
        color: var(--accent);
        cursor: pointer;
        font: inherit;
        font-weight: 600;
        font-size: 14px;
        padding: 8px 11px;
        transition: background 160ms ease, border-color 160ms ease, transform 160ms ease;
      }
      .quick-action:hover {
        background: var(--accent-soft);
        border-color: var(--accent);
        transform: translateY(-1px);
      }
      .composer {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 12px 18px 16px;
        border-top: 1px solid var(--border-subtle);
        background: linear-gradient(180deg, var(--bg-secondary) 0%, var(--bg-primary) 100%);
      }
      .composer.hidden {
        display: none;
      }
      .closed-note {
        display: none;
        padding: 12px 18px 16px;
        border-top: 1px solid var(--border-subtle);
        background: linear-gradient(180deg, var(--bg-secondary) 0%, var(--bg-primary) 100%);
      }
      .closed-note.visible {
        display: block;
      }
      .closed-reset {
        width: 100%;
        min-height: 54px;
        border: 0;
        border-radius: var(--radius-control);
        background: linear-gradient(180deg, var(--accent) 0%, var(--accent-strong) 100%);
        color: #FFFDF8;
        cursor: pointer;
        font: inherit;
        font-size: 13px;
        font-weight: 700;
        box-shadow: var(--shadow-soft);
      }
      .input {
        flex: 1 1 auto;
        box-sizing: border-box;
        height: 48px;
        min-height: 48px;
        max-height: 48px;
        padding: 13px 15px;
        border: 1px solid var(--border-subtle);
        border-radius: var(--radius-control);
        background: var(--bg-secondary);
        color: var(--text-primary);
        font: inherit;
        font-size: 14px;
        resize: none;
        outline: none;
        box-shadow: var(--shadow-soft);
      }
      .input:focus {
        border-color: rgba(45, 95, 79, 0.5);
        box-shadow: 0 0 0 4px rgba(45, 95, 79, 0.11);
      }
      .send {
        box-sizing: border-box;
        min-width: 118px;
        height: 48px;
        align-self: stretch;
        border: 0;
        border-radius: var(--radius-control);
        background: linear-gradient(180deg, var(--accent) 0%, var(--accent-strong) 100%);
        color: #FFFDF8;
        font: inherit;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
        padding: 0 16px;
        box-shadow: var(--shadow-soft);
        transition: transform 160ms ease, box-shadow 160ms ease, opacity 160ms ease;
      }
      .send:hover {
        transform: translateY(-1px);
        box-shadow: 0 8px 18px rgba(45, 95, 79, 0.14);
      }
      .send[disabled], .input[disabled] {
        opacity: 0.6;
        cursor: not-allowed;
      }
      @media (max-width: 640px) {
        .shell { right: 12px; bottom: 12px; }
        .panel { width: calc(100vw - 24px); height: min(78vh, 680px); }
        .composer {
          align-items: stretch;
          flex-direction: column;
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
        </header>
        <div class="statusbar status-ai"><span class="dot"></span><span class="status-text">AI-консультант на связи</span></div>
        <div class="messages"></div>
        <div class="composer">
          <textarea class="input" rows="1" placeholder="Напишите ваш вопрос"></textarea>
          <button class="send" type="button">Отправить</button>
        </div>
        <div class="closed-note">
          <button class="closed-reset" type="button">Начать новый диалог</button>
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
        typingNode: null,
        typingStartedAt: 0,
      };
      this.shadow = this.attachShadow({ mode: "closed" });
      this.shadow.appendChild(template.content.cloneNode(true));
      this.elements = {
        launcher: this.shadow.querySelector(".launcher"),
        panel: this.shadow.querySelector(".panel"),
        messages: this.shadow.querySelector(".messages"),
        input: this.shadow.querySelector(".input"),
        send: this.shadow.querySelector(".send"),
        statusbar: this.shadow.querySelector(".statusbar"),
        statusText: this.shadow.querySelector(".status-text"),
        reset: this.shadow.querySelector(".closed-reset"),
        close: this.shadow.querySelector(".close"),
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
      this.elements.launcher.classList.toggle("hidden", this.state.open);
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

    shouldHideSystemMessage(text) {
      const compact = String(text || "").trim();
      return (
        compact === "Ожидаем подключения специалиста. Ваше сообщение сохранено в истории диалога." ||
        compact === "Специалист подключился к диалогу" ||
        compact === "Диалог завершён. Если остались вопросы — напишите снова."
      );
    }

    isHandoffMessage(text) {
      const compact = String(text || "").trim();
      return compact.startsWith("Передаю диалог специалисту.") || compact.startsWith("Передаю специалисту.");
    }

    handoffMessage() {
      return "Передаю специалисту. Можете добавить детали — оператор увидит историю.";
    }

    renderHistory(messages) {
      this.elements.messages.innerHTML = "";
      if (!messages.length) {
        this.pushEmptyMessage();
        return;
      }
      for (const item of messages) {
        if (item.role === "system" && this.shouldHideSystemMessage(item.text)) {
          continue;
        }
        const isHandoff = item.role === "assistant" && this.isHandoffMessage(item.text);
        this.addMessage(isHandoff ? "system" : item.role, isHandoff ? this.handoffMessage() : item.text, true);
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
        this.scrollToBottom();
      }
    }

    showTyping() {
      this.hideTyping();
      this.clearEmptyState();
      const node = document.createElement("article");
      node.className = "message assistant typing";
      node.setAttribute("aria-label", "AI-консультант печатает");

      const text = document.createElement("span");
      text.textContent = "печатает";
      const dots = document.createElement("span");
      dots.className = "typing-dots";
      for (let index = 0; index < 3; index += 1) {
        dots.appendChild(document.createElement("span"));
      }
      node.appendChild(text);
      node.appendChild(dots);
      this.elements.messages.appendChild(node);
      this.state.typingNode = node;
      this.state.typingStartedAt = Date.now();
      this.scrollToBottom();
    }

    async hideTyping() {
      if (this.state.typingNode) {
        const elapsed = Date.now() - this.state.typingStartedAt;
        if (elapsed < MIN_TYPING_VISIBLE_MS) {
          await wait(MIN_TYPING_VISIBLE_MS - elapsed);
        }
        this.state.typingNode.remove();
        this.state.typingNode = null;
        this.state.typingStartedAt = 0;
      }
    }

    clearQuickActions() {
      this.elements.messages.querySelectorAll(".quick-actions").forEach((node) => node.remove());
    }

    addQuickActions(actions) {
      this.clearQuickActions();
      if (!Array.isArray(actions) || actions.length === 0 || this.state.status === STATUS.CLOSED) {
        return;
      }

      const wrap = document.createElement("div");
      wrap.className = "quick-actions";

      for (const action of actions) {
        const normalizedAction =
          typeof action === "string"
            ? { label: action, type: "message", value: action }
            : action;
        const label = String(normalizedAction?.label || "").trim();
        const type = String(normalizedAction?.type || "message").trim();
        const value = String(normalizedAction?.value || label).trim();
        if (!label || !value) continue;

        const button = document.createElement("button");
        button.className = "quick-action";
        button.type = "button";
        button.textContent = label;
        button.addEventListener("click", () => this.handleQuickAction({ type, value }));
        wrap.appendChild(button);
      }

      if (!wrap.childElementCount) return;
      this.elements.messages.appendChild(wrap);
      this.scrollToBottom();
    }

    handleQuickAction(action) {
      if (action.type === "link") {
        window.open(action.value, "_blank", "noopener,noreferrer");
        return;
      }

      this.sendText(String(action.value || "").trim());
    }

    async sendText(text) {
      if (!text || this.state.sending || this.state.status === STATUS.CLOSED) return;

      this.elements.input.value = "";
      this.clearQuickActions();
      this.addMessage("user", text);

      if (this.state.status === STATUS.HUMAN_ACTIVE && this.state.ws) {
        this.state.ws.send(text);
        return;
      }

      this.state.sending = true;
      this.elements.send.disabled = true;
      this.showTyping();
      await nextFrame();

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
        await this.hideTyping();
        if (payload.session_id) {
          this.state.sessionId = payload.session_id;
          window.localStorage.setItem(STORAGE_KEY, payload.session_id);
        }
        this.applyState(payload.status);
        if (payload.answer) {
          const isHandoff = payload.status === STATUS.WAITING_OPERATOR && this.isHandoffMessage(payload.answer);
          const role =
            isHandoff || payload.status === STATUS.HUMAN_ACTIVE || payload.action === "reject" ? "system" : "assistant";
          this.addMessage(role, isHandoff ? this.handoffMessage() : payload.answer);
          this.addQuickActions(payload.quick_actions);
        }
        if (payload.status === STATUS.WAITING_OPERATOR || payload.status === STATUS.HUMAN_ACTIVE) {
          this.connectWebSocket();
        }
      } catch (error) {
        await this.hideTyping();
        this.addMessage("system", "Не удалось отправить сообщение. Попробуйте ещё раз.");
      } finally {
        this.state.sending = false;
        this.elements.send.disabled = this.state.status === STATUS.CLOSED;
      }
    }

    applyState(status) {
      this.state.status = status;
      const labels = {
        AI_ACTIVE: "AI-консультант на связи",
        WAITING_OPERATOR: "Ожидаем специалиста",
        HUMAN_ACTIVE: "Специалист в чате",
        CLOSED: "Диалог завершён",
      };
      const placeholders = {
        AI_ACTIVE: "Напишите ваш вопрос",
        WAITING_OPERATOR: "Добавьте детали, оператор их увидит...",
        HUMAN_ACTIVE: "Напишите ваш вопрос",
        CLOSED: "Диалог завершён",
      };
      const statusClasses = {
        AI_ACTIVE: "status-ai",
        WAITING_OPERATOR: "status-waiting",
        HUMAN_ACTIVE: "status-human",
        CLOSED: "status-closed",
      };

      this.elements.statusText.textContent = labels[status] || labels.AI_ACTIVE;
      this.elements.statusbar.classList.remove("status-ai", "status-waiting", "status-human", "status-closed");
      this.elements.statusbar.classList.add(statusClasses[status] || statusClasses.AI_ACTIVE);
      this.elements.composer.classList.toggle("hidden", status === STATUS.CLOSED);
      this.elements.closedNote.classList.toggle("visible", status === STATUS.CLOSED);
      this.elements.input.placeholder = placeholders[status] || placeholders.AI_ACTIVE;
      this.elements.input.disabled = status === STATUS.CLOSED;
      this.elements.send.disabled = status === STATUS.CLOSED;
    }

    async handleSubmit() {
      const text = this.elements.input.value.trim();
      this.sendText(text);
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
      this.clearQuickActions();
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

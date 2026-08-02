(function () {
  const SCRIPT = document.currentScript;
  const EMBED_COMPANY_ID = (SCRIPT && SCRIPT.dataset.companyId) || "";
  const DEMO_EXPAND_ENABLED = Boolean(SCRIPT && SCRIPT.dataset.demoExpand === "true");
  const API_BASE =
    (SCRIPT && SCRIPT.dataset.apiBase) ||
    (SCRIPT && new URL(SCRIPT.src, window.location.href).origin) ||
    window.location.origin;

  const STATUS = {
    AI_ACTIVE: "AI_ACTIVE",
    WAITING_OPERATOR: "WAITING_OPERATOR",
    HUMAN_ACTIVE: "HUMAN_ACTIVE",
    CLOSED: "CLOSED",
    UNAVAILABLE: "UNAVAILABLE",
  };
  const MIN_TYPING_VISIBLE_MS = 450;
  const SEND_TIMEOUT_MS = 30000;

  const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  const nextFrame = () => new Promise((resolve) => window.requestAnimationFrame(resolve));

  const URL_RE = /\[?(https?:\/\/[^\s\]]+)\]?/g;
  function renderMessageBody(container, text) {
    const str = String(text == null ? "" : text);
    let lastIndex = 0;
    let match;
    let hasLink = false;
    URL_RE.lastIndex = 0;
    while ((match = URL_RE.exec(str)) !== null) {
      hasLink = true;
      const before = str.slice(lastIndex, match.index);
      if (before) container.appendChild(document.createTextNode(before));

      let url = match[1];
      const trailing = url.match(/[.,;:!?)]+$/);
      let trailingPunct = "";
      if (trailing) {
        trailingPunct = trailing[0];
        url = url.slice(0, -trailingPunct.length);
      }

      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = url;
      container.appendChild(a);
      if (trailingPunct) container.appendChild(document.createTextNode(trailingPunct));

      lastIndex = URL_RE.lastIndex;
    }
    const rest = str.slice(lastIndex);
    if (rest) container.appendChild(document.createTextNode(rest));
    if (!hasLink) container.textContent = str;
  }

  const template = document.createElement("template");
  template.innerHTML = `
    <style>
      @import url("https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap");

      :host {
        all: initial;
        --accent: #1F7A5C;
        --accent-dark: #0f4d38;
        --accent-soft: #e8f5f0;
        --accent-border: #d1ede4;
        --bg: #ffffff;
        --bg-page: #fafaf8;
        --bg-warm: #f5f3ef;
        --text: #111111;
        --text-secondary: #6b7280;
        --text-muted: #9ca3af;
        --border: #ede9e3;
        --border-soft: #f0ede8;
        --shadow: 0 24px 70px rgba(0,0,0,.14), 0 6px 20px rgba(0,0,0,.07);
        --radius: 28px;
        --radius-sm: 16px;
        --radius-msg: 20px;
        font-family: "Manrope", system-ui, -apple-system, sans-serif;
      }

      *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

      .shell {
        position: fixed;
        right: 24px;
        bottom: 24px;
        z-index: 2147483647;
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 12px;
      }
      .shell.pos-left {
        right: auto;
        left: 24px;
        align-items: flex-start;
      }

      /* ── Launcher ── */
      .launcher {
        width: 58px;
        height: 58px;
        border: 0;
        border-radius: 50%;
        background: linear-gradient(160deg, #2a9c7d 0%, var(--accent) 42%, var(--accent-dark) 100%);
        color: #fff;
        font-family: inherit;
        cursor: pointer;
        box-shadow: var(--shadow), inset 0 1.5px 0 rgba(255,255,255,.35);
        display: grid;
        place-items: center;
        transition: transform .18s ease, box-shadow .18s ease;
        position: relative;
      }
      .launcher svg { width: 26px; height: 26px; }
      .launcher:hover { transform: translateY(-2px); box-shadow: 0 24px 64px rgba(0,0,0,.16), inset 0 1.5px 0 rgba(255,255,255,.4); }
      .launcher.hidden { display: none; }

      /* ── Unread badge ── */
      .unread {
        position: absolute;
        top: -4px;
        right: -4px;
        width: 18px;
        height: 18px;
        border-radius: 999px;
        background: #ef4444;
        border: 2px solid #fff;
        display: none;
      }
      .unread.visible { display: block; }

      /* ── Panel ── */
      .panel {
        width: min(400px, calc(100vw - 32px));
        height: min(660px, calc(100vh - 100px));
        display: none;
        flex-direction: column;
        border-radius: var(--radius);
        background: var(--bg);
        box-shadow: var(--shadow);
        overflow: hidden;
        border: 1px solid var(--border-soft);
      }
      .panel.open { display: flex; }
      .panel.demo-big {
        width: min(760px, calc(100vw - 32px));
        height: min(940px, calc(100vh - 24px));
      }

      /* ── Header ── */
      .header {
        padding: 16px 18px;
        display: flex;
        align-items: center;
        gap: 12px;
        border-bottom: 1px solid var(--border-soft);
        background: linear-gradient(180deg, var(--accent-soft) 0%, var(--bg) 100%);
        flex-shrink: 0;
      }
      .avatar {
        width: 42px;
        height: 42px;
        border-radius: var(--radius-sm);
        background: linear-gradient(145deg, var(--accent) 0%, var(--accent-dark) 100%);
        display: flex;
        align-items: center;
        justify-content: center;
        color: #fff;
        flex-shrink: 0;
        box-shadow: 0 4px 12px rgba(31,122,92,.25);
      }
      .avatar svg { width: 20px; height: 20px; }
      .header-info { flex: 1; min-width: 0; }
      .header-name {
        font-size: 15px;
        font-weight: 700;
        color: var(--text);
        letter-spacing: -.02em;
        line-height: 1.2;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .header-status {
        display: flex;
        align-items: center;
        gap: 5px;
        margin-top: 2px;
      }
      .dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: #22c55e;
        flex-shrink: 0;
        transition: background .3s;
      }
      .dot.waiting {
        background: #f59e0b;
        animation: blink-dot 1.2s ease-in-out infinite;
      }
      .dot.human { background: #22c55e; animation: none; }
      .dot.closed, .dot.unavailable { background: #d1d5db; animation: none; }
      @keyframes blink-dot {
        0%, 100% { opacity: 1; }
        50% { opacity: .3; }
      }
      .status-label {
        font-size: 12px;
        font-weight: 600;
        color: var(--text-muted);
        letter-spacing: .01em;
      }
      .close-btn {
        width: 32px;
        height: 32px;
        border-radius: 10px;
        background: var(--bg-warm);
        border: 0;
        color: var(--text-muted);
        font: inherit;
        cursor: pointer;
        display: grid;
        place-items: center;
        flex-shrink: 0;
        transition: background .15s, color .15s;
        line-height: 1;
      }
      .close-btn svg { width: 16px; height: 16px; }
      .close-btn:hover { background: var(--border); color: var(--text); }

      /* ── AI badge strip ── */
      .ai-strip {
        padding: 7px 16px;
        border-bottom: 1px solid var(--border-soft);
        display: flex;
        justify-content: center;
        background: var(--bg);
        flex-shrink: 0;
      }
      .ai-strip-inner {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        background: var(--accent-soft);
        border-radius: 999px;
        padding: 3px 10px;
        font-size: 12px;
        font-weight: 700;
        color: var(--accent-dark);
        letter-spacing: .02em;
      }

      /* ── Messages ── */
      .messages {
        flex: 1;
        overflow-y: auto;
        padding: 14px 14px;
        display: flex;
        flex-direction: column;
        gap: 10px;
        background: var(--bg-page);
        scroll-behavior: smooth;
      }
      .messages::-webkit-scrollbar { width: 4px; }
      .messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

      /* ── Empty / special states ── */
      .empty-state {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 24px;
        text-align: center;
        color: var(--text-secondary);
        font-size: 14px;
        line-height: 1.55;
      }
      .empty-icon {
        width: 64px;
        height: 64px;
        border-radius: 20px;
        background: linear-gradient(145deg, var(--accent-soft), var(--accent-border));
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 28px;
      }
      .empty-title {
        font-size: 16px;
        font-weight: 800;
        color: var(--text);
        letter-spacing: -.02em;
      }
      .empty-sub {
        font-size: 13px;
        color: var(--text-muted);
        max-width: 240px;
      }

      /* ── Message bubbles ── */
      .msg {
        max-width: 82%;
        padding: 10px 13px;
        border-radius: var(--radius-msg);
        font-size: 14px;
        line-height: 1.55;
        word-break: break-word;
        white-space: pre-wrap;
      }
      .msg.user {
        align-self: flex-end;
        background: linear-gradient(160deg, #2a9c7d 0%, var(--accent) 60%, var(--accent-dark) 100%);
        color: #fff;
        border-bottom-right-radius: 7px;
      }
      .msg.assistant {
        align-self: flex-start;
        background: var(--bg);
        color: var(--text);
        border: 1px solid var(--border);
        border-bottom-left-radius: 7px;
        box-shadow: 0 1px 2px rgba(17,17,17,.03);
        max-width: 86%;
      }
      .msg.operator {
        align-self: flex-start;
        background: var(--accent-soft);
        color: var(--accent-dark);
        border: 1px solid var(--accent-border);
        border-bottom-left-radius: 7px;
      }
      .msg.system {
        align-self: center;
        background: var(--bg-warm);
        color: var(--text-muted);
        border: 1px solid var(--border-soft);
        border-radius: 10px;
        font-size: 12px;
        font-weight: 600;
        max-width: 90%;
        text-align: center;
        padding: 7px 12px;
      }
      .msg a {
        color: inherit;
        text-decoration: underline;
        text-decoration-color: currentColor;
        opacity: 0.85;
      }
      .msg a:hover,
      .msg a:focus-visible {
        opacity: 1;
      }
      .msg-label {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: .04em;
        text-transform: uppercase;
        opacity: .55;
        margin-bottom: 4px;
      }
      .msg.assistant .msg-label { color: var(--accent-dark); }
      .msg.operator .msg-label { color: var(--accent-dark); }

      /* ── Typing indicator ── */
      .typing-bubble {
        align-self: flex-start;
        background: var(--bg);
        border: 1px solid var(--border);
        border-radius: var(--radius-msg);
        border-bottom-left-radius: 5px;
        padding: 12px 16px;
        display: flex;
        align-items: center;
        gap: 5px;
      }
      .typing-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--accent);
        animation: typing-bounce 1s ease-in-out infinite;
      }
      .typing-dot:nth-child(2) { animation-delay: .15s; }
      .typing-dot:nth-child(3) { animation-delay: .3s; }
      @keyframes typing-bounce {
        0%, 80%, 100% { transform: translateY(0); opacity: .4; }
        40% { transform: translateY(-4px); opacity: 1; }
      }

      /* ── Quick actions ── */
      .quick-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        padding: 0 0 2px;
      }
      .quick-btn {
        background: var(--bg);
        border: 1px solid var(--accent-border);
        border-radius: 999px;
        color: var(--accent);
        font: inherit;
        font-size: 13px;
        font-weight: 700;
        padding: 6px 12px;
        cursor: pointer;
        transition: background .15s, transform .12s;
        letter-spacing: -.01em;
        display: inline-flex;
        align-items: center;
        gap: 0;
      }
      .quick-btn:hover {
        background: var(--accent-soft);
        transform: translateY(-1px);
      }
      /* первая кнопка в группе — самый вероятный next-step, выделяем заливкой;
         позиционно, не по тексту лейбла — лейблы приходят из phrasebook и могут
         отличаться у клиента/со временем, привязка к конкретной строке хрупкая. */
      .quick-btn.primary {
        background: var(--accent);
        border-color: var(--accent);
        color: #fff;
      }
      .quick-btn.primary:hover { background: var(--accent-dark); }
      .quick-btn.primary::after {
        content: "→";
        display: inline-block;
        max-width: 0;
        opacity: 0;
        overflow: hidden;
        transition: max-width .18s ease, opacity .18s ease, margin .18s ease;
      }
      .quick-btn.primary:hover::after,
      .quick-btn.primary:focus-visible::after {
        max-width: 16px;
        opacity: 1;
        margin-left: 5px;
      }

      /* ── Waiting / closed overlay ── */
      .overlay-state {
        display: none;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        flex: 1;
        padding: 24px;
        text-align: center;
        background: var(--bg-page);
      }
      .overlay-state.visible { display: flex; }
      .overlay-icon {
        width: 64px;
        height: 64px;
        border-radius: 20px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 28px;
      }
      .overlay-icon.waiting { background: linear-gradient(145deg, #fef3ee, #fde0d0); }
      .overlay-icon.closed { background: var(--bg-warm); }
      .overlay-title {
        font-size: 16px;
        font-weight: 800;
        color: var(--text);
        letter-spacing: -.02em;
      }
      .overlay-sub {
        font-size: 13px;
        color: var(--text-muted);
        max-width: 230px;
        line-height: 1.5;
      }

      /* ── Composer ── */
      .composer {
        padding: 10px 12px 14px;
        display: flex;
        align-items: center;
        gap: 8px;
        border-top: 1px solid var(--border-soft);
        background: var(--bg);
        flex-shrink: 0;
      }
      .composer.hidden { display: none; }

      .input-wrap {
        position: relative;
        flex: 1;
        display: flex;
        align-items: center;
        min-width: 0;
      }

      .inp {
        flex: 1;
        width: 100%;
        height: 44px;
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
        padding: 0 14px;
        font: inherit;
        font-size: 14px;
        color: var(--text);
        background: var(--bg-page);
        outline: none;
        transition: border-color .15s, box-shadow .15s;
        resize: none;
      }
      .inp.has-mic { padding-right: 40px; }
      .inp::placeholder { color: var(--text-muted); }
      .inp:focus {
        border-color: var(--accent-border);
        box-shadow: 0 0 0 3px rgba(31,122,92,.10);
      }
      .inp:disabled { opacity: .55; cursor: not-allowed; }

      .voice-hint {
        padding: 0 12px 6px;
        font-size: 12px;
        line-height: 1.4;
        color: var(--text-muted);
      }
      .voice-hint.hidden { display: none; }

      .send-btn {
        height: 44px;
        padding: 0 16px;
        border: 0;
        border-radius: var(--radius-sm);
        background: linear-gradient(160deg, #2a9c7d 0%, var(--accent) 42%, var(--accent-dark) 100%);
        color: #fff;
        font: inherit;
        font-size: 14px;
        font-weight: 700;
        cursor: pointer;
        white-space: nowrap;
        transition: transform .15s, box-shadow .15s, opacity .15s;
        letter-spacing: -.01em;
        box-shadow: 0 4px 12px rgba(31,122,92,.25), inset 0 1px 0 rgba(255,255,255,.25);
      }
      .send-btn:hover { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(31,122,92,.3), inset 0 1px 0 rgba(255,255,255,.3); }
      .send-btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }

      .mic-btn {
        position: absolute;
        right: 4px;
        top: 50%;
        transform: translateY(-50%);
        height: 32px;
        width: 32px;
        flex-shrink: 0;
        border: 0;
        border-radius: 50%;
        background: transparent;
        color: var(--text-muted);
        padding: 0;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: background .15s, color .15s, opacity .15s;
      }
      .mic-btn svg { width: 16px; height: 16px; }
      .mic-btn:hover { background: var(--accent-soft); color: var(--accent); }
      .mic-btn.listening {
        background: var(--accent-soft);
        color: var(--accent-dark);
        animation: mic-pulse 1.2s ease-in-out infinite;
      }
      .mic-btn:disabled { opacity: .4; cursor: not-allowed; }
      @keyframes mic-pulse {
        0%, 100% { box-shadow: 0 0 0 0 rgba(31,122,92,.25); }
        50% { box-shadow: 0 0 0 5px rgba(31,122,92,0); }
      }

      /* ── Closed reset ── */
      .closed-note {
        display: none;
        padding: 10px 12px 14px;
        border-top: 1px solid var(--border-soft);
        background: var(--bg);
      }
      .closed-note.visible { display: block; }
      .reset-btn {
        width: 100%;
        height: 44px;
        border: 0;
        border-radius: var(--radius-sm);
        background: var(--bg-warm);
        border: 1px solid var(--border);
        color: var(--text-secondary);
        font: inherit;
        font-size: 14px;
        font-weight: 700;
        cursor: pointer;
        transition: background .15s;
        letter-spacing: -.01em;
      }
      .reset-btn:hover { background: var(--border); }

      /* ── Mobile ── */
      @media (max-width: 480px) {
        .shell { right: 12px; bottom: 12px; }
        .shell.pos-left { left: 12px; right: auto; }
        .panel {
          width: calc(100vw - 24px);
          height: min(78vh, 600px);
          border-radius: 18px;
        }
        .composer { flex-direction: column; align-items: stretch; }
        .input-wrap { width: 100%; }
        .send-btn { width: 100%; }
      }
    </style>

    <div class="shell">
      <button class="launcher" type="button" aria-label="Открыть чат">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"></path>
        </svg>
        <span class="unread" aria-hidden="true"></span>
      </button>

      <section class="panel" aria-live="polite">
        <header class="header">
          <div class="avatar">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"></path>
              <path d="m9 12 2 2 4-4"></path>
            </svg>
          </div>
          <div class="header-info">
            <div class="header-name">AI-консультант</div>
            <div class="header-status">
              <span class="dot"></span>
              <span class="status-label">на связи</span>
            </div>
          </div>
          <button class="close-btn" type="button" aria-label="Закрыть">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M18 6 6 18"></path>
              <path d="m6 6 12 12"></path>
            </svg>
          </button>
        </header>

        <div class="ai-strip">
          <div class="ai-strip-inner">✦ Отвечаем с ИИ</div>
        </div>

        <div class="messages"></div>

        <div class="voice-hint hidden"></div>
        <div class="composer">
          <div class="input-wrap">
            <input class="inp" type="text" placeholder="Напишите вопрос…" />
            <button class="mic-btn" type="button" aria-label="Голосовой ввод" hidden>
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                <line x1="12" y1="19" x2="12" y2="23"></line>
                <line x1="8" y1="23" x2="16" y2="23"></line>
              </svg>
            </button>
          </div>
          <button class="send-btn" type="button">Отправить</button>
        </div>
        <div class="closed-note">
          <button class="reset-btn" type="button">Начать новый диалог</button>
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
        companyId: "",
        sessionId: "",
        ws: null,
        typingNode: null,
        typingStartedAt: 0,
        voiceEnabled: false,
        recognition: null,
        widgetConfig: {
          primary_color: "#1F7A5C",
          button_color: "#1F7A5C",
          header_title: "AI-консультант",
          header_subtitle: "Запись, цены и услуги",
          position: "bottom-right",
          avatar_emoji: "👩‍⚕️",
        },
      };
      this.shadow = this.attachShadow({ mode: "closed" });
      this.shadow.appendChild(template.content.cloneNode(true));
      this.$ = (sel) => this.shadow.querySelector(sel);
      this.el = {
        shell: this.$(".shell"),
        launcher: this.$(".launcher"),
        unread: this.$(".unread"),
        panel: this.$(".panel"),
        headerName: this.$(".header-name"),
        dot: this.$(".dot"),
        statusLabel: this.$(".status-label"),
        close: this.$(".close-btn"),
        messages: this.$(".messages"),
        inp: this.$(".inp"),
        mic: this.$(".mic-btn"),
        voiceHint: this.$(".voice-hint"),
        send: this.$(".send-btn"),
        composer: this.$(".composer"),
        closedNote: this.$(".closed-note"),
        reset: this.$(".reset-btn"),
      };
    }

    storageKey() {
      return "ai-widget-sid:" + this.state.companyId;
    }

    connectedCallback() {
      this.bindEvents();
      if (DEMO_EXPAND_ENABLED) this.el.panel.classList.add("demo-big");
      this.pushGreeting();
      this.bootstrap();
    }

    bindEvents() {
      this.el.launcher.addEventListener("click", () => this.toggle());
      this.el.close.addEventListener("click", () => this.toggle());
      this.el.send.addEventListener("click", () => this.submit());
      this.el.reset.addEventListener("click", () => this.startNew());
      this.el.inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.submit(); }
      });
    }

    pushGreeting() {
      if (this.el.messages.childElementCount > 0) return;
      const d = document.createElement("div");
      d.className = "empty-state";
      d.innerHTML = `
        <div class="empty-icon">💬</div>
        <div class="empty-title">Чем могу помочь?</div>
        <div class="empty-sub">Спросите об услугах, ценах или запишитесь на приём</div>
      `;
      this.el.messages.appendChild(d);
    }

    clearGreeting() {
      const e = this.el.messages.querySelector(".empty-state");
      if (e) e.remove();
    }

    showGreeting() {
      if (this.el.messages.querySelector(".msg")) return;
      if (this.state.greetingText) {
        this.addMsg("assistant", this.state.greetingText, true);
      } else {
        this.pushGreeting();
      }
    }

    async bootstrap() {
      try {
        const url = new URL(API_BASE + "/api/widget/bootstrap");
        if (EMBED_COMPANY_ID) url.searchParams.set("company_id", EMBED_COMPANY_ID);
        const res = await fetch(url.toString());
        if (!res.ok) throw await this.buildBootstrapError(res);
        const data = await res.json();
        this.state.companyId = data.company_id || EMBED_COMPANY_ID;
        if (!this.state.companyId) throw new Error("company not resolved");
        this.applyConfig(data.widget_config || {});
        this.state.greetingText = typeof data.greeting === "string" ? data.greeting.trim() : "";
        const voiceFeatureEnabled = Boolean(data.features && data.features.voice_input !== false);
        this.state.voiceEnabled = voiceFeatureEnabled && this.setupVoiceInput();
        await this.restoreSession();
      } catch (err) {
        this.markUnavailable(err);
      }
    }

    setupVoiceInput() {
      if (!this.el.mic) return false;
      const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognitionImpl) return false;
      this.SpeechRecognitionImpl = SpeechRecognitionImpl;
      this.el.mic.hidden = false;
      this.el.inp.classList.add("has-mic");
      this.el.mic.addEventListener("click", () => this.toggleVoiceInput());
      return true;
    }

    toggleVoiceInput() {
      if (this.state.recognition) {
        this.state.recognition.stop();
        return;
      }
      this.startVoiceInput();
    }

    startVoiceInput() {
      if (!this.SpeechRecognitionImpl || !this.el.mic || this.el.inp.disabled) return;
      if (this.el.voiceHint) {
        clearTimeout(this._voiceHintTimer);
        this.el.voiceHint.classList.add("hidden");
      }
      const recognition = new this.SpeechRecognitionImpl();
      recognition.lang = "ru-RU";
      recognition.interimResults = false;
      recognition.maxAlternatives = 1;

      recognition.onstart = () => {
        this.state.recognition = recognition;
        this.el.mic.classList.add("listening");
      };
      recognition.onresult = (event) => {
        const transcript = event.results && event.results[0] && event.results[0][0]
          ? event.results[0][0].transcript
          : "";
        const text = String(transcript || "").trim();
        if (text) {
          this.el.inp.value = (this.el.inp.value ? this.el.inp.value + " " : "") + text;
          this.el.inp.focus();
        }
      };
      recognition.onerror = (event) => {
        this.handleVoiceError(event.error);
      };
      recognition.onend = () => {
        this.state.recognition = null;
        this.el.mic.classList.remove("listening");
      };

      try {
        recognition.start();
      } catch (_) {
        this.state.recognition = null;
        this.el.mic.classList.remove("listening");
      }
    }

    handleVoiceError(errorCode) {
      this.state.recognition = null;
      if (this.el.mic) this.el.mic.classList.remove("listening");
      const messages = {
        "not-allowed": "Доступ к микрофону запрещён в браузере.",
        "no-speech": "Не расслышал, попробуйте ещё раз.",
        "audio-capture": "Микрофон не найден.",
      };
      const text = messages[errorCode];
      if (text) this.showVoiceHint(text);
    }

    showVoiceHint(text) {
      if (!this.el.voiceHint) return;
      clearTimeout(this._voiceHintTimer);
      this.el.voiceHint.textContent = text;
      this.el.voiceHint.classList.remove("hidden");
      this._voiceHintTimer = setTimeout(() => {
        this.el.voiceHint.classList.add("hidden");
      }, 3000);
    }

    async buildBootstrapError(res) {
      let detail = "";
      try { const d = await res.json(); detail = String(d.detail || d.error || ""); } catch (_) {}
      const e = new Error(detail || "bootstrap failed");
      e.status = res.status; e.detail = detail;
      return e;
    }

    bootstrapMsg(err) {
      const s = Number(err?.status || 0);
      if (s === 403) return "Виджет недоступен: домен не разрешён.";
      if (s === 404) return "Виджет недоступен: клиент не найден.";
      if (s === 409) return "Ошибка конфигурации: конфликт доменов.";
      if (s >= 500) return "Сервис временно недоступен. Попробуйте позже.";
      return "Виджет не запустился. Проверьте подключение.";
    }

    applyConfig(cfg) {
      const c = this.state.widgetConfig;
      const merge = (key) => { const v = String(cfg[key] || "").trim(); if (v) c[key] = v; };
      Object.keys(c).forEach(merge);
      if (!["bottom-right","bottom-left"].includes(c.position)) c.position = "bottom-right";
      this.state.widgetConfig = c;

      const root = this.shadow.host;
      root.style.setProperty("--accent", c.primary_color);
      root.style.setProperty("--accent-dark", this.darken(c.primary_color));

      this.el.headerName.textContent = c.header_title;
      this.el.shell.classList.toggle("pos-left", c.position === "bottom-left");
    }

    darken(hex) {
      try {
        const n = parseInt(hex.replace("#",""), 16);
        const r = Math.max(0, (n>>16) - 40);
        const g = Math.max(0, ((n>>8)&0xff) - 40);
        const b = Math.max(0, (n&0xff) - 30);
        return `#${r.toString(16).padStart(2,"0")}${g.toString(16).padStart(2,"0")}${b.toString(16).padStart(2,"0")}`;
      } catch (_) { return hex; }
    }

    async restoreSession() {
      if (!this.state.companyId) { this.setStatus(STATUS.UNAVAILABLE); return; }
      this.state.sessionId = window.localStorage.getItem(this.storageKey()) || "";
      if (!this.state.sessionId) { this.setStatus(STATUS.AI_ACTIVE); this.showGreeting(); return; }
      try {
        const res = await fetch(API_BASE + "/api/chat/session/" + this.state.sessionId);
        if (!res.ok) { this.clearLocalSession(); this.setStatus(STATUS.AI_ACTIVE); this.showGreeting(); return; }
        const data = await res.json();
        if (data.company_id !== this.state.companyId) { this.clearLocalSession(); this.setStatus(STATUS.AI_ACTIVE); this.showGreeting(); return; }
        this.renderHistory(data.messages || []);
        this.setStatus(data.status);
        if (data.status === STATUS.HUMAN_ACTIVE) this.connectWS();
      } catch (_) { this.setStatus(STATUS.AI_ACTIVE); this.showGreeting(); }
    }

    renderHistory(msgs) {
      this.el.messages.innerHTML = "";
      if (!msgs.length) { this.showGreeting(); return; }
      for (const m of msgs) this.addMsg(m.role, m.text, true);
      this.scrollBottom();
    }

    toggle() {
      this.state.open = !this.state.open;
      this.el.panel.classList.toggle("open", this.state.open);
      this.el.launcher.classList.toggle("hidden", this.state.open);
      this.el.unread.classList.remove("visible");
      if (this.state.open) { this.scrollBottom(); this.el.inp.focus(); }
    }

    addMsg(role, text, silent) {
      this.clearGreeting();
      this.el.messages.querySelectorAll(".quick-actions").forEach(n => n.remove());

      const isSystem = role === "system";
      const isHandoff = (role === "assistant" || isSystem) && (
        String(text).startsWith("Передаю") || String(text).startsWith("Ожидаем")
      );

      const article = document.createElement("article");
      article.className = "msg " + (isHandoff ? "system" : role);

      if (!isSystem && !isHandoff) {
        const label = document.createElement("div");
        label.className = "msg-label";
        const cfg = this.state.widgetConfig;
        if (role === "assistant") label.textContent = cfg.avatar_emoji + " AI";
        else if (role === "operator") label.textContent = "Специалист";
        else if (role === "user") label.textContent = "";
        if (label.textContent) article.appendChild(label);
      }

      const body = document.createElement("div");
      renderMessageBody(body, text);
      article.appendChild(body);
      this.el.messages.appendChild(article);
      if (!silent) this.scrollBottom();

      if (!this.state.open) this.el.unread.classList.add("visible");
    }

    addQuickActions(actions) {
      this.el.messages.querySelectorAll(".quick-actions").forEach(n => n.remove());
      if (!Array.isArray(actions) || !actions.length) return;
      if ([STATUS.CLOSED, STATUS.UNAVAILABLE].includes(this.state.status)) return;

      const wrap = document.createElement("div");
      wrap.className = "quick-actions";
      for (const a of actions) {
        const norm = typeof a === "string" ? {label:a,type:"message",value:a} : a;
        if (!norm?.label?.trim()) continue;
        const btn = document.createElement("button");
        // первая кнопка группы — самый вероятный next-step, визуально выделяем;
        // позиция в массиве, а не текст лейбла (тот приходит из phrasebook и
        // может отличаться у клиента).
        btn.className = "quick-btn" + (wrap.childElementCount === 0 ? " primary" : "");
        btn.type = "button";
        btn.textContent = norm.label;
        btn.addEventListener("click", () => {
          if (norm.type === "link") { window.open(norm.value, "_blank", "noopener,noreferrer"); return; }
          this.sendText(norm.value);
        });
        wrap.appendChild(btn);
      }
      if (wrap.childElementCount) { this.el.messages.appendChild(wrap); this.scrollBottom(); }
    }

    showTyping() {
      this.hideTyping();
      this.clearGreeting();
      const b = document.createElement("div");
      b.className = "typing-bubble";
      b.setAttribute("aria-label", "AI печатает");
      for (let i = 0; i < 3; i++) {
        const d = document.createElement("div");
        d.className = "typing-dot";
        b.appendChild(d);
      }
      this.el.messages.appendChild(b);
      this.state.typingNode = b;
      this.state.typingStartedAt = Date.now();
      this.scrollBottom();
    }

    async hideTyping() {
      if (this.state.typingNode) {
        const elapsed = Date.now() - this.state.typingStartedAt;
        if (elapsed < MIN_TYPING_VISIBLE_MS) await wait(MIN_TYPING_VISIBLE_MS - elapsed);
        this.state.typingNode.remove();
        this.state.typingNode = null;
      }
    }

    setStatus(status) {
      this.state.status = status;
      const dotClass = { AI_ACTIVE:"", WAITING_OPERATOR:"waiting", HUMAN_ACTIVE:"human", CLOSED:"closed", UNAVAILABLE:"unavailable" };
      const labels = {
        AI_ACTIVE: "на связи",
        WAITING_OPERATOR: "ожидаем специалиста",
        HUMAN_ACTIVE: "специалист в чате",
        CLOSED: "диалог завершён",
        UNAVAILABLE: "недоступен",
      };
      const placeholders = {
        AI_ACTIVE: "Напишите вопрос…",
        WAITING_OPERATOR: "Добавьте детали…",
        HUMAN_ACTIVE: "Напишите…",
        CLOSED: "",
        UNAVAILABLE: "",
      };

      this.el.dot.className = "dot " + (dotClass[status] || "");
      this.el.statusLabel.textContent = labels[status] || labels.AI_ACTIVE;
      this.el.inp.placeholder = placeholders[status] || "";

      const isClosed = status === STATUS.CLOSED;
      const isUnavail = status === STATUS.UNAVAILABLE;
      this.el.composer.classList.toggle("hidden", isClosed || isUnavail);
      this.el.closedNote.classList.toggle("visible", isClosed);
      this.el.inp.disabled = isClosed || isUnavail;
      this.el.send.disabled = isClosed || isUnavail;
      if (this.el.mic) this.el.mic.disabled = isClosed || isUnavail;
    }

    async submit() {
      const text = this.el.inp.value.trim();
      if (!text) return;
      if (this.state.recognition) this.state.recognition.stop();
      this.sendText(text);
    }

    async sendText(text) {
      if (!text || this.state.sending || !this.state.companyId) return;
      if ([STATUS.CLOSED, STATUS.UNAVAILABLE].includes(this.state.status)) return;

      this.el.inp.value = "";
      this.addMsg("user", text);

      if (this.state.status === STATUS.HUMAN_ACTIVE && this.state.ws) {
        this.state.ws.send(text);
        return;
      }

      this.state.sending = true;
      this.el.send.disabled = true;
      this.showTyping();
      await nextFrame();

      const timeoutController = new AbortController();
      const timeoutId = window.setTimeout(() => timeoutController.abort(), SEND_TIMEOUT_MS);
      try {
        const res = await fetch(API_BASE + "/api/chat/message", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: this.state.sessionId || null,
            company_id: this.state.companyId,
            message: text,
          }),
          signal: timeoutController.signal,
        });
        if (!res.ok) throw new Error("request failed");
        const data = await res.json();
        await this.hideTyping();

        if (data.session_id) {
          this.state.sessionId = data.session_id;
          window.localStorage.setItem(this.storageKey(), data.session_id);
        }
        this.setStatus(data.status);
        if (data.answer) {
          const isHandoff = data.status === STATUS.WAITING_OPERATOR;
          this.addMsg(isHandoff ? "system" : "assistant", data.answer);
        }
        this.addQuickActions(data.quick_actions);
        if ([STATUS.WAITING_OPERATOR, STATUS.HUMAN_ACTIVE].includes(data.status)) this.connectWS();
      } catch (err) {
        await this.hideTyping();
        const isTimeout = err && err.name === "AbortError";
        this.addMsg(
          "system",
          isTimeout
            ? "Сервер отвечает дольше обычного. Попробуйте отправить сообщение ещё раз через минуту."
            : "Не удалось отправить. Попробуйте ещё раз."
        );
      } finally {
        window.clearTimeout(timeoutId);
        this.state.sending = false;
        this.el.send.disabled = [STATUS.CLOSED, STATUS.UNAVAILABLE].includes(this.state.status);
      }
    }

    async startNew() {
      if (this.state.ws) { this.state.ws.close(); this.state.ws = null; }
      this.clearLocalSession();
      this.el.messages.innerHTML = "";
      this.pushGreeting();
      this.setStatus(STATUS.AI_ACTIVE);
    }

    clearLocalSession() {
      if (this.state.companyId) window.localStorage.removeItem(this.storageKey());
      this.state.sessionId = "";
      this.state.status = STATUS.AI_ACTIVE;
    }

    markUnavailable(err) {
      this.clearLocalSession();
      if (this.state.ws) { this.state.ws.close(); this.state.ws = null; }
      this.el.messages.innerHTML = "";
      this.setStatus(STATUS.UNAVAILABLE);

      const d = document.createElement("div");
      d.className = "empty-state";
      d.innerHTML = `
        <div class="empty-icon" style="background:linear-gradient(145deg,#fef2f2,#fee2e2)">🔌</div>
        <div class="empty-title">Виджет недоступен</div>
        <div class="empty-sub">${this.bootstrapMsg(err)}</div>
      `;
      this.el.messages.appendChild(d);
    }

    connectWS() {
      if (!this.state.sessionId || !this.state.companyId) return;
      if (this.state.ws && this.state.ws.readyState <= 1) return;
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = new URL(proto + "//" + new URL(API_BASE).host + "/ws/chat/" + this.state.sessionId);
      url.searchParams.set("company_id", this.state.companyId);
      this.state.ws = new WebSocket(url.toString());
      this.state.ws.addEventListener("message", (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d.type === "operator_joined") { this.setStatus(STATUS.HUMAN_ACTIVE); this.addMsg("system", d.text); }
          else if (d.type === "operator_left") { this.setStatus(STATUS.CLOSED); this.addMsg("system", d.text); }
          else if (d.type === "message" && d.role === "operator") { this.setStatus(STATUS.HUMAN_ACTIVE); this.addMsg("operator", d.text); }
        } catch (_) {}
      });
      this.state.ws.addEventListener("close", () => {
        if (this.state.status === STATUS.HUMAN_ACTIVE) this.setStatus(STATUS.CLOSED);
      });
    }

    scrollBottom() {
      requestAnimationFrame(() => { this.el.messages.scrollTop = this.el.messages.scrollHeight; });
    }
  }

  if (!window.customElements.get("ai-chat-widget")) {
    window.customElements.define("ai-chat-widget", AIChatWidget);
  }

  window.addEventListener("load", () => {
    if (!document.querySelector("ai-chat-widget")) {
      document.body.appendChild(document.createElement("ai-chat-widget"));
    }
  });
})();

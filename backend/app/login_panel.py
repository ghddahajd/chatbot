"""Страница логина (2026-08-27) — единственная цель: убрать токен из URL. Тот же общий
секрет (OPERATOR_TOKEN), просто вводится один раз через форму и оседает в HttpOnly-cookie,
а не торчит в адресной строке (утекает в историю браузера/логи сервера/случайный скриншот).
Токен на человека — отдельный, более поздний шаг, не этот."""

import html


def sanitize_next_path(value: str, *, default: str = "/analytics") -> str:
    """Живой баг (код-ревью, 2026-08-27): "next" гулял от GET query до POST-редиректа без
    проверки — "/login?next=https://evil.com" после успешного логина уводил оператора на
    чужой домен (open redirect). Разрешаем только локальный путь: начинается с одного "/",
    не "//" и не "/\\" (protocol-relative — браузер трактует как схему, ведёт на чужой
    хост), без переносов строк (защита от header injection через Location)."""

    candidate = (value or "").strip()
    if not candidate or "\n" in candidate or "\r" in candidate:
        return default
    if not candidate.startswith("/") or candidate.startswith("//") or candidate.startswith("/\\"):
        return default
    return candidate


def render_login_page(*, error: bool = False, next_path: str = "/analytics") -> str:
    error_block = (
        '<p class="error-text">Неверный пароль</p>' if error else ""
    )
    # Живой баг (код-ревью, 2026-08-27): next_path раньше шёл в атрибут value="" сырым
    # f-string'ом без экранирования — "/login?next="><script>..." закрывал атрибут и
    # выполнял произвольный JS в браузере оператора (reflected XSS). html.escape — поверх
    # sanitize_next_path (та валидирует ПУТЬ, эта — экранирует ЛЮБОЙ текст для HTML-атрибута,
    # разные уровни защиты, оба нужны).
    safe_next_path = html.escape(sanitize_next_path(next_path), quote=True)
    return f"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Вход</title>
  <style>
    :root {{
      --bg: #fbf9f7;
      --bg-page: #f6f4f3;
      --card: #ffffff;
      --text: #080e0d;
      --text-secondary: #5c6560;
      --border: #ece8e4;
      --accent: #080e0d;
      --accent-soft: #adce6d;
      --radius: 24px;
      --radius-sm: 16px;
      --shadow: 0 16px 40px rgba(8,14,13,.08), 0 4px 12px rgba(8,14,13,.04);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
      background: var(--bg-page);
      color: var(--text);
    }}
    .card {{
      width: 100%;
      max-width: 340px;
      background: var(--card);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 32px 28px;
      margin: 20px;
    }}
    h1 {{ font-size: 20px; font-weight: 800; margin: 0 0 6px; letter-spacing: -.01em; }}
    p.subtitle {{ font-size: 13.5px; color: var(--text-secondary); margin: 0 0 20px; }}
    label {{ font-size: 12.5px; font-weight: 700; color: var(--text-secondary); display: block; margin-bottom: 6px; }}
    input {{
      width: 100%;
      font: inherit;
      font-size: 15px;
      padding: 12px 14px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
    }}
    input:focus {{ outline: 2px solid var(--accent-soft); outline-offset: 1px; }}
    button {{
      width: 100%;
      margin-top: 16px;
      height: 46px;
      border: 0;
      border-radius: 999px;
      background: var(--accent);
      color: #fff;
      font: inherit;
      font-size: 14.5px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ opacity: .9; }}
    .error-text {{ color: #b3261e; font-size: 13px; font-weight: 600; margin: 0 0 12px; }}
  </style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <h1>Аналитика ROSH</h1>
    <p class="subtitle">Введите пароль, чтобы посмотреть дашборд</p>
    {error_block}
    <label for="password">Пароль</label>
    <input id="password" name="password" type="password" autofocus required />
    <input type="hidden" name="next" value="{safe_next_path}" />
    <button type="submit">Войти</button>
  </form>
</body>
</html>
"""

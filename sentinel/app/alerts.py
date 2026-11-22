"""Push alerts to a phone. All channels are optional and use only the standard library.

  ntfy      easiest: install the ntfy app, subscribe to your topic name. No account.
  Telegram  create a bot with @BotFather, put the bot token and your chat id.
  LINE      LINE Messaging API (LINE Notify was shut down in 2025): channel access token + your user ID.

The in-page alarm (sound + browser notification) is handled by the web page itself.
"""
import json
import urllib.error
import urllib.request


def _post_json(url, payload, headers=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def _error_text(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        return f"เชื่อมต่อไม่ได้: {e.reason}"
    return type(e).__name__


def configured(a):
    return bool(a.get("ntfy_topic") or (a.get("telegram_token") and a.get("telegram_chat_id"))
                or (a.get("line_token") and a.get("line_user_id")))


def send_all(a, title, message, urgent=True):
    results = []

    def attempt(channel, fn):
        try:
            fn()
            results.append({"channel": channel, "ok": True})
        except Exception as e:
            results.append({"channel": channel, "ok": False, "error": _error_text(e)})

    if a.get("ntfy_topic"):
        attempt("ntfy", lambda: _post_json((a.get("ntfy_server") or "https://ntfy.sh").rstrip("/"), {
            "topic": a["ntfy_topic"], "title": title, "message": message,
            "priority": 5 if urgent else 3, "tags": ["rotating_light"] if urgent else ["white_check_mark"]}))
    if a.get("telegram_token") and a.get("telegram_chat_id"):
        attempt("telegram", lambda: _post_json(
            f"https://api.telegram.org/bot{a['telegram_token']}/sendMessage",
            {"chat_id": a["telegram_chat_id"], "text": f"{title}\n{message}"}))
    if a.get("line_token") and a.get("line_user_id"):
        attempt("line", lambda: _post_json(
            "https://api.line.me/v2/bot/message/push",
            {"to": a["line_user_id"], "messages": [{"type": "text", "text": f"{title}\n{message}"}]},
            headers={"Authorization": f"Bearer {a['line_token']}"}))
    return results

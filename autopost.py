"""
BY FARKHOD — Telegram autopost
Claude writes the article -> Manus turns it into an MP3 -> the bot posts
the article (Telegram "Article" / rich message) and the audio to the channel.

Secrets (GitHub -> Settings -> Secrets and variables -> Actions):
  ANTHROPIC_API_KEY, MANUS_API_KEY, TELEGRAM_BOT_TOKEN
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
MANUS_KEY = os.environ.get("MANUS_API_KEY", "")
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MANUS_PROFILE = os.environ.get("MANUS_PROFILE", "manus-1.6-lite")
FORCE_CHANNEL = os.environ.get("FORCE_CHANNEL", "").strip()  # manual run
HISTORY_FILE = "history.json"

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- history
def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_history(h):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- Claude
def write_article(channel, recent_titles):
    avoid = "\n".join(f"- {t}" for t in recent_titles[-40:]) or "(aucun)"
    prompt = (
        f"{channel['style']}\n\n"
        f"Sujets déjà publiés récemment (NE PAS répéter) :\n{avoid}\n\n"
        "Choisis un nouveau sujet utile et rédige l'article.\n"
        "Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour :\n"
        '{"title": "...", "article_markdown": "...", "audio_script": "..."}\n'
        "- article_markdown : l'article complet en Markdown (titres #, ##, "
        "listes, **gras**, *italique*, tableaux si utile).\n"
        "- audio_script : le même contenu rédigé pour être LU à voix haute "
        "(français uniquement, sans emojis, sans hashtags, sans liens, "
        "sans symboles de mise en forme), 4 000 à 6 000 caractères."
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": os.environ.get("ANTHROPIC_VERSION", "2026-10-21"),
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 16000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=600,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"])
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0))
    for k in ("title", "article_markdown", "audio_script"):
        if not data.get(k):
            raise ValueError(f"Claude response missing {k}")
    return data


# ---------------------------------------------------------------- Manus
def make_mp3(script, outro, title):
    if not MANUS_KEY:
        return None
    headers = {"x-manus-api-key": MANUS_KEY, "content-type": "application/json"}
    prompt = (
        "Convert the French text below into ONE MP3 file (text-to-speech).\n"
        "- French only, natural native French voice (France), calm clear "
        "teacher tone, moderate speed.\n"
        "- Do not read symbols, emojis or URLs. 1-second pause between parts.\n"
        f"- At the very end, add exactly this sentence: « {outro} »\n"
        "- Deliver the final MP3 as an attachment. Do not ask questions.\n\n"
        f"=== TEXT ===\n{script}"
    )
    r = requests.post(
        "https://api.manus.ai/v2/task.create",
        headers=headers,
        json={
            "message": {"content": [{"type": "text", "text": prompt}]},
            "agent_profile": MANUS_PROFILE,
            "interactive_mode": False,
            "hide_in_task_list": False,
            "title": f"MP3 — {title}"[:120],
        },
        timeout=60,
    )
    r.raise_for_status()
    task_id = r.json()["task_id"]
    log("Manus task", task_id)

    deadline = time.time() + 30 * 60
    while time.time() < deadline:
        time.sleep(30)
        try:
            msgs = requests.get(
                "https://api.manus.ai/v2/task.listMessages",
                headers=headers,
                params={"task_id": task_id, "limit": 100, "order": "desc"},
                timeout=60,
            ).json().get("messages", [])
        except Exception as e:  # noqa: BLE001
            log("Manus poll error:", e)
            continue
        status = None
        for m in msgs:
            if m.get("type") == "status_update" and status is None:
                status = m["status_update"].get("agent_status")
            if m.get("type") == "assistant_message":
                for a in m["assistant_message"].get("attachments") or []:
                    name = (a.get("filename") or "").lower()
                    ctype = (a.get("content_type") or "").lower()
                    if name.endswith(".mp3") or "audio" in ctype:
                        log("MP3 ready:", a.get("filename"))
                        return a["url"]
        log("Manus status:", status)
        if status in ("error",):
            break
        if status == "stopped":
            # finished without an audio attachment
            break
    log("No MP3 produced.")
    return None


# ---------------------------------------------------------------- Telegram
def tg(method, **kw):
    r = requests.post(f"{TG_API}/{method}", timeout=120, **kw)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data}")
    return data["result"]


def post_article(chat_id, markdown):
    try:
        res = tg("sendRichMessage", json={
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        })
        log("Article posted:", res.get("message_id"))
        return res.get("message_id")
    except Exception as e:  # noqa: BLE001
        log("Rich message failed, falling back to normal posts:", e)
    # Fallback: plain text chunks (<= 4000 chars)
    parts, buf = [], ""
    for para in markdown.split("\n\n"):
        if len(buf) + len(para) + 2 > 3900:
            parts.append(buf)
            buf = ""
        buf += para + "\n\n"
    if buf.strip():
        parts.append(buf)
    first = None
    for p in parts:
        res = tg("sendMessage", json={
            "chat_id": chat_id, "text": p,
            "link_preview_options": {"is_disabled": True},
        })
        first = first or res["message_id"]
    return first


def post_audio(chat_id, url, title, performer, reply_to):
    payload = {"chat_id": chat_id, "title": title, "performer": performer}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}
    try:
        tg("sendAudio", json={**payload, "audio": url})
        log("Audio posted (url)")
        return
    except Exception as e:  # noqa: BLE001
        log("Audio by URL failed, uploading file:", e)
    f = requests.get(url, timeout=300)
    f.raise_for_status()
    form = {k: (json.dumps(v) if isinstance(v, dict) else v) for k, v in payload.items()}
    tg("sendAudio", data=form, files={"audio": ("article.mp3", f.content, "audio/mpeg")})
    log("Audio posted (upload)")


# ---------------------------------------------------------------- main
def due_channels(cfg):
    now = datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Tashkent")))
    out = []
    for ch in cfg["channels"]:
        if not ch.get("enabled", True):
            continue
        if FORCE_CHANNEL:
            if FORCE_CHANNEL in (ch["id"], "all"):
                out.append(ch)
            continue
        hours = {int(t.split(":")[0]) for t in ch["times"]}
        if now.hour in hours:
            out.append(ch)
    return out


def main():
    with open("channels.json", encoding="utf-8") as f:
        cfg = json.load(f)
    history = load_history()
    channels = due_channels(cfg)
    if not channels:
        log("No channel due at this hour.")
        return
    failed = False
    for ch in channels:
        log("=== Channel", ch["id"], ch["chat_id"])
        try:
            titles = history.get(ch["id"], [])
            art = write_article(ch, titles)
            log("Article:", art["title"], f"({len(art['article_markdown'])} chars)")
            msg_id = post_article(ch["chat_id"], art["article_markdown"])
            titles.append(art["title"])
            history[ch["id"]] = titles[-100:]
            save_history(history)
            if ch.get("mp3"):
                url = make_mp3(art["audio_script"], ch.get("audio_outro", ""), art["title"])
                if url:
                    post_audio(ch["chat_id"], url, art["title"][:60],
                               ch.get("audio_performer", ""), msg_id)
        except Exception as e:  # noqa: BLE001
            failed = True
            log("ERROR:", repr(e))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

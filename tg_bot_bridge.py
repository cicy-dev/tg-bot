#!/usr/bin/env python3
"""
TG-to-Tmux Bridge Script
Forwards messages from Telegram to tmux pane.
Runs as a supervised process per pane.
"""
import os
import sys
import json
import time
import subprocess
import shlex

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")

if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key.strip()] = val.strip()

import requests
import pymysql

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "tts_bot")

TMUX_SOCKET = os.getenv("TMUX_SOCKET", "/home/w3c_offical/.tmux/default")

LLM_PROXY_HOST = os.getenv("LLM_PROXY_HOST", "127.0.0.1")
LLM_PROXY_PORT = os.getenv("LLM_PROXY_PORT", "18080")
LLM_PROXY_ENABLED = os.getenv("LLM_PROXY_ENABLED", "false").lower() == "true"

CA_BUNDLE_PATH = "/home/w3c_offical/.mitmproxy/mitmproxy-ca-cert.pem"

session = requests.Session()
session.verify = True

API_TOKEN = ''
PANE_ID = ''
TG_CHAT_ID = ''
PROXY = None
STT_ENGINE = "google"
TTS_REPLY = False


def get_db():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        cursorclass=pymysql.cursors.DictCursor
    )


def load_config():
    """Load bot configuration from database."""
    global API_TOKEN, TG_CHAT_ID, PROXY
    
    if not PANE_ID:
        print("Error: PANE_ID not set")
        sys.exit(1)
    
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT tg_token, tg_chat_id, proxy, tg_enable
                FROM ttyd_config
                WHERE pane_id = %s
            """, (PANE_ID,))
            row = c.fetchone()
            
            if not row:
                print(f"Pane {PANE_ID} not in database, stopping bot.")
                sys.exit(0)
            
            if not row.get('tg_enable'):
                print(f"TG bot disabled for pane {PANE_ID}")
                sys.exit(0)
            
            API_TOKEN = str(row.get('tg_token') or '')
            TG_CHAT_ID = str(row.get('tg_chat_id') or '') if row.get('tg_chat_id') else ''
            PROXY = row.get('proxy')
            
            if not API_TOKEN:
                print(f"Error: tg_token not configured")
                sys.exit(1)
    finally:
        conn.close()


def set_proxy():
    """Apply proxy environment variables if configured."""
    if PROXY:
        os.environ['http_proxy'] = PROXY
        os.environ['https_proxy'] = PROXY
        os.environ['HTTP_PROXY'] = PROXY
        os.environ['HTTPS_PROXY'] = PROXY
        os.environ['ALL_PROXY'] = PROXY


def set_llm_proxy():
    """Apply LLM proxy environment variables if enabled.
    Note: Telegram API uses direct connection, proxy is for Agent processes in tmux."""
    global session
    
    if LLM_PROXY_ENABLED:
        proxy_url = f"http://{LLM_PROXY_HOST}:{LLM_PROXY_PORT}"
        os.environ['X_PANE_ID'] = PANE_ID
        print(f"[PROXY] Proxy enabled: {proxy_url} for pane: {PANE_ID}")
    else:
        print(f"[PROXY] Proxy disabled for pane: {PANE_ID}")


def send_tts_voice(text: str):
    """Convert text to speech and send as voice message."""
    try:
        tts_text = text[:500]  # limit length
        r = requests.post("http://127.0.0.1:15002/tts", json={"text": tts_text}, timeout=30)
        if r.status_code == 200:
            url = f"https://api.telegram.org/bot{API_TOKEN}/sendVoice"
            session.post(url, data={"chat_id": TG_CHAT_ID}, files={"voice": ("reply.mp3", r.content, "audio/mpeg")}, timeout=15)
    except Exception as e:
        print(f"TTS voice send failed: {e}")


def send_telegram_message(text: str, parse_mode: str = None):
    """Send message to Telegram bot API."""
    if not TG_CHAT_ID or TG_CHAT_ID == 'None':
        print("Cannot send message: chat_id not bound")
        return False
    
    url = f"https://api.telegram.org/bot{API_TOKEN}/sendMessage"
    data = {
        "chat_id": TG_CHAT_ID,
        "text": text
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    
    headers = {"X-Pane-Id": PANE_ID} if PANE_ID else {}
    
    for attempt in range(3):
        try:
            resp = session.post(url, json=data, timeout=10, headers=headers)
            if resp.status_code == 200:
                return True
            print(f"Telegram API error: {resp.text}")
        except requests.exceptions.RequestException as e:
            print(f"Network error (attempt {attempt + 1}/3): {e}")
            time.sleep(2)
    
    return False


def ensure_pane_alive():
    """Check if tmux pane is alive, restart via fast-api if not."""
    target = PANE_ID
    if target.endswith(".0"):
        target = target[:-2]
    r = subprocess.run(
        ["tmux", "-S", TMUX_SOCKET, "has-session", "-t", target.split(":")[0]],
        capture_output=True
    )
    if r.returncode != 0:
        print(f"Pane {PANE_ID} dead, restarting...")
        send_telegram_message(f"🔄 Pane {PANE_ID} 已断开，正在重启...")
        try:
            h = fastapi_headers()
            resp = requests.post(f"{FASTAPI_BASE}/api/tmux/restart_pane/{PANE_ID}", headers=h, timeout=30)
            if resp.status_code == 200:
                send_telegram_message(f"✅ Pane {PANE_ID} 已重启")
                time.sleep(2)
            else:
                send_telegram_message(f"❌ 重启失败: {resp.text[:200]}")
        except Exception as e:
            send_telegram_message(f"❌ 重启异常: {e}")


def send_to_tmux(text: str, send_enter: bool = False):
    """Send text to tmux pane with proper escaping and env injection."""
    target = PANE_ID
    if target.endswith(".0"):
        target = target[:-2]
    
    pane_id_safe = shlex.quote(target)
    
    env_vars = {
        "HTTP_PROXY": "http://127.0.0.1:18080",
        "HTTPS_PROXY": "http://127.0.0.1:18080",
        "REQUESTS_CA_BUNDLE": "/home/w3c_offical/.mitmproxy/mitmproxy-ca-cert.pem",
        "X_PANE_ID": PANE_ID,
    }
    
    for key, val in env_vars.items():
        subprocess.run(
            ["tmux", "-S", TMUX_SOCKET, "set-environment", "-g", key, val],
            capture_output=True
        )
    
    cmd = ["tmux", "-S", TMUX_SOCKET, "send-keys", "-t", pane_id_safe]
    
    if send_enter:
        cmd.extend([text, "Enter"])
    else:
        cmd.append(text)
    
    print(f"TMUX cmd: {' '.join(cmd)}")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error = result.stderr.strip()
        print(f"TMUX error: {error}")
        send_telegram_message(f"TMUX error: {error}")
        return False
    return True


FASTAPI_BASE = "http://localhost:14444"


def load_api_token() -> str:
    for path in ["/home/w3c_offical/global.json", os.path.expanduser("~/global.json")]:
        try:
            with open(path) as f:
                return json.load(f).get("api_token", "")
        except Exception:
            pass
    return ""


def fastapi_headers():
    return {"Authorization": f"Bearer {load_api_token()}", "Content-Type": "application/json"}


def handle_bot_command(text: str) -> str | None:
    """Handle /commands, return reply text or None to fall through."""
    global PANE_ID
    parts = text.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]
    base = f"{FASTAPI_BASE}/api/tmux"
    h = fastapi_headers()

    try:
        if cmd == "/kb":
            kb = [
                [{"text": "✅ y", "callback_data": "kb_y"}, {"text": "❌ n", "callback_data": "kb_n"}, {"text": "📌 t", "callback_data": "kb_t"}, {"text": "⏎ Enter", "callback_data": "kb_enter"}],
                [{"text": "↑", "callback_data": "kb_up"}, {"text": "↓", "callback_data": "kb_down"}, {"text": "←", "callback_data": "kb_left"}, {"text": "→", "callback_data": "kb_right"}, {"text": "Space", "callback_data": "kb_space"}],
                [{"text": "Ctrl+C", "callback_data": "kb_ctrlc"}, {"text": "Tab", "callback_data": "kb_tab"}, {"text": "Esc", "callback_data": "kb_esc"}, {"text": "Ctrl+D", "callback_data": "kb_ctrld"}],
                [{"text": "Ctrl+A", "callback_data": "kb_ctrla"}, {"text": "Ctrl+L", "callback_data": "kb_ctrll"}, {"text": "Ctrl+Z", "callback_data": "kb_ctrlz"}, {"text": "Ctrl+R", "callback_data": "kb_ctrlr"}],
            ]
            session.post(f"https://api.telegram.org/bot{API_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID,
                "text": "⌨️ 虚拟键盘",
                "reply_markup": {"inline_keyboard": kb},
            }, timeout=10)
            return ""

        if cmd == "/start":
            text = (
                f"👋 TG Bot Bridge\n"
                f"📟 Pane: {PANE_ID}\n\n"
                f"📋 命令:\n"
                f"/kb - 虚拟键盘\n"
                f"/admin - 管理面板\n"
                f"直接发文字 = 发到 tmux"
            )
            session.post(f"https://api.telegram.org/bot{API_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "reply_markup": {
                    "keyboard": [[{"text": "/admin"}, {"text": "/kb"}]],
                    "resize_keyboard": True,
                    "is_persistent": True,
                },
            }, timeout=10)
            return ""

        if cmd == "/admin":
            ttyd_url = ""
            try:
                conn = get_db()
                with conn.cursor() as c:
                    c.execute("SELECT url FROM ttyd_config WHERE pane_id = %s", (PANE_ID,))
                    row = c.fetchone()
                    ttyd_url = row.get("url", "") if row else ""
                conn.close()
            except Exception:
                pass
            lines = [
                f"⚙️ Admin Panel",
                f"📟 pane_id: {PANE_ID}",
                f"💬 chat_id: {TG_CHAT_ID}",
                f"🔗 proxy: {PROXY or 'none'}",
                f"🔀 llm_proxy: {'on' if LLM_PROXY_ENABLED else 'off'}",
            ]
            if ttyd_url:
                lines.append(f"\n🖥 Terminal:\n{ttyd_url}")
            text_msg = "\n".join(lines)
            inline_buttons = []
            if ttyd_url:
                inline_buttons.append([{"text": "🖥 Terminal", "web_app": {"url": ttyd_url}}])
            other = "whisper" if STT_ENGINE == "google" else "google"
            inline_buttons.append([{"text": f"🎙 STT: {STT_ENGINE} → {other}", "callback_data": f"stt_{other}"}])
            tts_status = "🔊 ON" if TTS_REPLY else "🔇 OFF"
            inline_buttons.append([{"text": f"🗣 语音回复: {tts_status}", "callback_data": "toggle_tts"}])
            session.post(f"https://api.telegram.org/bot{API_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID,
                "text": text_msg,
                "reply_markup": {"inline_keyboard": inline_buttons},
            }, timeout=10)
            return ""

    except Exception as e:
        return f"❌ {e}"

    return None


def send_wait_tmux(text: str, timeout: int = 60) -> dict:
    """Send text to tmux pane via FastAPI and wait for reply."""
    token = load_api_token()
    url = f"{FASTAPI_BASE}/api/tmux/send_wait"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"target": PANE_ID, "text": text, "timeout": timeout}
    try:
        resp = requests.post(url, json=data, headers=headers, timeout=timeout + 5)
        if resp.status_code == 200:
            return resp.json()
        print(f"FastAPI error: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"FastAPI request failed: {e}")
    return {}


def check_tmux_pane_exists() -> bool:
    """Check if tmux pane exists."""
    session_part = PANE_ID.split(":")[0]
    result = subprocess.run(
        ["tmux", "-S", TMUX_SOCKET, "has-session", "-t", session_part],
        capture_output=True
    )
    return result.returncode == 0


def get_updates(offset: int | None = None):
    """Get updates from Telegram bot API."""
    url = f"https://api.telegram.org/bot{API_TOKEN}/getUpdates"
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    
    headers = {"X-Pane-Id": PANE_ID} if PANE_ID else {}
    
    for attempt in range(3):
        try:
            resp = session.get(url, params=params, timeout=35, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return data.get("result", [])
            print(f"Telegram API error: {resp.text}")
        except requests.exceptions.RequestException as e:
            print(f"Network error (attempt {attempt + 1}/3): {e}")
            time.sleep(2)
    
    return []


def main():
    global PANE_ID, TG_CHAT_ID, STT_ENGINE, TTS_REPLY
    
    if len(sys.argv) < 2:
        print("Usage: tg_bot_bridge.py <pane_id>")
        sys.exit(1)
    
    PANE_ID = sys.argv[1]
    print(f"Starting TG Bot Bridge for pane: {PANE_ID}")
    
    if not check_tmux_pane_exists():
        print(f"Tmux 会话 {PANE_ID} 不存在, 等待重试...")
        while not check_tmux_pane_exists():
            time.sleep(30)
        print(f"Tmux 会话 {PANE_ID} 已恢复")
    
    load_config()
    set_proxy()
    set_llm_proxy()
    
    print(f"[PROXY] Routing via {LLM_PROXY_HOST}:{LLM_PROXY_PORT} with Pane-ID: {PANE_ID}")
    print(f"Bot started for pane {PANE_ID}, waiting for messages...")
    print(f"TG_CHAT_ID: {TG_CHAT_ID or 'not bound yet - send a message to bind'}")
    
    offset = None
    
    while True:
        try:
            updates = get_updates(offset)
            
            for update in updates:
                if offset is None:
                    offset = 0
                offset = int(update.get("update_id", 0)) + 1
                
                try:
                    message = update.get("message", {})
                    if not message:
                        # Handle callback queries (inline button clicks)
                        cb = update.get("callback_query")
                        if cb:
                            cb_data = cb.get("data", "")
                            cb_id = cb.get("id")
                            print(f"Callback: {cb_data}")
                            if cb_data.startswith("stt_"):
                                STT_ENGINE = cb_data[4:]
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={
                                    "callback_query_id": cb_id, "text": f"🎙 STT → {STT_ENGINE}"
                                }, timeout=5)
                                send_telegram_message(f"🎙 STT 引擎已切换: {STT_ENGINE}")
                            elif cb_data == "toggle_tts":
                                TTS_REPLY = not TTS_REPLY
                                status = "🔊 ON" if TTS_REPLY else "🔇 OFF"
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={
                                    "callback_query_id": cb_id, "text": f"🗣 语音回复: {status}"
                                }, timeout=5)
                                send_telegram_message(f"🗣 语音回复: {status}")
                            elif cb_data.startswith("kb_"):
                                KB_MAP = {
                                    "kb_y": "y", "kb_n": "n", "kb_t": "t", "kb_enter": "Enter", "kb_space": " ",
                                    "kb_up": "Up", "kb_down": "Down", "kb_left": "Left", "kb_right": "Right",
                                    "kb_ctrlc": "C-c", "kb_tab": "Tab", "kb_esc": "Escape", "kb_ctrld": "C-d",
                                    "kb_ctrla": "C-a", "kb_ctrll": "C-l", "kb_ctrlz": "C-z", "kb_ctrlr": "C-r",
                                }
                                key = KB_MAP.get(cb_data, "")
                                if key:
                                    if key in ("Up","Down","Left","Right","Tab","Escape","C-c","C-d","C-a","C-l","C-z","C-r","Enter"):
                                        subprocess.run(["tmux", "send-keys", "-t", PANE_ID, key], timeout=5)
                                    elif cb_data in ("kb_y","kb_n","kb_t"):
                                        subprocess.run(["tmux", "send-keys", "-t", PANE_ID, "-l", key], timeout=5)
                                        subprocess.run(["tmux", "send-keys", "-t", PANE_ID, "Enter"], timeout=5)
                                    else:
                                        subprocess.run(["tmux", "send-keys", "-t", PANE_ID, "-l", key], timeout=5)
                                label = cb_data[3:].upper()
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={
                                    "callback_query_id": cb_id, "text": f"⌨️ {label}"
                                }, timeout=5)
                        continue
                    
                    chat = message.get("chat", {})
                    text = message.get("text", "")
                    
                    if message.get("from", {}).get("is_bot"):
                        continue
                    
                    chat_id = str(chat.get("id"))
                    
                    is_bound = TG_CHAT_ID and TG_CHAT_ID != 'None' and TG_CHAT_ID != 'null'
                    
                    if is_bound and chat_id != str(TG_CHAT_ID):
                        print(f"Ignored message from unauthorized chat_id: {chat_id}")
                        continue
                    
                    if not text:
                        # Handle voice messages
                        voice = message.get("voice")
                        if voice and is_bound:
                            file_id = voice.get("file_id")
                            try:
                                fr = session.get(f"https://api.telegram.org/bot{API_TOKEN}/getFile", params={"file_id": file_id}, timeout=10).json()
                                file_path = fr.get("result", {}).get("file_path", "")
                                if file_path:
                                    audio_resp = session.get(f"https://api.telegram.org/file/bot{API_TOKEN}/{file_path}", timeout=15)
                                    import time as _time
                                    t0 = _time.time()
                                    stt_resp = requests.post("http://127.0.0.1:15003/stt", files={"file": ("voice.ogg", audio_resp.content)}, data={"engine": STT_ENGINE}, timeout=30)
                                    elapsed = round(_time.time() - t0, 1)
                                    stt_data = stt_resp.json()
                                    recognized = stt_data.get("text", "")
                                    engine = stt_data.get("engine", STT_ENGINE)
                                    if recognized:
                                        send_telegram_message(f"🎙 {recognized}\n⚙️ {engine} | ⏱ {elapsed}s")
                                    else:
                                        err = stt_data.get("error", "未知错误")
                                        send_telegram_message(f"❌ 识别失败: {err}\n⚙️ {engine} | ⏱ {elapsed}s")
                            except Exception as e:
                                send_telegram_message(f"❌ 语音处理失败: {e}")
                        continue
                    
                    if not is_bound:
                        print(f"Binding chat_id: {chat_id}")
                        conn = get_db()
                        try:
                            with conn.cursor() as c:
                                c.execute(
                                    "UPDATE ttyd_config SET tg_chat_id = %s WHERE pane_id = %s",
                                    (chat_id, PANE_ID)
                                )
                            conn.commit()
                            TG_CHAT_ID = chat_id
                            send_telegram_message(f"✅ 已绑定 Chat ID: {chat_id}")
                        finally:
                            conn.close()
                        continue
                    
                    # Handle bot commands first
                    if text.startswith("/"):
                        reply = handle_bot_command(text)
                        if reply is not None:
                            if reply:
                                send_telegram_message(reply)
                            continue

                    if text.startswith("/run "):
                        cmd = text[5:].strip()
                    else:
                        cmd = text
                    
                    print(f"Sending: {cmd}")
                    ensure_pane_alive()
                    result = send_wait_tmux(cmd)
                    
                    if result.get("success"):
                        answer = result.get("answer", "").strip()
                        if answer:
                            if len(answer) > 4000:
                                answer = answer[-4000:]
                            send_telegram_message(f"📟 {PANE_ID}\n{answer}")
                            if TTS_REPLY and answer and len(answer) <= 20:
                                send_tts_voice(answer)
                        else:
                            send_telegram_message(f"🚀 Sent to {PANE_ID} (no output)")
                    else:
                        # Fallback: direct tmux send, then capture reply
                        send_to_tmux(cmd, send_enter=True)
                        send_telegram_message(f"🚀 Sent to {PANE_ID}")
                        print(f"TTS_REPLY={TTS_REPLY}")
                        # Wait and capture response
                        time.sleep(8)
                        try:
                            h = fastapi_headers()
                            r = requests.post(f"{FASTAPI_BASE}/api/tmux/capture_pane", json={"pane_id": PANE_ID, "start": -5}, headers=h, timeout=10)
                            output = r.json().get("output", "").strip()
                            if output:
                                lines = [l for l in output.split("\n") if l.strip() and not l.strip().startswith(("λ >", "> ", "55%", "Credits:"))]
                                trimmed = "\n".join(lines[-3:]) if lines else ""
                                if trimmed and trimmed != cmd:
                                    if len(trimmed) > 500:
                                        trimmed = trimmed[-500:]
                                    send_telegram_message(f"📟 {trimmed}")
                                    if TTS_REPLY and len(trimmed) <= 20:
                                        send_tts_voice(trimmed)
                        except Exception as e:
                            print(f"Capture failed: {e}")
                except Exception as e:
                    print(f"Message handling error: {e}")
                    try:
                        send_telegram_message(f"⚠️ 处理出错: {e}")
                    except:
                        pass
            
            time.sleep(1)
            
        except KeyboardInterrupt:
            print("Shutting down...")
            send_telegram_message("⚠️ 系统已下线")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()

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

TTYD_BASE_URL = os.getenv("TTYD_BASE_URL", "https://ttyd-proxy.cicy.de5.net/ttyd")
TTYD_TOKEN = os.getenv("TTYD_TOKEN", "")
FASTAPI_URL = os.getenv("FASTAPI_URL", "http://localhost:14444")
LLM_PROXY_HOST = os.getenv("LLM_PROXY_HOST", "127.0.0.1")
LLM_PROXY_PORT = os.getenv("LLM_PROXY_PORT", "18080")
LLM_PROXY_ENABLED = False

CA_BUNDLE_PATH = "/home/w3c_offical/.mitmproxy/mitmproxy-ca-cert.pem"

session = requests.Session()
session.verify = True

API_TOKEN = ''
PANE_ID = ''
TG_CHAT_ID = ''
PROXY = None
STT_ENGINE = "google"
TTS_REPLY = False
PRIVATE_MODE = False
ALLOWED_USERS = []


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
    global API_TOKEN, TG_CHAT_ID, PROXY, PRIVATE_MODE, ALLOWED_USERS, LLM_PROXY_ENABLED
    
    if not PANE_ID:
        print("Error: PANE_ID not set")
        sys.exit(1)
    
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT tg_token, tg_chat_id, proxy, tg_enable, private_mode, allowed_users, proxy_enable
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
            PRIVATE_MODE = bool(row.get('private_mode'))
            raw_users = row.get('allowed_users') or ''
            ALLOWED_USERS = [u.strip() for u in raw_users.split(',') if u.strip()]
            LLM_PROXY_ENABLED = bool(row.get('proxy_enable'))
            
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


def set_proxy_enable():
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
    
    env_vars = {"X_PANE_ID": PANE_ID}
    if LLM_PROXY_ENABLED:
        env_vars.update({
            "HTTP_PROXY": f"http://127.0.0.1:{LLM_PROXY_PORT}",
            "HTTPS_PROXY": f"http://127.0.0.1:{LLM_PROXY_PORT}",
            "REQUESTS_CA_BUNDLE": "/home/w3c_offical/.mitmproxy/mitmproxy-ca-cert.pem",
        })
    
    for key, val in env_vars.items():
        subprocess.run(
            ["tmux", "-S", TMUX_SOCKET, "set-environment", "-g", key, val],
            capture_output=True
        )
    
    cmd = ["tmux", "-S", TMUX_SOCKET, "send-keys", "-t", pane_id_safe]
    
    if send_enter:
        cmd.extend([text, "Enter"])
        # Send text first, then Enter after a small delay
        cmd_text = ["tmux", "-S", TMUX_SOCKET, "send-keys", "-t", pane_id_safe, "-l", text]
        print(f"TMUX cmd: {' '.join(cmd_text)} + Enter")
        result = subprocess.run(cmd_text, capture_output=True, text=True)
        if result.returncode != 0:
            error = result.stderr.strip()
            print(f"TMUX error: {error}")
            send_telegram_message(f"TMUX error: {error}")
            return False
        time.sleep(0.3)
        result = subprocess.run(["tmux", "-S", TMUX_SOCKET, "send-keys", "-t", pane_id_safe, "Enter"], capture_output=True, text=True)
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


FASTAPI_BASE = FASTAPI_URL

# --- 消息队列 & 状态检查 ---
import threading

msg_queue = []  # 待发送的普通消息队列
queue_lock = threading.Lock()
queue_event = threading.Event()

def check_pane_status_full() -> dict:
    """检查 pane 完整状态"""
    try:
        token = load_api_token()
        pane_base = PANE_ID.split(":")[0] if ":" in PANE_ID else PANE_ID
        resp = requests.get(
            f"{FASTAPI_BASE}/api/tmux/pane/agent/status/{pane_base}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[Queue] Status check error: {e}")
    return {"status": "unknown"}

def check_pane_status() -> str:
    return check_pane_status_full().get("status", "unknown")

def queue_worker():
    """后台线程：等待 pane idle 后合并发送队列中的消息"""
    while True:
        queue_event.wait()
        queue_event.clear()
        while True:
            with queue_lock:
                if not msg_queue:
                    break
            # 检查状态
            status = check_pane_status()
            if status in ("thinking", "compacting"):
                print(f"[Queue] Pane busy ({status}), waiting 3s... ({len(msg_queue)} queued)")
                time.sleep(3)
                continue
            # idle 或其他状态，合并发送
            with queue_lock:
                msgs = list(msg_queue)
                msg_queue.clear()
            if msgs:
                merged = "\n".join(msgs)
                print(f"[Queue] Sending {len(msgs)} merged messages")
                ensure_pane_alive()
                send_to_tmux(merged, send_enter=True)
                
                threading.Thread(target=wait_and_capture, daemon=True).start()
            break

_queue_thread = threading.Thread(target=queue_worker, daemon=True)
_queue_thread_started = False

def extract_last_reply(output: str) -> str:
    """提取 kiro agent 最后一次回复：在 Credits 行之前、prompt 行之后的 > 开头内容"""
    lines = output.split("\n")
    # 找最后一个 Credits 行（回复结束标记）
    credits_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if "▸ Credits:" in lines[i]:
            credits_idx = i
            break
    if credits_idx < 0:
        return ""
    # 往上找 prompt 行（% !>）
    prompt_idx = -1
    for i in range(credits_idx - 1, -1, -1):
        if "% !>" in lines[i] or "% >" in lines[i]:
            prompt_idx = i
            break
    # 提取 prompt 和 credits 之间的内容
    start = prompt_idx + 1 if prompt_idx >= 0 else 0
    reply_lines = lines[start:credits_idx]
    # 清理：去掉空行、thinking、工具调用行
    clean = []
    for l in reply_lines:
        s = l.strip()
        if not s:
            continue
        if "Thinking.." in s:
            continue
        if s.startswith("I will run"):
            continue
        if "(using tool:" in s:
            continue
        if " - Completed in " in s:
            continue
        # 去掉 > 前缀
        if s.startswith("> "):
            s = s[2:]
        clean.append(s)
    return "\n".join(clean)

def wait_and_capture():
    """后台等待 agent 回复完成，用 Completed 切割提取回复"""
    try:
        # 等 agent 开始 thinking（最多 5 秒）
        for _ in range(5):
            time.sleep(1)
            s = check_pane_status()
            if s in ("thinking", "compacting"):
                break
        # 等 agent 回到 idle（最多 300 秒）
        for _ in range(100):
            time.sleep(3)
            s = check_pane_status()
            if s not in ("thinking", "compacting"):
                break
        # capture 并提取回复
        time.sleep(1)
        h = fastapi_headers()
        r = requests.post(f"{FASTAPI_BASE}/api/tmux/capture_pane", json={"pane_id": PANE_ID, "start": -80}, headers=h, timeout=10)
        output = r.json().get("output", "").strip()
        if output:
            reply = extract_last_reply(output)
            if reply and len(reply) <= 500:
                send_telegram_message(f"📟 {reply}")
                if TTS_REPLY and len(reply) <= 200:
                    send_tts_voice(reply)
            elif reply:
                print(f"[Capture] Skipped long reply ({len(reply)} chars)")
    except Exception as e:
        print(f"[Capture] Error: {e}")

def enqueue_message(text: str):
    """将消息加入队列，如果 pane idle 则直接发送"""
    global _queue_thread_started
    if not _queue_thread_started:
        _queue_thread.start()
        _queue_thread_started = True
    status = check_pane_status()
    if status in ("thinking", "compacting"):
        with queue_lock:
            msg_queue.append(text)
        send_telegram_message(f"⏳ Queued ({len(msg_queue)}) — pane is {status}")
        queue_event.set()
    else:
        # idle，直接发送（但先检查队列里有没有积压的）
        with queue_lock:
            if msg_queue:
                msg_queue.append(text)
                msgs = list(msg_queue)
                msg_queue.clear()
            else:
                msgs = [text]
        merged = "\n".join(msgs)
        ensure_pane_alive()
        send_to_tmux(merged, send_enter=True)
        
        threading.Thread(target=wait_and_capture, daemon=True).start()


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
        if cmd == "/ft":
            try:
                r1 = subprocess.run(["/home/w3c_offical/.local/bin/ft", "server-status"], capture_output=True, text=True, timeout=10)
                r2 = subprocess.run(["/home/w3c_offical/.local/bin/ft", "client-status"], capture_output=True, text=True, timeout=10)
                msg = (r1.stdout.strip() + "\n\n" + r2.stdout.strip()).strip() or "No output"
            except Exception as e:
                msg = f"❌ {e}"
            inline_buttons = [[{"text": "🔌 Test Telnet", "callback_data": "ft_telnet"}]]
            session.post(f"https://api.telegram.org/bot{API_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID, "text": msg, "reply_markup": {"inline_keyboard": inline_buttons}
            }, timeout=5)
            return ""

        if cmd == "/gcloud":
            try:
                r1 = subprocess.run(["sudo", "-u", "w3c_offical", "/snap/bin/gcloud", "compute", "instances", "list", "--project=spry-cosine-483915-j6"], capture_output=True, text=True, timeout=10)
                r2 = subprocess.run(["sudo", "-u", "w3c_offical", "/snap/bin/gcloud", "compute", "firewall-rules", "list", "--project=spry-cosine-483915-j6"], capture_output=True, text=True, timeout=10)
                msg = "📦 GCP Instances:\n" + (r1.stdout.strip() or "No instances") + "\n\n🔥 Firewall Rules:\n" + (r2.stdout.strip() or "No rules")
            except Exception as e:
                msg = f"❌ {e}"
            inline_buttons = [[{"text": "🔄 Refresh", "callback_data": "menu_gcloud"}]]
            session.post(f"https://api.telegram.org/bot{API_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID, "text": msg, "reply_markup": {"inline_keyboard": inline_buttons}
            }, timeout=5)
            return ""

        if cmd == "/ls":
            page = int(args[0]) if args else 0
            PAGE_SIZE = 6
            conn = get_db()
            with conn.cursor() as c:
                c.execute("SELECT pane_id, title, tg_enable FROM ttyd_config WHERE active = 1 ORDER BY pane_id")
                rows = c.fetchall()
            conn.close()
            total = len(rows)
            start = page * PAGE_SIZE
            page_rows = rows[start:start + PAGE_SIZE]
            buttons = []
            for r in page_rows:
                label = f"{'🟢' if r['tg_enable'] else '⚪'} {r['title'] or r['pane_id']}"
                buttons.append([{"text": label, "callback_data": f"ls_{r['pane_id']}"}])
            nav = []
            if page > 0:
                nav.append({"text": "⬅️ 上一页", "callback_data": f"ls_page_{page-1}"})
            if start + PAGE_SIZE < total:
                nav.append({"text": "➡️ 下一页", "callback_data": f"ls_page_{page+1}"})
            if nav:
                buttons.append(nav)
            session.post(f"https://api.telegram.org/bot{API_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID,
                "text": f"📋 Active Panes ({total})",
                "reply_markup": {"inline_keyboard": buttons},
            }, timeout=10)
            return ""

        if cmd == "/cf":
            page = int(args[0]) if args else 0
            PAGE_SIZE = 5
            try:
                cf_env = os.environ.copy()
                result = subprocess.run(["python3", "/home/w3c_offical/skills/cf-tunnel.py", "list"], capture_output=True, text=True, timeout=15, env=cf_env)
                print(f"CF stdout: {result.stdout[:200]}")
                print(f"CF stderr: {result.stderr[:200]}")
                lines = [l.strip() for l in result.stdout.strip().split('\n') if '→' in l and 'catch-all' not in l]
                routes = []
                for l in lines:
                    parts_r = l.split('→')
                    if len(parts_r) == 2:
                        host = parts_r[0].strip().rstrip()
                        status = '✅' if '✅' in l else '❌'
                        routes.append((host, status))
            except Exception as ex:
                print(f"CF error: {ex}")
                routes = []
            total = len(routes)
            start = page * PAGE_SIZE
            page_routes = routes[start:start + PAGE_SIZE]
            buttons = []
            for host, status in page_routes:
                buttons.append([{"text": f"{status} {host}", "callback_data": f"cf_{host}"}])
            nav = []
            if page > 0:
                nav.append({"text": "⬅️", "callback_data": f"cf_page_{page-1}"})
            if start + PAGE_SIZE < total:
                nav.append({"text": "➡️", "callback_data": f"cf_page_{page+1}"})
            if nav:
                buttons.append(nav)
            session.post(f"https://api.telegram.org/bot{API_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID,
                "text": f"🌐 CF Routes ({total})",
                "reply_markup": {"inline_keyboard": buttons},
            }, timeout=10)
            return ""

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
                f"直接发文字 = 发到 tmux"
            )
            inline = [
                [{"text": "⚙️ Admin", "callback_data": "menu_admin"}, {"text": "⌨️ 键盘", "callback_data": "menu_kb"}],
            ]
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

        if cmd == "/kiro":
            info = check_pane_status_full()
            status = info.get("status", "unknown")
            ctx = info.get("contextUsage")
            status_emoji = {"idle": "🟢", "thinking": "🟡", "compacting": "🔵", "wait_auth": "🔴", "wait_startup": "⚪"}.get(status, "⚫")
            lines = [f"🤖 Kiro Agent Status", f"{status_emoji} Status: {status}"]
            if ctx is not None:
                lines.append(f"📊 Context: {ctx}%")
            with queue_lock:
                q = len(msg_queue)
                q_preview = list(msg_queue)
            lines.append(f"📬 Queue: {q} msgs")
            if q_preview:
                for i, m in enumerate(q_preview[:5], 1):
                    lines.append(f"  {i}. {m[:60]}")
                if len(q_preview) > 5:
                    lines.append(f"  ... +{len(q_preview)-5} more")
            buttons = [
                [{"text": "🗜 /compact", "callback_data": "kiro_compact"}, {"text": "🔄 /model", "callback_data": "kiro_model"}],
                [{"text": "🗑 Clear Queue", "callback_data": "kiro_clear"}, {"text": "🔃 Refresh", "callback_data": "kiro_refresh"}],
            ]
            session.post(f"https://api.telegram.org/bot{API_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID,
                "text": "\n".join(lines),
                "reply_markup": {"inline_keyboard": buttons},
            }, timeout=10)
            return ""

        if cmd == "/admin":
            pane_base = PANE_ID.split(":")[0] if ":" in PANE_ID else PANE_ID
            ttyd_url = f"{TTYD_BASE_URL}/{pane_base}/?token={TTYD_TOKEN}" if TTYD_TOKEN else f"{TTYD_BASE_URL}/{pane_base}/"
            # Get public IP and domain
            try:
                pub_ip = requests.get("https://ifconfig.me", timeout=5).text.strip()
            except:
                pub_ip = "unknown"
            domain = "gcp-hk-1001.cicy.de5.net"
            try:
                import socket
                resolved = socket.gethostbyname(domain)
                domain_status = f"✅ {domain} → {resolved}" if resolved == pub_ip else f"⚠️ {domain} → {resolved} (mismatch)"
            except:
                domain_status = f"❌ {domain} (resolve failed)"
            lines = [
                f"⚙️ Admin Panel",
                f"📟 pane_id: {PANE_ID}",
                f"💬 chat_id: {TG_CHAT_ID}",
                f"🌐 IP: {pub_ip}",
                f"🏷 Domain: {domain}",
                f"🔗 {domain_status}",
                f"🔗 proxy: {PROXY or 'none'}",
                f"🔀 proxy_enable: {'on' if LLM_PROXY_ENABLED else 'off'}",
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
            inline_buttons.append([{"text": "📋 Panes", "callback_data": "menu_ls"}, {"text": "🌐 CF Routes", "callback_data": "menu_cf"}])
            inline_buttons.append([{"text": "🚀 FT Status", "callback_data": "menu_ft"}, {"text": "☁️ GCloud", "callback_data": "menu_gcloud"}])
            inline_buttons.append([{"text": "🔄 更新DNS→当前IP", "callback_data": "update_dns"}])
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
    set_proxy_enable()
    
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
                            if cb_data == "ft_telnet":
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "⏳ Testing..."}, timeout=5)
                                try:
                                    import socket
                                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                    sock.settimeout(5)
                                    result = sock.connect_ex(("34.150.15.106", 7000))
                                    sock.close()
                                    if result == 0:
                                        send_telegram_message("✅ Telnet 34.150.15.106:7000 OK")
                                    else:
                                        send_telegram_message(f"❌ Telnet 34.150.15.106:7000 Failed (errno {result})")
                                except Exception as e:
                                    send_telegram_message(f"❌ Telnet error: {e}")
                            elif cb_data == "update_dns":
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "⏳ 检查中..."}, timeout=5)
                                try:
                                    import socket
                                    pub_ip = requests.get("https://ifconfig.me", timeout=5).text.strip()
                                    domain = "gcp-hk-1001.cicy.de5.net"
                                    try:
                                        resolved = socket.gethostbyname(domain)
                                    except:
                                        resolved = ""
                                    if resolved == pub_ip:
                                        send_telegram_message(f"✅ {domain} 已指向 {pub_ip}，无需更新")
                                    else:
                                        zone_id = "70b43be80d3f763069a08457d7794e43"
                                        cf_token = os.environ.get("CLOUDFLARE_API_TOKEN_TUNNEL", "")
                                        cf_headers = {"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"}
                                        r = requests.get(f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?name={domain}", headers=cf_headers, timeout=10)
                                        records = r.json().get("result", [])
                                        if records:
                                            rec_id = records[0]["id"]
                                            requests.put(f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec_id}", headers=cf_headers, json={"type": "A", "name": domain, "content": pub_ip, "proxied": False, "ttl": 1}, timeout=10)
                                        else:
                                            requests.post(f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records", headers=cf_headers, json={"type": "A", "name": domain, "content": pub_ip, "proxied": False, "ttl": 1}, timeout=10)
                                        send_telegram_message(f"✅ {domain} → {pub_ip} (已更新)")
                                except Exception as e:
                                    send_telegram_message(f"❌ DNS更新失败: {e}")
                            elif cb_data == "copy_ip":
                                try:
                                    ip = requests.get("https://ifconfig.me", timeout=5).text.strip()
                                except:
                                    ip = "unknown"
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                                send_telegram_message(f"`{ip}`", parse_mode="Markdown")
                            elif cb_data == "copy_domain":
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                                send_telegram_message(f"`gcp-hk-1001.cicy.de5.net`", parse_mode="Markdown")
                            elif cb_data == "menu_gcloud":
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                                handle_bot_command("/gcloud")
                            elif cb_data.startswith("menu_"):
                                menu = cb_data[5:]
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                                handle_bot_command(f"/{menu}")
                            elif cb_data.startswith("stt_"):
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
                            elif cb_data.startswith("cf_page_"):
                                page = int(cb_data[8:])
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                                handle_bot_command(f"/cf {page}")
                            elif cb_data.startswith("cf_"):
                                host = cb_data[3:]
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "⏳ Testing..."}, timeout=5)
                                try:
                                    r = requests.get(f"https://{host}/", timeout=10, verify=True)
                                    code = r.status_code
                                except Exception as ex:
                                    code = f"ERR: {ex}"
                                send_telegram_message(f"🌐 {host}\nHTTP: {code}")
                            elif cb_data.startswith("ls_page_"):
                                page = int(cb_data[8:])
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                                handle_bot_command(f"/ls {page}")
                            elif cb_data.startswith("ls_"):
                                pane = cb_data[3:]
                                try:
                                    conn = get_db()
                                    with conn.cursor() as c:
                                        c.execute("SELECT title FROM ttyd_config WHERE pane_id = %s", (pane,))
                                        row = c.fetchone()
                                    conn.close()
                                    title = row.get("title", pane) if row else pane
                                    pane_base = pane.split(":")[0] if ":" in pane else pane
                                    ttyd_url = f"{TTYD_BASE_URL}/{pane_base}/?token={TTYD_TOKEN}" if TTYD_TOKEN else f"{TTYD_BASE_URL}/{pane_base}/"
                                    send_telegram_message(f"🖥 {title}\n{ttyd_url}")
                                except Exception as e:
                                    send_telegram_message(f"📟 {pane}: {e}")
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                            elif cb_data == "kiro_compact":
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "⏳ Queuing /compact..."}, timeout=5)
                                enqueue_message("/compact")
                            elif cb_data == "kiro_model":
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "⏳ Queuing /model..."}, timeout=5)
                                enqueue_message("/model")
                            elif cb_data == "kiro_clear":
                                with queue_lock:
                                    cleared = len(msg_queue)
                                    msg_queue.clear()
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": f"🗑 Cleared {cleared} msgs"}, timeout=5)
                                handle_bot_command("/kiro")
                            elif cb_data == "kiro_refresh":
                                session.post(f"https://api.telegram.org/bot{API_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "🔃 Refreshing..."}, timeout=5)
                                handle_bot_command("/kiro")
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
                    user_id = str(message.get("from", {}).get("id", ""))
                    
                    # Permission check
                    if PRIVATE_MODE and ALLOWED_USERS and user_id not in ALLOWED_USERS:
                        print(f"Blocked user_id: {user_id}")
                        continue
                    
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
                    enqueue_message(cmd)

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

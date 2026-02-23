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
                print(f"Error: Pane {PANE_ID} not found in database")
                sys.exit(1)
            
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


def capture_tmux_output(max_lines=50) -> str:
    """Capture current tmux pane content."""
    target = PANE_ID
    if target.endswith(".0"):
        target = target[:-2]
    result = subprocess.run(
        ["tmux", "-S", TMUX_SOCKET, "capture-pane", "-t", shlex.quote(target), "-p", "-S", f"-{max_lines}"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return result.stdout.rstrip()
    return ""


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
    global PANE_ID, TG_CHAT_ID
    
    if len(sys.argv) < 2:
        print("Usage: tg_bot_bridge.py <pane_id>")
        sys.exit(1)
    
    PANE_ID = sys.argv[1]
    print(f"Starting TG Bot Bridge for pane: {PANE_ID}")
    
    if not check_tmux_pane_exists():
        error_msg = f"错误: Tmux 会话 {PANE_ID} 不存在"
        print(error_msg)
        load_config()
        send_telegram_message(error_msg)
        sys.exit(1)
    
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
                
                message = update.get("message", {})
                if not message:
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
                
                if text.startswith("/run "):
                    cmd = text[5:].strip()
                    print(f"Executing command: {cmd}")
                elif text.startswith("/"):
                    cmd = text[1:].strip()
                    print(f"Executing command: {cmd}")
                else:
                    cmd = text
                    print(f"Typing and executing: {cmd}")
                
                if send_to_tmux(cmd, send_enter=True):
                    time.sleep(1.5)
                    output = capture_tmux_output()
                    if output:
                        # Telegram message limit is 4096 chars
                        if len(output) > 4000:
                            output = output[-4000:]
                        send_telegram_message(f"📟 {PANE_ID}\n{output}")
                    else:
                        send_telegram_message(f"🚀 Sent to {PANE_ID}")
                else:
                    send_telegram_message(f"发送失败: {cmd}")
            
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

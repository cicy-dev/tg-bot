#!/usr/bin/env python3
"""
TG Bot Supervisor Manager
Polls database every 10-15 seconds to manage TG bot processes via supervisor.
"""
import os
import sys
import time
import json
import logging
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")

if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key.strip()] = val.strip()

from jinja2 import Template

import pymysql

LOG_FILE = os.getenv("LOG_FILE", "/home/w3c_offical/projects/ai-workers/tg-bot/manager.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "tts_bot")

SUPERVISOR_CONF_DIR = os.getenv("SUPERVISOR_CONF_DIR", "/etc/supervisor/conf.d")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SUPERVISOR_CONF_TEMPLATE = """[program:{{ program_name }}]
command={{ script_path }} {{ pane_id }}
directory={{ workspace }}
environment=PATH="/usr/local/bin:/usr/bin:/bin",PYTHONPATH="/usr/lib/python3/dist-packages:/usr/local/lib/python3.12/dist-packages"
autostart=true
autorestart=true
startretries=3
stderr_logfile=/var/log/supervisor/{{ program_name }}.err.log
stdout_logfile=/var/log/supervisor/{{ program_name }}.out.log
user=root
"""


def get_db():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        cursorclass=pymysql.cursors.DictCursor
    )


def load_api_token() -> str:
    for path in ["/home/w3c_offical/global.json", os.path.expanduser("~/global.json")]:
        try:
            with open(path) as f:
                return json.load(f).get("api_token", "")
        except Exception:
            pass
    return ""


def get_active_bots():
    """Get all bots with tg_enable=1 from database."""
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT pane_id, tg_token, tg_chat_id, tg_enable, workspace, proxy
                FROM ttyd_config
                WHERE tg_enable = 1 AND tg_token IS NOT NULL AND tg_token != ''
            """)
            return c.fetchall()
    finally:
        conn.close()


def get_supervisor_programs():
    """Get list of currently managed programs from supervisor conf dir."""
    programs = set()
    conf_dir = Path(SUPERVISOR_CONF_DIR)
    if conf_dir.exists():
        for f in conf_dir.glob("tg_bot_*.conf"):
            name = f.stem
            programs.add(name)
    return programs


def write_supervisor_conf(program_name: str, pane_id: str, workspace: str):
    """Generate supervisor config file for a bot."""
    script_path = os.path.join(SCRIPT_DIR, "tg_bot_bridge.py")
    
    conf_content = Template(SUPERVISOR_CONF_TEMPLATE).render(
        program_name=program_name,
        script_path=f"/usr/bin/python3 {script_path}",
        pane_id=pane_id,
        workspace=workspace or os.path.expanduser("~")
    )
    
    conf_path = Path(SUPERVISOR_CONF_DIR) / f"{program_name}.conf"
    import subprocess
    result = subprocess.run(
        ["sudo", "tee", str(conf_path)],
        input=conf_content,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        logger.error(f"Failed to write config: {result.stderr}")
    return conf_path


def remove_supervisor_conf(program_name: str):
    """Remove supervisor config file."""
    conf_path = Path(SUPERVISOR_CONF_DIR) / f"{program_name}.conf"
    if conf_path.exists():
        import subprocess
        result = subprocess.run(["sudo", "rm", str(conf_path)], capture_output=True)
        return result.returncode == 0
    return False


def run_supervisorctl(cmd: str):
    """Run supervisorctl command."""
    import subprocess
    result = subprocess.run(["supervisorctl", cmd], capture_output=True, text=True)
    return result.returncode == 0, result.stdout, result.stderr


def sync_bots():
    """Sync supervisor processes with database state."""
    active_bots = get_active_bots()
    current_programs = get_supervisor_programs()
    
    active_program_names = {
        f"tg_bot_{row['pane_id'].replace(':', '_')}"
        for row in active_bots
    }
    
    to_stop = current_programs - active_program_names
    to_start = active_program_names - current_programs
    to_restart = set()
    
    existing_configs = {}
    for row in active_bots:
        program_name = f"tg_bot_{row['pane_id'].replace(':', '_')}"
        existing_configs[program_name] = row
    
    for program_name in list(current_programs):
        conf_path = Path(SUPERVISOR_CONF_DIR) / f"{program_name}.conf"
        if not conf_path.exists():
            continue
        
        content = conf_path.read_text()
        
        if program_name in existing_configs:
            row = existing_configs[program_name]
            pane_id = row['pane_id']
            new_workspace = row.get('workspace', '')
            
            need_restart = False
            
            for line in content.splitlines():
                if line.startswith("directory="):
                    current_workspace = line.split("=", 1)[1].strip()
                    if current_workspace != (new_workspace or os.path.expanduser("~")):
                        need_restart = True
            
            if need_restart:
                to_restart.add(program_name)
    
    if to_stop or to_start or to_restart:
        for program_name in to_stop:
            logger.info(f"[STOP] {program_name}")
            remove_supervisor_conf(program_name)
            run_supervisorctl(f"stop {program_name}")
        
        for program_name in to_start:
            row = existing_configs[program_name]
            pane_id = row['pane_id']
            logger.info(f"[START] {program_name} for pane {pane_id}")
            
            write_supervisor_conf(
                program_name=program_name,
                pane_id=pane_id, 
                workspace=row.get('workspace')
            )
        
        for program_name in to_restart:
            row = existing_configs[program_name]
            pane_id = row['pane_id']
            logger.info(f"[RESTART] {program_name}")
            
            write_supervisor_conf(
                program_name=program_name,
                pane_id=pane_id,
                workspace=row.get('workspace')
            )
            run_supervisorctl(f"stop {program_name}")
        
        run_supervisorctl(f"reread")
        run_supervisorctl(f"update")


def main():
    print("TG Bot Supervisor Manager started")
    interval = 10
    
    while True:
        try:
            sync_bots()
        except Exception as e:
            print(f"Error during sync: {e}")
        
        time.sleep(interval)


if __name__ == "__main__":
    main()

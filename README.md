# TG-Tmux 联动控制器

通过 Telegram 聊天框远程操控 Tmux 终端。

## 快速开始

### 1. 安装依赖环境

```bash
cd /home/w3c_offical/projects/ai-workers/tg-bot
pip install -r requirements.txt
```

### 2. 配置环境变量

创建 `.env` 文件：

```bash
cp ../fast-api/.env .env
# 或手动创建
```

确保以下变量正确：
- `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`
- `SUPERVISOR_CONF_DIR=/etc/supervisor/conf.d` (默认)
- `TMUX_SOCKET=/home/w3c_offical/.tmux/default` (默认)

### 3. 启动全局管理器

```bash
python3 supervisor_manager.py > manager.log 2>&1 &
tail -f manager.log
```

看到 `[START] tg_bot_...` 说明已成功识别数据库中的机器人。

### 4. 通知 Supervisor 立即上线

```bash
supervisorctl reread
supervisorctl update
supervisorctl status
```

## 验证

### 检查进程状态

```bash
sudo supervisorctl status
```

应看到 `tg_bot_你的PaneID` 进程处于 RUNNING 状态。

### 手机互动

1. 打开 Telegram Bot
2. 发送：`你好，系统上线了吗？` (只是输入文字，不执行)
3. 发送：`/run ls` (执行命令)

## 指令说明

| 指令 | 说明 | 是否执行 |
|------|------|----------|
| 普通文字 | 输入到终端 | 否 |
| `/run xxx` | 执行命令 | 是 |
| `/xxx` | 执行命令 | 是 |

## 文件说明

```
tg-bot/
├── supervisor_manager.py   # 全局管理器 (大脑)
├── tg_bot_bridge.py       # 消息中继脚本
├── requirements.txt        # Python 依赖
└── .env                   # 环境变量 (需创建)
```

## 数据库要求

`ttyd_config` 表需包含以下字段：

- `pane_id` - Tmux Pane ID
- `tg_token` - Telegram Bot Token
- `tg_chat_id` - Telegram Chat ID
- `tg_enable` - 启用标志 (1=启用, 0=禁用)
- `workspace` - 工作目录
- `proxy` - 代理地址 (可选)

## 停止服务

```bash
# 停止管理器
pkill -f supervisor_manager.py

# 停止所有 TG Bot
supervisorctl stop tg_bot_*
supervisorctl update
```
# tg-bot

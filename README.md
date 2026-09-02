# GoLand Dashboard

本机 GoLand 实时监控面板。仅监听 `127.0.0.1`，不依赖第三方 Python 包。

## 启动

```bash
cd /Users/zty/Desktop/zruler/tools/goland-dashboard
./start.sh
```

默认地址：<http://127.0.0.1:17654>

指定端口：

```bash
./start.sh --port 17655
```

## 停止操作的边界

- 每次操作前重新核验 PID、命令和 GoLand 父子关系。
- GoLand 主进程、JCEF 和 `fsnotifier` 禁止停止。
- 停止按钮只发送 `SIGTERM`，不发送 `SIGKILL`。
- 单进程操作包含该进程的子进程，避免留下孤儿进程。
- 整组停止需要输入页面显示的确认文本。
- CC GUI/Claude Agent 标为高风险，因为可能中断活跃会话。
- 插件可能自动重新拉起进程；持久解决需要禁用插件并重启 GoLand。

操作记录保存在 `logs/actions.jsonl`。

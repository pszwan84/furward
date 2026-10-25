# ver1 后端 P0

用户已确认只负责后端P0，不实现3D前端。源计划见用户2026-10-24附件；代码合同见CONTRACT.md。

## 启动

从解包根目录运行README中的venv安装与uvicorn命令。Python3.10+；本轮实际环境为Mac ARM64/Python3.14。
依赖完整固定在requirements.lock，已在本项目安装并执行，不代表Ubuntu/T1板端已运行。
默认Mock；地图是米制合成示例。停止/取消/暂停优先于调度，不等待LLM。
Mock时间缩短为真实公式时长的3%，画面与返回包含time_scale，不能据此计算实机延迟。

前端同源连接REST `/api/*` 与 `/ws/app`，响应`{ok:true,data:...}`或统一错误。
`/docs`提供OpenAPI。默认室外，移动/转向/导航执行返回MODE_FORBIDDEN。
先POST `/api/mode` `{ "mode":"indoor" }`，再POST `/api/nav/plan`
`{ "mode":"indoor","from":"start","to":"door" }`，最后POST `/api/nav/execute`。
所有控制POST不需要虚构坐标或模型Key。`/api/nav/current`含route/nav/task。

## 重要语义

- 室内路线用Dijkstra；估算位置源为command_integral，没有里程计/定位/闭环到点证明。
- 默认固定起点和朝向。换起点需现场标记、测绘与人工对齐；模拟数据不是现场地图。
- Mock中暂停保留其报告的部分进度，继续显式触发。未知进度拒绝续走，不重发整段假装是剩余路程。
- `caution`和`stop`在P0均保守暂停。动态低速控制尚未实现。clear稳定1.5秒后仍需显式继续。
- 停止回执和软件PAUSED/DONE不表示物理静止/到达。硬件结果始终未验证。
- 室外仅有服务台路线的离线合成示意，`cached:true,synthetic:true`、真实距离/时长为null；
  未调用高德，不提供任意目的地规划。LLM/DeepSeek模型、真实视觉/RTSP、手机录音/定位均未集成。
- P1接口返回501；有限规则口令不冒称LLM。没有注册手机、读密钥或开外部模型服务。
- request_id固定命令内容、持久关联原任务；重启不自动重放活动任务。单库OS独占。
- WS事件带真实SQLite事件序号，慢客户端丢最旧缓冲后应GET `/api/events?after=...`补读。
- 本地保存必要文本事件，不保存音视频。退出保留库，不用删库伪造故障恢复。

## 模拟桥全链路

先停止本项目自有Mock后端（不要按端口杀其他进程）。使用独立库：

```bash
ROBOT_BACKEND=bridge ROBOT_DB_PATH=var/backend/bridge.sqlite3 .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 18990 --workers 1 --ws-max-size 16384
# 第二终端，同一项目根目录
.venv/bin/python -m bridge.bridge --backend-url ws://127.0.0.1:18990/ws/bridge --adapter mock
```

这是真实HTTP/WebSocket软件链路和Mock执行，不是T1 ROS/实机。
桥接版本/能力/会话与命令代际固定，断连/未知结果不自动重试。CLI应用心跳丢失3秒请求停止；
后端接收超时5秒断开会话。这里只验证软件请求，机器人侧超时和实体遥控器效果仍待实测。
原生guide.ros2是另一条较早的受限SDK实验路径；它没有被悄悄当成ver1的完整动作桥。

默认loopback。手机LAN访问需你明确在受控热点上以`--host 0.0.0.0`运行并核对防火墙；
当前没有账号或公网保护，不对外发布。HTTP局域网麦克风/定位能力不作为P0前提。
不同时启动PrimeBuddy/其他业务运动源。真正接T1前，先实现并测试桥接硬件适配器，再做ACCEPTANCE无载验收。

# 绒途独立城市演示服务

此服务只驱动网页里的虚拟角色。访客各自拥有独立会话，不导入或启动 `robot/` 的设备控制、相机、模型或桥接程序。

在 `demo-service` 目录运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m uvicorn demo_service.main:app --host 127.0.0.1 --port 8100 --workers 1
```

需要 Python 3.10 或更新版本。默认读取工程根目录 `shared/route.json`；可以通过 `DEMO_WORLD_PATH` 指定其他路径。共享文件在进程启动时读取，更新后应重启服务。

使用单进程、单 worker。会话保存在内存中，服务重启后访客需要重新创建会话；不能直接增加 workers 或多副本。前端和服务通过同源代理连接 `/api/demo/` 与 `/ws/demo`，无需开启跨域访问。

```sh
.venv/bin/python -m pip install -r requirements-dev-lock.txt
.venv/bin/python -m pytest -q
```

`requirements-lock.txt` 锁定本次验证通过的运行依赖；`requirements-dev-lock.txt` 额外包含测试依赖。未锁版本的 requirements 文件用于主动升级依赖，升级后需重新执行测试。

会话 30 分钟无活动后过期，上限 100 个。浏览器每 5 秒发送一次应用层 WS ping，每条连接独立使用 15 秒心跳期限。最后一条有效连接正常关闭后有 10 秒重连宽限；没有正常关闭通知的半开连接，正常调度下从最后有效 ping 起约 25 秒内暂停（15 秒识别失活，加 10 秒宽限）。服务按期限限制可继续行走的距离，不能承诺物理断网后精确 10 秒暂停。已暂停的会话重连后仍需手动继续。新建会话也应先连接 WS，再开始导航。协议详见工程 `docs/demo-api.md`。

`tests/test_blackhole.py` 会在临时本机端口启动独立服务和 TCP 代理，双向丢弃字节但不主动发送 FIN/RST，实际等待约 25 秒验证失活、暂停及重连。它不连接或重启现有演示站点。前端的异步竞态测试在 `web` 目录执行 `node --test scripts/test-demo-session.mjs`。


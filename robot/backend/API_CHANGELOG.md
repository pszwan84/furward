# Backend API P0

- 新增独立 FastAPI `backend.main:create_app` 和默认 `app`，生命周期启动/关闭单个 runtime。默认 Mock，`ROBOT_BACKEND=bridge` 显式选择 Bridge。默认模块导入不打开数据库。
- ver1 P0 REST、事件历史、应用/Bridge WebSocket、中文 `/debug` 调试页。音频、定位和视觉流显式返回 501。
- 写请求 16 KiB 上限，拒绝重复 JSON 键、非有限数、额外模型字段；同源写请求及 WebSocket 校验，不启用通配 CORS。
- 调试页标明模拟、估算坐标和离线合成缓存；停止/取消始终可访问。此页面不代表真机安全验收。
- 浏览器验证：`node scripts/backend_browser.mjs` 启动隔离 Mock 服务、临时数据库和实际 Chrome，端口 18991；报告与截图输出到 `artifacts/backend-browser/`。

本地启动仅单 worker：`.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 18990`。不要将此无账户的本地调试服务公开到网络。

验证记录（2026-10-24）：先运行 `python -m unittest tests.test_backend_api` 得到 `ModuleNotFoundError: backend.main`（RED）；实现后 8 项 API 测试通过。实际 Chrome 独立临时 profile 验证 Mock 标识、室内规划、停止/取消、模拟障碍事件及 390px 页面无横向溢出；`artifacts/backend-browser/report.json` 的 `ok` 为 true、`browser_errors` 为空。桌面及手机截图已人工查看。

# 0.2 — T1 bridge software

- Native adapter now implemented, default remains mock. `t1-sim` uses the same
  adapter with pure memory SDK peer; no generated ROS types or DDS are implied.
- Backend opt-in `ROBOT_ALLOW_PHYSICAL=1` plus native CLI/config opt-in required
  for future onsite motion; hardware_verified remains false.
- Native Mimic acceptance holds task status AWAITING_OPERATOR with last_result;
  no automatic next step, and cancel does not clear native uncertainty.
- Motion dispatch deadline30s, stop transport2s, posture transitions restricted
  to reviewed quadruped graph; no transformation or low-level joints.
- T1 BLUE_FLOW rejected explicitly: no matching official effect.
- Deployment entry is START_HERE.md, independent of legacy guide launch paths.

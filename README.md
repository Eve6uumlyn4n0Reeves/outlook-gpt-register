Outlook 邮箱 + ChatGPT 批量注册（后端脚本）

说明
- 本仓库提供一个后端服务，基于 FastAPI + Playwright 驱动 Chrome，在“无痕窗口”中执行 ChatGPT 注册流程，并通过 Outlook 邮箱接收 OTP 验证码。
- 目标是批量化、稳定化地跑注册流程，同时尽量降低异常触发（如人机验证）的概率；不做也不提供任何验证码绕过方案。
- 适用场景：研究与测试自动化流程。请遵守目标站点的使用条款与法律法规，合理使用。

项目结构（与关键模块）
- gpt_register/browser.py：浏览器封装。默认以 Chrome 原生无痕窗口 UI 启动（incognito），移除常见自动化标志；可按配置附加启动参数。
- gpt_register/flows/chatgpt_signup.py：注册流程。一次初始导航到 chatgpt.com，之后通过页面交互推进；中英兼容；含 Cloudflare 挑战检测与等待。
- gpt_register/otp.py：OTP 轮询与提取。通过 mail_service 接口读取 Outlook 邮件并解析 6 位验证码。
- gpt_register/runner.py：按账号并发执行注册任务，提供节流与起始时间错峰；统一创建无痕窗口。
- gpt_register/jobs.py：批量任务队列与结果持久化（jsonl + sqlite）。
- main.py：FastAPI 服务入口，暴露 /gpt-register/* 相关接口与简单的运行页面。

环境要求
- 系统安装 Google Chrome（建议稳定版）。
- Python 3.10+，安装依赖：pip install -r requirements.txt
- 首次运行需安装 Playwright 浏览器内核（若使用 chrome channel 可不用）：python -m playwright install
- Outlook 邮箱 OAuth2 凭据（client_id、refresh_token）。

配置
- 文件：config/settings.yaml（示例已提供）
  - headless: false 建议在可视化模式下运行，便于人工处理挑战。
  - concurrency: 1 建议单 IP 低并发。
  - home_budget_sec: 6.0 初始页面观察时间，期间会有轻微滚动/鼠标移动。
  - action_delay_ms / action_delay_jitter_ms 步骤间自然停顿。
  - cloudflare.max_wait_sec / poll_interval_ms 只做检测与等待，超时则失败；不自动点击或绕过。
  - risk.max_concurrency_per_ip / stagger_start_sec 简单节流与错峰。
  - relax_fingerprints: true 放宽部分自动化表征（不等于绕过）。
  - browser_extra_args: [] 可追加自定义启动参数（可留空）。
- 文件：config/flow.yaml
  - preset: chatgpt_signup
  - start_url: https://chatgpt.com/

运行
1) 启动服务
   - pip install -r requirements.txt
   - python main.py
   - 访问 http://localhost:8000

2) 保存 Outlook 账号凭据（用于收验证码）
   - POST /accounts 保存 {email, refresh_token, client_id, password?}
   - 具体接口见 main.py 中账户管理部分（本仓库提供简化 UI 页面）。

3) 发起批量注册任务
   - POST /gpt-register/runs
   - 示例请求体：
     {
       "concurrency": 1,
       "headful": true,
       "limit": null,
       "accounts": [
         {"email": "newuser001@outlook.com", "password": null, "proxy": null, "user_agent": null},
         {"email": "newuser002@outlook.com"}
       ]
     }
   - 说明：
     - password 留空时，系统会优先使用保存的 Outlook 账户密码作为站点密码（或使用 default_site_password）。
     - proxy/user_agent 可按账号单独设定；未设定则使用系统默认。

4) 查询状态与导出结果
   - GET /gpt-register/runs/{run_id}
   - GET /gpt-register/runs/{run_id}/events（SSE 事件流）
   - POST /gpt-register/runs/{run_id}/cancel | /pause | /resume
   - GET /gpt-register/runs/{run_id}/export?status=success&format=csv

流程要点与问题分析
- 人机验证（Cloudflare/Turnstile）
  - 常见现象：chatgpt.com 首屏或注册后续流程中出现“确认您是真人”。
  - 处理策略：仅检测与等待，人工完成后继续；超过 max_wait_sec 则失败。不会尝试自动点击或绕过。
  - 行为层面：初始阶段执行轻微滚动/鼠标移动/短暂停顿，点击前悬停，小幅移动与微停顿；步骤间随机等待，减少“剧本式”行为。

- 无痕窗口与自动化表征
  - 始终使用 Chrome 原生无痕 UI：launch_persistent_context + --incognito + system chrome channel。
  - 去除 --enable-automation，并可附加 --disable-blink-features=AutomationControlled 等启动参数。
  - 不强行改 UA/时区/语言，保持系统自然环境；必要时可按账号覆写 user_agent。

- 路由与页面守卫
  - 仅初始 goto chatgpt.com；随后通过页面按钮推进。
  - 仅允许 chatgpt.com / auth.openai.com 上进行后续动作；若落入 platform.openai.com/signup 风控页，立即失败并记录截图。

- 选择器与中文兼容
  - 注册按钮：优先 [data-testid='signup-button']，后备“免费注册/Sign up”。
  - 邮箱输入：若输入框可见则直接填写；否则点击“Continue with email/使用电子邮件继续/用邮箱继续”。
  - 提交按钮：Continue/继续/下一步/完成。

- 并发与风控
  - 同一 IP 下建议单并发（max_concurrency_per_ip: 1），并设置起始错峰 stagger_start_sec。
  - 可为不同账号配置不同代理；但仍需遵守站点策略。

排障
- 遇到挑战长时间不消失：在可视化窗口中手动完成，再观察流程是否继续；必要时提高 cloudflare.max_wait_sec。
- 频繁命中风控页：降低并发与启动节奏，检查代理质量；确认选择器是否仍然匹配当前页面文案。
- OTP 长时间未到：提高 otp.poll_timeout_sec，检查凭据是否有效，或在 /emails 相关接口查看邮件是否入垃圾箱。

声明
- 本项目不提供验证码绕过能力，也不保证能在任何环境中稳定注册。
- 使用者应确保合法、合规地使用本项目，对其行为与后果负责。


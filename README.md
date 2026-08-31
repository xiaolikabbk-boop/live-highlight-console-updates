# 直播录制剪辑中控台更新仓库

这是公开的纯程序代码更新仓库。仓库和 Release 更新包只包含中控台程序文件。

永不提交或更新：

- 直播间数据库及业务记录
- GPT、DeepSeek 等 API 密钥和 `.env`
- 录像、候选素材、成片及导出记录
- Whisper 模型、Python 运行环境
- 录制器配置、直播间 URL 配置及 Cookie

## 发布新版本

1. 在本机运行 `scripts/sync_payload.ps1`，从工作区同步允许发布的程序文件。
2. 更新 `payload/VERSION`。
3. 提交并推送代码。
4. 创建并推送与版本一致的标签，例如 `v2026.08.31.1`。

GitHub Actions 会自动生成 `live-highlight-update.zip` 并创建 Release。客户端双击“检查并安装更新.bat”即可安装。


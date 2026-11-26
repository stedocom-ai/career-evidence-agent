# 邮箱草稿与预览：先用 Gmail

当前阶段先准备本地邮件和原 PDF 附件预览。默认方式为 `gmail-smtp`，但预览不连接 Gmail，不需要登录、密码、两步验证或应用专用密码，也不需要打开 Outlook。只有以后显式执行 `mail-send` 才会发送。

正文默认使用姓名、公司和岗位组成的简洁模板，也可由你提供 UTF-8 文本，不自动调用 LLM 或编造经历。每封邮件只支持一个明确指定的收件人。

## 1. 先看虚构样例

在项目目录的 PowerShell 中运行，使用新的空目录：

```powershell
python -m career_agent mail-demo --output .local/mail-demo
```

演示生成本地草稿和附件预览，不登录邮箱、不发送邮件，也不调用 DeepSeek。

## 2. 保存自己的发件资料

以下均为虚构占位符，请替换为自己的完整邮箱、署名和原 PDF 路径。学校 Google Workspace 邮箱也可先保存资料做预览，能否通过 SMTP 发送要另行确认。

```powershell
python -m career_agent --db .local/career.db mail-profile --email 'your@gmail.com' --name '你的署名' --resume 'C:\path\resume.pdf' --transport gmail-smtp
```

这个命令只保存本地配置，不验证邮箱所有权或发送能力。附件须为不超过 2 MiB 的 PDF；本地预览无需额外 Python 依赖。

## 3. 创建、查看和列出草稿

收件人应来自你核对过的招聘信息。下面地址仅作演示，不是招聘邮箱：

```powershell
python -m career_agent --db .local/career.db mail-draft --to 'hr@example.com' --company '示例公司' --position 'AI Agent 实习生'
```

命令返回 `mail_...` 草稿 ID。接下来替换占位符，查看同一份草稿：

```powershell
python -m career_agent --db .local/career.db mail-preview --id 'mail_替换为草稿ID'
python -m career_agent --db .local/career.db mail-list
```

默认预览保存在 `.local/mail/草稿ID/preview.txt` 和 `message.eml`。前者显示发件人、收件人、主题、正文、附件名和 SHA256；后者包含完整邮件及 PDF 附件，供本地查看，不会自动发出或创建 Gmail 网页草稿。

创建时可附加 `--job '岗位文档ID'`、`--source-url '招聘页面URL'` 保存来源，也可用 `--subject '自定主题' --body-file .\my-letter.txt` 提供正文。

## 4. 修改主题、正文或附件

将自己的正文保存为 UTF-8 文本，再修改指定草稿：

```powershell
python -m career_agent --db .local/career.db mail-edit --id 'mail_替换为草稿ID' --subject '新的主题' --body-file .\my-letter.txt
python -m career_agent --db .local/career.db mail-preview --id 'mail_替换为草稿ID'
```

附件保存的是创建时原文件的字节快照与哈希。修改源 PDF 后，如需更新尚未发送的草稿，显式运行：

```powershell
python -m career_agent --db .local/career.db mail-edit --id 'mail_替换为草稿ID' --refresh-resume
```

程序只允许修改 `draft` 或明确失败的 `failed` 状态。修改后重新预览，核对收件人、主题、正文和附件。上述所有命令都只处理本地资料，无需邮箱密码。

程序按发件邮箱、收件邮箱、公司和岗位名称识别同一申请；补填来源不创建第二封申请。同名岗位属于不同城市或批次时，将城市或批次写进岗位名。

资料默认保存在 `.local/mail-profile.json`、`.local/career.db` 和 `.local/mail/`，含姓名、邮箱、正文和原简历附件，未自动脱敏，不应提交仓库或共享。邮箱模板与预览不产生 DeepSeek token 费用。

## 以后需要发送时：可选 Gmail SMTP

**现在只做预览，可跳过本节。真实 Gmail 发送尚待账户联调验收。** 本版可使用 Gmail SMTP 配合应用专用密码；Google 更推荐“使用 Google 账号登录”，后续 OAuth 接入可以作为项目扩展，不能把它当成目前已实现的功能。

按 [Google 应用专用密码说明](https://support.google.com/accounts/answer/185833?hl=zh-Hans)，账户需开启两步验证并允许创建应用专用密码。学校或公司 Google Workspace、仅安全密钥验证或高级保护账户可能没有这个选项，不能保证每个账户都可用。学校禁止时，可改用有权限的个人 Gmail，或再实现账户允许的 OAuth 方案，不用普通 Google 登录密码代替。

若以后决定接入，在 Google 账户中创建专用于本项目的应用密码，仅在本机当前 PowerShell 输入。下面写法兼容 PowerShell 5.1，输入内容不会直接显示：

```powershell
$gmailTaskSecret = Read-Host '输入 Gmail 应用专用密码' -AsSecureString
$env:GMAIL_APP_PASSWORD = [System.Net.NetworkCredential]::new('', $gmailTaskSecret).Password
Remove-Variable gmailTaskSecret
```

预览不读取此变量；发送时才需要它。不要把密码发到聊天、正文、源码或 Git。`.env.example` 仅列配置项，程序不会自动读取 `.env`。SMTP 连接使用加密传输，服务地址和协议参考 [Google SMTP 文档](https://developers.google.com/workspace/gmail/imap/imap-smtp)。

未来明确决定发送并完成核对后，才使用下面命令；用完可清除当前会话中的密码：

```powershell
python -m career_agent --db .local/career.db mail-send --id 'mail_替换为草稿ID'
Remove-Item Env:\GMAIL_APP_PASSWORD
```

`accepted` 表示发送服务已接受请求，不证明招聘方已收到。`unknown` / `sending` 等状态表示结果待核对，程序不会盲目重发。先查看 Gmail 已发送邮件和退信，核对收件人、主题和时间；确认依据后才记录结果，不能把超时当作未发送：

```powershell
python -m career_agent --db .local/career.db mail-resolve --id 'mail_草稿ID' --result accepted --note '填写核对到的已发送记录'
python -m career_agent --db .local/career.db mail-resolve --id 'mail_草稿ID' --result not-sent --note '填写确认未发送的依据'
```

这两条是人工记录的可选结果，不自动证明送达，也不自动发送。已受理草稿不会再次发送；只有有依据确认未发送后，才考虑下一次显式操作。

## 其他已有适配：经典版 Outlook 与 Graph

这些方式均不是 Gmail 预览的前置条件，实际账户和发送也需单独验收。

- **经典版 Outlook**：用 `mail-profile ... --transport outlook-desktop` 保存配置，先在经典版 Outlook 添加发件邮箱，确认能手动发信，再用 `mail-check` 检查账户。发送使用 [SendUsingAccount](https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.sendusingaccount) 指定账户；`accepted` 只表示交给本地发件队列。新版 Outlook 不支持该对象模型，见[微软功能对照](https://support.microsoft.com/en-gb/outlook/getstarted/feature-comparison-between-new-outlook-and-classic-outlook)。
- **Microsoft Graph**：仅适用于个人 Microsoft 邮箱或有 Exchange Online 邮箱的 Microsoft 365 账户，不因使用学校域名就自动可用。需安装 `python -m pip install -e '.[outlook]'`，并用 `mail-profile ... --transport graph` 选择。

Graph 需按[微软应用注册指南](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)注册自己的应用，具备相应目录和注册权限，不能复制其他软件的应用 ID。个人邮箱选择支持个人 Microsoft 账户的类型；开启 **Authentication → Allow public client flows**，在 Graph **Delegated permissions** 添加 `Mail.Send` 和 `User.Read`，记录 Application (client) ID，不创建 client secret。组织策略可能要求管理员审批。

```powershell
python -m career_agent --db .local/career.db mail-login --client-id '自己的应用UUID' --tenant consumers
```

个人账户使用 `consumers`，组织账户可用 `organizations`，兼容两者的应用可用 `common`。按本次生成的网址和设备码完成[微软网页登录授权](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)，不分享密码或设备码。也可设置 `OUTLOOK_CLIENT_ID` 和 `OUTLOOK_TENANT`；Windows DPAPI 加密缓存保存在 `.local/outlook-cache.bin`。

Graph 的 HTTP `202` 仅表示微软受理，非处理完成或收件方送达，见[发送接口说明](https://learn.microsoft.com/en-us/graph/api/user-sendmail)。所有适配共享本地草稿与状态防重发机制。

# Career Evidence Agent

一个帮助求职者分析岗位要求与个人经历证据的学习项目。输入简历、项目材料和岗位描述（JD），Agent 调用本地检索工具，输出附原文引用的分析报告，以及对应的补强任务。

首版采用 Python、SQLite FTS5、模型工具调用协议和结构化报告校验。目的是把一次任务从输入、工具执行、错误反馈到结果留痕完整跑通，便于理解、演示和迭代。

## 项目贡献

陈昭晖负责使用场景与需求方向、DeepSeek API 配置和真实样例运行。实现代码主要在 Codex 辅助下生成，运行记录与报告质量由工具协助核对。当前是用于学习和展示的本地原型，已完成一份虚构岗位样例的真实模型工具调用验证。

## 先运行本地演示

需要 Python 3.10 或更新版本。在项目目录打开 PowerShell：

```powershell
python -m career_agent demo --output demo-output
```

文本演示仅使用 Python 标准库，无需 API key。查看生成的 `demo-output/report.md`、`report.json`、`trace.jsonl` 和 `run.json`。

**演示使用 ScriptedModel，调用顺序和回答由代码编排。** 它验证工具接口、检索、报告校验和运行记录，不代表真实大模型的规划或推理能力。`examples/` 中的候选人和岗位全部虚构。

运行测试：

```powershell
python -m unittest discover -s tests -v
```

## 使用自己的材料

TXT、Markdown 需为 UTF-8 编码。PDF 导入需要额外安装可选依赖：

```powershell
python -m pip install -e '.[pdf]'
```

PDF 只提取已有文本，不做 OCR；扫描件需要先转为可选中的文字。文档有大小和正文长度上限，超限会给出错误。

```powershell
python -m career_agent --db .local/career.db ingest .\examples\resume.md --kind resume
python -m career_agent --db .local/career.db ingest .\examples\project.md --kind project
python -m career_agent --db .local/career.db ingest .\examples\job.md --kind job
python -m career_agent --db .local/career.db documents
```

`ingest` 返回文档 ID。记录岗位材料的 `document_id`，下一步将它传给 `--job`。可用 `--title "自定义标题"` 设置来源名称。

## 接入真实模型

如果你第一次使用 API，先看 [零基础 API 接入指南](API_GUIDE.md)。指南提供 DeepSeek 的当前配置示例；也可以换用其他支持相同工具调用协议的服务商。

需要支持 Chat Completions 函数调用协议的 API。模型名称和地址由所选服务商提供；兼容性需要实测，不能仅凭“OpenAI 兼容”字样判断。

在当前 PowerShell 会话设置环境变量：

```powershell
$env:AGENT_BASE_URL = 'https://api.provider.example/v1'
$env:AGENT_MODEL = 'your-tool-calling-model'
$env:AGENT_API_KEY = 'your-api-key'
python -m career_agent --db .local/career.db analyze --job '替换为岗位文档ID' --output .local/report --max-turns 12
```

上面的地址和模型是占位符，需替换为真实配置。`.env.example` 仅说明配置项，程序不会自动读取 `.env`。不要把 key 写入源码或提交到 Git。

真实模式会把 JD、检索到的简历与项目片段及对话上下文发送给配置的模型服务商。可先用虚构样例验证，再选择自己愿意交给服务商的材料。本地数据库和运行记录也可能包含这些材料，默认目录已加入 `.gitignore`。报告与 trace 会保存引用和工具返回的原文，不做自动脱敏。

缺少配置或请求失败时，`analyze` 会报错。首版已于 2026-11-26 使用 `deepseek-flash` 跑通虚构样例，覆盖模型选工具、检索、错误恢复和报告提交。查看 [验收记录](VALIDATION.md)。

## Agent 如何工作

```text
导入材料 -> 按页分块 -> SQLite 关键词索引
                              |
JD + 任务 -> 模型选择工具 -> 程序校验并执行 -> 工具结果返回模型
                 ^                                  |
                 +-------- 继续查证或修正 -----------+
                              |
                    finish_report 提交结果
                              |
                   引用校验 -> 报告 + trace
```

真实模式下，模型决定调用哪个工具、用什么查询词、是否继续查证以及何时提交结果；程序负责执行与校验。它与把全部材料拼进一次提示词的区别，体现在可观察的工具选择和结果反馈循环。演示模式只展示协议和编排。

| 工具 | 用途 |
| --- | --- |
| `list_documents` | 查看已导入资料及其类别 |
| `read_job` | 读取指定岗位全文 |
| `search_evidence` | 在简历和项目材料中查找证据，排除 JD |
| `read_evidence` | 根据资料块 ID 复查原文 |
| `finish_report` | 提交结构化报告，校验失败后获取错误反馈 |

工具只能访问已导入的资料，不接受任意文件路径或任意 shell 命令。文档内容视为不可信输入，提示词要求模型将其中的指令当作材料处理；这不能证明模型完全抵抗提示注入。

## 报告与运行记录

每条岗位要求分为“有材料支持”“部分材料支持”或“材料未体现”。“材料未体现”表示当前输入未提供证据，不等于候选人不会。

校验器要求：岗位要求引用自 JD；支持性结论包含当前运行已检索到的证据 ID；证据原文必须真实出现在对应资料块中；行动任务关联报告中的岗位要求，并给出交付物。

**引用存在不等于语义成立。** 程序可拒绝不存在的 ID、伪造原文和结构错误，但不能自动证明每个解释都被引用充分支持。报告仍需人工核对，不输出“匹配度 93%”一类缺少依据的分数。

| 输出文件 | 内容 |
| --- | --- |
| `report.md` | 便于阅读的报告与来源引用 |
| `report.json` | 通过校验的结构化结果 |
| `trace.jsonl` | 模型响应、工具执行和反馈等运行事件 |
| `run.json` | 运行模式及结束状态等信息 |

调用轮数受 `--max-turns` 限制；工具调用与网络请求也受程序限制。达到上限或校验持续失败时会停止并报告原因，不将失败输出冒充成功报告。

## 项目结构

```text
career_agent/   CLI、模型接入、工具循环、存储、报告校验
examples/       可公开的虚构样例
tests/          不消耗 API 的确定性测试
LEARNING.md     逐步理解和修改项目的练习
```

检索采用 FTS5 关键词检索和 BM25 排序，预处理英文词与中文二元片段。它不使用 embedding，也不做语义检索；同义词、跨语言表述和中文分词仍可能漏检。

首版按单用户、本地资料、单份 JD 分析设计。岗位抓取、自动投递、向量检索、跨运行的模型记忆和多用户隔离属于后续扩展。自动化测试主要验证工程行为，真实模型的引用质量、需求覆盖和抗注入表现需要另行评估。

## 后续质量评估

1. 阅读现有真实运行记录，核对模型调用的工具及报告结论。
2. 更换查询表述，观察证据不足时是否追加检索，并人工核对引用与结论。
3. 准备至少 5 条不同 JD，人工标注要求与证据，记录需求覆盖率、无依据结论数和失败原因。
4. 改进最明显的一类问题，再用同一组案例比较，记录真实结果。

作品介绍可以引用已完成的单样例验收。若要报告稳定性或准确率，先在多组样例上收集结果。

协议与实现参考：[OpenAI 函数调用文档](https://developers.openai.com/api/docs/guides/function-calling)、[SQLite FTS5 文档](https://www.sqlite.org/fts5.html)。

## 先准备 Gmail 邮件预览

邮箱模块可以保存发件资料，为一个明确指定的岗位收件人生成本地邮件和原 PDF 附件预览，并用 `mail-edit` 修改主题、正文或附件快照。正文默认使用简洁模板，也可以自备 UTF-8 文本；它不自动调用 LLM 生成经历。

当前先只做预览，默认配置 `gmail-smtp`，无需登录邮箱或提供密码，也不用打开 Outlook。可先运行 `python -m career_agent mail-demo --output .local/mail-demo` 查看虚构样例；输出目录须为新的空目录。自己的资料按 `mail-profile → mail-draft → mail-preview / mail-edit / mail-list` 操作，详见 [邮箱预览与可选发送指南](MAIL_GUIDE.md)。

以后明确选择发送时才配置 Gmail 应用专用密码，账户需开启两步验证并允许该功能；学校 Google Workspace 可能限制使用，真实发送仍待验收。只有显式运行 `mail-send` 才会外发。每次只支持一个收件人，PDF 上限为 2 MiB，保存原附件快照和哈希；受理不代表 HR 收到，状态不明不会盲目重发。经典版 Outlook 与 Graph 保留为可选适配。

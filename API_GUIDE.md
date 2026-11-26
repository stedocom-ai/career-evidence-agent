# 第一次给 Agent 接上大模型

你可以先运行 README 里的本地演示，不需要注册、充值或设置密钥。演示让你理解程序怎样工作；接入 API 后，才由真实模型选择工具并生成分析。

## API 在这个项目中做什么

API 是程序向另一个服务提出请求的接口。你在网页聊天时，是自己输入内容；这个项目通过 API，让 Python 程序把任务和工具说明发送给模型服务，再接收回答。

一次工具调用通常这样进行：

1. 程序发送任务，例如“分析这份岗位描述需要哪些经历证据”。
2. 模型返回结构化请求，例如调用 `search_evidence` 搜索 `Python`。
3. Python 程序执行搜索，把真实结果返回给模型。
4. 模型继续查证或提交报告，程序检查引用后保存结果。

模型返回的是调用请求，真正执行检索的是我们的程序。这也是你需要理解和能够修改的 Agent 核心。

数据库是资料仓库；模型负责提出下一步；我们的程序负责调用模型、执行工具、反馈结果、校验和限制循环。DeepSeek 在 API 响应中给出工具调用请求，程序读懂后执行；DeepSeek 不会主动连接你的电脑或直接查询数据库。

## 先认识五个词

| 名称 | 作用 | 本项目中的例子 |
| --- | --- | --- |
| API | 程序与模型服务沟通的入口 | Python 发出一次 HTTP 请求 |
| API Key | 服务商识别账户和计费的凭证，类似密码 | `AGENT_API_KEY` |
| Base URL | 模型服务的地址 | `https://api.deepseek.com` |
| Model | 请求使用的模型名称 | `deepseek-flash` |
| Token | 模型处理文本与计费的单位 | 输入和输出分别累计 |

Token 不是固定等于一个汉字或一个英文单词。一次 Agent 任务常有多轮模型请求，每一轮的输入可能包含之前的对话和工具结果。

## 首次接入示例：DeepSeek

按 2026 年 11 月 25 日核对的[官方模型说明](https://api-docs.deepseek.com/quick_start/pricing/)，`deepseek-flash` 支持工具调用，提供兼容本项目的接口。这里用它作为学习起点；真实调用结果仍需在你的账户和网络环境下验证。

1. 打开 [DeepSeek 开放平台](https://platform.deepseek.com/)，注册或登录。
2. 在平台的 API Key 管理页面创建密钥，按平台提示保存。
3. 查看账户的 API 可用额度；是否有赠送额度以账户实际显示为准，需要时按自己的预算充值。
4. 回到项目目录，在 PowerShell 设置下列变量。将密钥占位符替换为自己的值，**不要把密钥发到聊天、截图或 Git 仓库**。

```powershell
$env:AGENT_BASE_URL = 'https://api.deepseek.com'
$env:AGENT_MODEL = 'deepseek-flash'
$env:AGENT_API_KEY = '在这里填自己的密钥'
$env:AGENT_EXTRA_BODY = '{"thinking":{"type":"disabled"}}'
```

这些环境变量只在当前 PowerShell 会话中生效；换一个终端时需要重新设置。程序从环境变量读取配置，不需要安装 OpenAI SDK，也不需要修改 Python 源码。

DeepSeek 的默认设置包含思考模式。首次联调按[官方工具调用示例](https://api-docs.deepseek.com/guides/tool_calls/)关闭它，减少需要排查的参数。`AGENT_EXTRA_BODY` 是可选的服务商参数；更换服务商后，应按对方文档调整或清除，不能用它覆盖模型、消息或工具定义。

## 跑第一次真实分析

这里的“真实”，指使用真实的 DeepSeek 模型选择工具和生成分析。简历和岗位仍然可以是虚构样例。`demo` 的下一步由脚本预先写好；`analyze` 的下一步由真实模型提出，再由程序检查并执行。

这部分分为两件事：先把材料存进本地数据库，再指定一份岗位，让模型开始分析。

**第一步：在项目文件夹打开 PowerShell。**

项目文件夹是同时包含 `README.md`、`pyproject.toml`、`career_agent` 和 `examples` 的文件夹。后续命令都在这个文件夹、同一个 PowerShell 窗口中执行。前文设置的 API 环境变量也需要在这个窗口中有效。

**第二步：导入三份材料。这一步不调用模型。**

逐行运行下面的命令。输入的是仓库自带的虚构样例：

```powershell
python -m career_agent --db .local/career.db ingest .\examples\resume.md --kind resume
python -m career_agent --db .local/career.db ingest .\examples\project.md --kind project
python -m career_agent --db .local/career.db ingest .\examples\job.md --kind job
python -m career_agent --db .local/career.db documents
```

| 命令部分 | 意思 |
| --- | --- |
| `python -m career_agent` | 启动我们写的 Python 程序 |
| `--db .local/career.db` | 使用项目里的这份本地数据库；不存在时会创建 |
| `ingest` | 把后面的文件读取、分块并存入数据库 |
| `--kind resume / project / job` | 标记它是简历、项目材料或岗位描述 |
| `documents` | 列出已导入材料及其编号 |

前三行相当于把三份资料放进资料柜；第四行查看柜子里的目录。导入成功会显示 `document_id`，它是本程序给材料生成的编号，不是招聘网站的岗位编号。相同内容重复导入不会增加副本。

当前自带样例中的岗位会得到下面这条目录记录：

```json
{
  "document_id": "doc_a2f68e53b66a8f7670c2",
  "title": "job.md",
  "kind": "job",
  "chunk_count": 1
}
```

**第三步：已配置 API 后，启动真实模型分析。**

如果使用未修改的自带样例，可以直接运行：

```powershell
python -m career_agent --db .local/career.db analyze --job doc_a2f68e53b66a8f7670c2 --max-turns 12
```

这句话的意思是：“请使用这份数据库，分析编号为 `doc_a2f68e53b66a8f7670c2` 的岗位，最多向模型请求 12 轮。”更换岗位材料后，要用 `documents` 列出的新岗位编号替换它。简历与项目材料由模型通过检索工具查找，无需在这条命令里逐个填写。

运行后，程序会发送任务和工具说明给 DeepSeek。模型可能先请求读取岗位，再请求搜索 Python、Docker 等证据；程序执行后把结果发回，模型可以继续检索或提交报告。具体顺序由模型选择，并受程序的校验和次数上限约束。

这是一段命令行程序，结果会保存成文件。成功时终端会显示“真实模型运行完成”和“报告与记录”的绝对路径。不写 `--output` 时，程序自动为每次运行创建新的输出目录，方便重复尝试。

- 先打开输出目录中的 `report.md`，看分析结论和引用。
- 再看 `trace.jsonl`，每一行是一条运行记录，检查调用了哪些工具、用了什么参数、返回了什么材料。
- `run.json` 的 `status` 为 `completed`，表示程序完成了报告校验；仍要自己检查引用是否充分支持结论。

如果还没设置密钥，`analyze` 会提示缺少 `AGENT_API_KEY` 等环境变量。前面导入的材料已保存在本地；设置好后重新运行分析即可，不必再次导入。不要把密钥发送到聊天里。

如果失败，保留不含密钥的错误信息，区分配置、网络、额度、协议和报告校验问题。`analyze` 在失败时会停止并给出错误。项目的首个成功案例见 [验收记录](VALIDATION.md)。

## 费用怎么理解

以核对当日的[官方价格表](https://api-docs.deepseek.com/quick_start/pricing/)为准，`deepseek-flash` 高峰时段未命中缓存的输入价为每百万 token 0.30 美元，输出价为每百万 token 1.20 美元；非高峰价格为对应高峰价格的一半。价格可能调整，以调用时平台显示为准。

计算一次任务费用时，要把**所有模型轮次**的输入和输出分别累加，再按实际价格计费。任务中反复发送的上下文也可能计入输入；缓存、时段和服务商返回的用量会影响结果。不要将一次用户提问理解为固定价格的一次调用。

先以虚构短材料、小批量案例调试，并查看平台用量。项目的轮数上限用于限制运行，不代表服务商费用上限，也不保证每次调用都成功或费用固定。

官方入口：[DeepSeek API 文档](https://api-docs.deepseek.com/)、[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)。

# Victorique 入门

本文供不写代码的数学用户使用，内容包括：在 Windows 上安装 Victorique 0.2.0a1，登录模型服务，用网页界面运行题目，查看结果，停止和继续运行，以及运行记录的位置。命令均在 Windows PowerShell 中执行。

本版本为 Alpha 版，只在 Windows 10 和 Python 3.11 上测试过。英文说明见 [README](../README.md)。

## 功能

Victorique 调用语言模型，为一道数学题生成一份证明，再用两次单独的模型调用检查这份证明：

- 题面检查：判断证明所证的命题是否与题面完全一致；
- 证明检查：逐步检查整份证明。

两项检查都通过，才交付证明；否则不交付，并给出原因和最佳草稿。每次运行都有调用次数和时间的上限。全部记录保存在本机。

## 与直接调用一次模型的比较

在基准题上使用前沿模型时，Victorique 解出的题数不多于直接调用一次同一模型。2026 年 11 月的测试使用 GPT-6 Astra（经 Codex CLI 调用，最高推理强度），72 道奥赛级证明题各做 4 轮，共 288 个题轮（一道题做一轮为一个题轮）。评分器与求解模型属于同一模型族，因此结果为暂定值。

| 方式 | 解出的题轮 |
| --- | --- |
| 直接调用一次，结果不经检查直接交付 | 276 |
| full 策略：先规划，再分部分求解、批评和修复，最后做两项检查 | 277 |
| single 策略（默认）：一次求解加两项检查 | 271 |

single 有 5 份首稿评分为 7/7，但没有交付：

- 3 份被两项检查拒绝，都来自题目 NEM-G1。这些首稿用反例否定了题面中的命题，两项检查的报告都写明反例成立；但按检查规则，只有证明题面所述命题的稿件才能通过。
- 2 份被旧的预检查拦下。预检查查找未完成的文本；它的标记规则已在 0.1.0b2 中修正，本版本的 single 策略不运行预检查。

开放问题上的比较没有测量。

与直接调用相比，Victorique 增加了以下内容：交付前的两项检查；不交付时给出原因；完整的本地记录；调用次数和时间的上限。两项检查本身也是模型调用，结论可能有误。误收指检查通过了错误的证明，误拒指检查拒绝了正确的证明。测得的误收率和误拒率见 [README 的“Known limitations”一节](../README.md#known-limitations)。

## 安装

1. 安装 Python 3.11 或更新版本：https://www.python.org/downloads/
2. 从发布页面 <https://github.com/Yyuzrah/Solver_Victorique/releases> 下载 `victorique-0.2.0a1-py3-none-any.whl`，保存到“下载”文件夹（`$HOME\Downloads`）。
3. 在 PowerShell 中依次执行：

```powershell
py -3.11 -m venv "$HOME\victorique-venv"
& "$HOME\victorique-venv\Scripts\python.exe" -m pip install "$HOME\Downloads\victorique-0.2.0a1-py3-none-any.whl"
& "$HOME\victorique-venv\Scripts\victorique.exe" --version
```

- 第一条命令在用户文件夹中创建虚拟环境 `victorique-venv`。Victorique 只安装在这个环境中，本机其他 Python 环境不受影响。
- 使用更新版本的 Python 时，把 `-3.11` 改为对应的版本号，例如 `-3.12`。
- 最后一条命令输出 `0.2.0a1`，表示安装完成。

使用 Codex、Claude Code 等编程助手时，也可以把 [README 的“With your coding agent”一节](../README.md#with-your-coding-agent)中的安装指令交给编程助手，由它完成安装和一次练习运行。

## 登录模型服务

正式运行前，需要安装下列命令行工具之一，并由本人登录。Victorique 用这个工具调用模型，不索取密码或密钥，也不打开工具的登录文件或密钥文件。练习运行不调用模型，不需要登录。

### Codex CLI（OpenAI 的模型）

```powershell
codex login
```

按官方说明（https://learn.chatgpt.com/docs/codex/cli）安装 Codex CLI 后，执行上面的命令，按提示登录。只安装 Codex 桌面应用时，系统中不一定有 `codex` 命令。

### Claude Code CLI（Anthropic 的模型）

```powershell
claude
```

按 Anthropic 的文档（code.claude.com）安装 Claude Code 后，执行上面的命令，输入 `/login`，选择 Anthropic Console 账户（按 API 用量计费）。

Anthropic 的法律与合规页面（https://code.claude.com/docs/en/legal-and-compliance，2026-11-19 核对）规定：

- Claude.ai 订阅登录（Free、Pro、Max、Ultra）用于 Anthropic 自家应用的普通使用；
- 开发产品的开发者应使用 API 密钥；
- 第三方开发者不得代用户使用订阅凭据转发请求。

Victorique 启动 Claude Code 时，从它的环境变量中删除 `ANTHROPIC_API_KEY`，所以 Claude Code 使用 `/login` 保存的登录。Victorique 无法得知 Claude Code 使用哪一种登录。

## 启动网页界面

```powershell
New-Item -ItemType Directory -Force "$HOME\victorique-work" | Out-Null
Set-Location "$HOME\victorique-work"
& "$HOME\victorique-venv\Scripts\victorique.exe" gui
```

在存放运行记录的文件夹中启动界面，上例为 `$HOME\victorique-work`。

- 命令打印网页地址，并在默认浏览器中打开网页。默认地址为 `http://127.0.0.1:8765/`。8765 端口被其他程序占用时，界面改用其他空闲端口，以打印的地址为准。
- 使用界面期间，保持这个 PowerShell 窗口打开。按 Ctrl+C 停止界面。关闭网页或停止界面，都不会停止进行中的运行。
- 网页只接受本机连接。网页和字体都来自本机。

另选运行文件夹：

```powershell
& "$HOME\victorique-venv\Scripts\victorique.exe" gui --runs-dir "D:\proofs\runs"
```

运行文件夹无法创建或写入时，命令打印一行说明（文件夹路径、原因，以及可用 `--runs-dir` 另选文件夹），以退出码 2 结束。这时用上面的命令另选文件夹。

## 练习运行

练习运行使用内置的固定示例回答，不调用模型，用于确认安装和界面可以使用。

1. 打开“New problem（新建题目）”页。
2. 在“Problem statement”框中粘贴：`Prove that n + 0 = n for every integer n.`
3. 在“Model service”中选择“Practice run (no model is called)”，其他选项保持默认值。
4. 点击“Start”。

运行在几秒内结束，共 3 次调用，状态显示为“Proof delivered（已交付）”。示例回答只用于检查软件，其中的证明没有数学内容。

## 正式运行

1. 在“Model service”中选择“Codex CLI”或“Claude Code CLI”。
2. 粘贴题目。标题可以不填，只用于在运行列表中查找运行。
3. “Approach（方法）”保持默认的“Solve once and check”，即 single 策略：一次求解，加题面检查和证明检查。
4. 点击“Start”。

默认上限：

| 项目 | 默认值 |
| --- | --- |
| 调用次数 | 12 次 |
| 整次运行的时间 | 2 小时 |
| 每次调用的时间 | 600 秒 |

- single 策略共 3 次模型调用。某次调用达到时间上限时，默认最多重发一次，每次重发另计 1 次调用。重发的条件见 [README 的“Known limitations”一节](../README.md#known-limitations)。
- 每次正式运行都消耗所选模型服务的额度或 API 费用。
- 题目较长时，可以选择“Full workflow”（full 策略）。full 策略的调用次数较多，2026 年 11 月的测试中平均每个题轮 8.06 次。

## 查看结果

“Runs（运行记录）”页列出运行文件夹中的全部运行，并自动刷新。点击一个运行，打开运行页面。运行页面显示：

- 状态：Running（运行中）、Proof delivered（已交付，即两项检查在同一最终版本上都通过）、No proof delivered（未交付）、Stopped by you（已停止）、Failed（失败）或 Stopped before finishing（中途停止）；
- 证明：已交付时显示“Delivered proof（已交付证明）”；未交付时显示最佳草稿，标注“最佳草稿（未交付）”；
- 每项检查的结论和发现的问题，每个问题标有严重程度（界面中为 Fatal、Serious 或 Minor）；
- 未交付的原因；
- 每次模型调用的步骤、状态、用时和 token 数，以及调用在时间轴上的位置；
- 文件下载：`proof.md`、`summary.json`、`calls.jsonl` 等。运行结束后，还可以下载整个运行文件夹的 zip 文件。

## 停止和继续运行

### 停止

```powershell
& "$HOME\victorique-venv\Scripts\victorique.exe" stop "$HOME\victorique-work\runs\run-20261119-093000-a1b2c3"
```

把 `run-20261119-093000-a1b2c3` 换成实际的运行名称。也可以在运行页面点击“Stop run”；页面先显示确认说明，确认后停止。

停止后：

- 运行不再发出新的模型调用，并放弃进行中的调用；
- 运行结束，不交付证明；
- 最佳草稿保留在 `proof.md` 中。

状态先显示“Stopping（停止中）”，运行结束后显示“Stopped by you（已停止）”。

### 继续

```powershell
& "$HOME\victorique-venv\Scripts\victorique.exe" resume "$HOME\victorique-work\runs\run-20261119-093000-a1b2c3"
```

适用于进程已结束、但没有写出 `summary.json` 的运行，例如计算机重启后的运行。这类运行的状态为“Stopped before finishing（中途停止）”，运行页面显示“Resume”按钮。点击“Resume”或执行上面的命令后，运行在原有的调用次数上限和截止时间内继续。

- 运行已经结束时，resume 不发出新的调用。
- 运行已有停止请求时，resume 使运行立即结束，不发出新的调用。

## 运行记录的位置

运行文件夹默认为启动 `victorique gui` 时所在文件夹下的 `runs`，上例中为 `$HOME\victorique-work\runs`。用 `--runs-dir` 可以指定其他文件夹。

每个运行有自己的文件夹，名称形如 `run-20261119-093000-a1b2c3`：开始时的 UTC 日期和时间，加 6 位十六进制随机字符。主要文件：

| 文件 | 内容 |
| --- | --- |
| `proof.md` | 已交付的证明；未交付时为最佳草稿 |
| `summary.json` | 结果、结束原因、调用次数、用时和 token 数 |
| `calls.jsonl` | 每次模型调用一行，含开始和结束时间、状态和 token 数 |
| `invocation.json` | 运行的设置 |

其余文件供继续运行使用，请勿修改。

界面自己的文件在 `runs\.victorique-gui\` 下与运行同名的文件夹中，包括题目 `problem.md` 和命令输出 `console.log`。

## 数据去向

- 题目、草稿和检查报告只发送给所选的模型服务，由它的命令行工具发送。
- 运行记录只保存在本机。Victorique 不向其他地方发送数据。
- 命令行工具在 Victorique 的输入之外向服务发送的内容（例如工具自己的指令、工作环境信息或使用数据），由工具及其服务决定。

详见 [README 的“Where your data goes”一节](../README.md#where-your-data-goes)。

## 其他文档

| 文档 | 内容 |
| --- | --- |
| [README](../README.md)（英文） | 功能、安装、模型服务、数据去向和已知限制 |
| [使用说明](usage.md)（英文） | 网页界面的详细步骤、命令行用法、各求解方法和设置 |
| [网页界面说明](gui.md) | 每个字段和状态的说明，文末有中文说明 |
| [INSTALL.md](../INSTALL.md)（英文） | 交给编程助手的分步安装指令 |
| [研究结果](study.md)（英文） | 2026 年 11 月测试的完整结果表 |

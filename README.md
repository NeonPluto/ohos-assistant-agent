# ohos-assistant-agent

基于 **LangGraph** 的多会话编码助手，通过 **Gradio** 提供 Web 界面；内置 **Skill** 加载（扫描仓库内 `skills/**/SKILL.md`）、安全范围内的文件读写与目录列举，会话历史持久化在本地 `.sessions/` 目录。垂域能力以 **HarmonyOS** 相关技能为主（见 `skills/harmonyos-tool/`）。

## 环境要求

- **Python：3.13**（推荐与当前依赖一致；其他版本未做兼容性保证）
- 可访问的 **OpenAI 兼容 API**（如阿里云 DashScope、DeepSeek 等，通过环境变量配置 `base_url` 与 `api_key`）

## 仓库结构（概要）

| 路径 | 说明 |
|------|------|
| `agents/s_full_langgraph_multisession_fs.py` | 主程序：LangGraph Agent + Gradio WebUI |
| `skills/` | Skill 定义（含 frontmatter 的 `SKILL.md`） |
| `.env` / `.env.example` | 模型与 API 配置（`.env` 需自行创建，勿提交密钥） |
| `.sessions/` | 多会话 JSON 存储（默认由程序创建；可按需加入 `.gitignore`） |
| `requirements.txt` | Python 依赖锁定列表 |

辅助脚本（与主 WebUI 独立）：

| 路径 | 说明 |
|------|------|
| `skills/harmonyos-tool/DomainSpecificConceptMining/executor.py` | 将模型输出的 JSON 规范化并写入 `data/`、`knowledge/`（需按脚本说明传入 `--input-file` 或 `--input-json`） |

## 配置说明（`.env`）

复制示例文件并按实际服务填写：

```bash
cp .env.example .env
```

主要变量（与 `.env.example` 一致）：

| 变量 | 说明 |
|------|------|
| `OPENAI_API_KEY` | API 密钥（必填，勿泄露） |
| `OPENAI_BASE_URL` | OpenAI 兼容接口地址，例如 DashScope 或 DeepSeek 的 `/v1` 根路径 |
| `MODEL_ID` | 模型名称（主程序 `ChatOpenAI` 读取此项；未设置时默认 `gpt-4o`） |
| `OPENAI_MODEL` | `.env.example` 中与服务商文档对应的别名，主入口脚本当前**未读取**；可与 `MODEL_ID` 填成同一模型名以便人工对照 |

可选：通过环境变量调整 Web 服务监听地址（见 `agents/s_full_langgraph_multisession_fs.py` 中 `main()`）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `S_FULL_LG_FS_HOST` | `127.0.0.1` | Gradio 绑定地址 |
| `S_FULL_LG_FS_PORT` | `8771` | 监听端口 |

启动成功后终端会打印类似：`WebUI running at http://127.0.0.1:8771`。

---

## macOS 安装与运行

### 1. 安装 Python 3.13

若尚未安装，可从 [python.org](https://www.python.org/downloads/) 下载 macOS 安装包，或使用 **Homebrew**：

```bash
brew install python@3.13
```

确认版本：

```bash
python3.13 --version
```

### 2. 进入项目目录并创建虚拟环境

```bash
cd /path/to/ohos-assistant-agent
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt
```

### 3. 配置环境变量并启动

```bash
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY 等

python agents/s_full_langgraph_multisession_fs.py
```

在浏览器打开终端提示的地址（默认 `http://127.0.0.1:8771`）。

### 4. 退出虚拟环境

```bash
deactivate
```

---

## Windows 安装与运行

### 1. 安装 Python 3.13

从 [python.org](https://www.python.org/downloads/windows/) 安装 **Windows installer (64-bit)**，安装时勾选 **“Add python.exe to PATH”**。打开 **PowerShell** 或 **cmd** 验证：

```powershell
py -3.13 --version
```

若 `py` 启动器可用，推荐使用 `py -3.13` 调用 3.13，避免与系统其他 Python 混淆。

### 2. 进入项目目录并创建虚拟环境

在 PowerShell 中：

```powershell
cd C:\path\to\ohos-assistant-agent
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
```

若在 **cmd.exe** 中：

```bat
cd C:\path\to\ohos-assistant-agent
py -3.13 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
pip install -r requirements.txt
```

> **PowerShell 执行策略**：若 `Activate.ps1` 被禁止运行，可先以管理员或当前用户执行：  
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

### 3. `uvloop` 与 Windows 说明

当前 `requirements.txt` 中包含 **`uvloop`**，该包主要面向 **Linux / macOS**，在 **Windows** 上安装常失败。若 `pip install -r requirements.txt` 在 `uvloop` 处报错，可采用其一：

- **临时做法**：用编辑器打开 `requirements.txt`，删除或注释掉 **`uvloop==...`** 这一行后重新执行 `pip install -r requirements.txt`。主程序使用 Gradio 自带服务，不依赖本机 `uvloop` 即可运行。
- **长期做法**：可在后续改为按平台拆分的依赖文件或使用 [PEP 508 环境标记](https://peps.python.org/pep-0508/) 仅在非 Windows 安装 `uvloop`（需改仓库依赖定义，此处不展开）。

### 4. 配置环境变量并启动

```powershell
copy .env.example .env
# 用记事本或 VS Code 编辑 .env，填入 OPENAI_API_KEY 等

python agents/s_full_langgraph_multisession_fs.py
```

浏览器访问终端输出的 URL（默认 `http://127.0.0.1:8771`）。

### 5. 退出虚拟环境

```powershell
deactivate
```

---

## 使用说明（WebUI）

1. **会话**：支持新建、切换、按会话 ID 切换；历史保存在 `.sessions/<session_id>.json`。
2. **Skill**：`skills` 目录下各 `SKILL.md` 的 `triggers` 若与用户输入前缀匹配，会自动触发 `load_skill`（**不含** frontmatter 中 `explicit_invoke_only: true` 的 Skill）。显式专用 Skill 须首行写 `/invoke_skill <name>` 换行后再写问题，或在 WebUI 选择「显式启用 Skill」；此类 Skill 不会出现在系统提示的可用列表中，且 `load_skill` 会被拒绝加载。
3. **文件工具**：Agent 仅能在**当前工作区（仓库根目录）**内读写与列举路径，请勿将仓库根设到不受信任的位置。

## 常见问题

- **无法连接模型**：检查 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`MODEL_ID` 是否与服务商文档一致；部分网关需指定具体模型名。
- **端口占用**：修改环境变量 `S_FULL_LG_FS_PORT` 为其他端口后重启。
- **从非仓库根目录运行**：`WORKDIR` 为进程当前工作目录，请始终在克隆后的项目根目录执行 `python agents/s_full_langgraph_multisession_fs.py`，否则 Skill 路径与文件沙箱可能不符合预期。

## 依赖说明

`requirements.txt` 中除 Web/Agent 所需外，还包含如 **Elasticsearch、FAISS** 等库，当前主入口脚本未直接使用；若你扩展 RAG 或其它服务，可继续沿用该清单或按需裁剪。

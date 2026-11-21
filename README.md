# Novel Forge

**Novel Forge** 是一个纯本地运行的长篇中文小说创作与修订工作台。采用 **FastAPI + LangGraph** 编排任务状态机，搭配 **React** 界面与 **SQLite** 本地持久化，覆盖从选题立项、长篇连载生成、写中防 AI 味与跨章记忆，到成书评估、评估后精修与多格式导出的完整闭环。

---

## 核心特性

- **闭环长篇写作流水线**
  - 选题立项 → 全书总纲（含本书风格宪法 `styleBible`）→ 连续章纲 → 逐章写作 → 写后确定性清洗（格式整理与跨章衔接去重）→ 审校与自动返修 → 原子提交正文与事实 → 全书终审与四重门禁。
  - **写中 AI 味门禁**：结合本地规则（破折号密度、心理引导套语、连续同主语、单段到底）与模型原文引证核实（P0/P1 阻断、P2 建议），当章不达标即自动返修。
- **Token 预算与跨章长记忆**
  - **写章输入预算器**：对渲染后的完整写章提示词执行 Token 上限保护（默认 16,384 估算 Token，预留 1,024），保护核心硬约束不被误砍，其余槽位按优先级句末自然截断并动态回补。
  - **长期记忆与卷归档**：随章节记录角色目标/情绪/立场/关系变化、硬约束、期限承诺与活跃支线；每满 10 章自动归档卷摘要，并对已完结支线、过期承诺及超 20 章已回收伏笔自动衰减。
- **断点续跑与模型弹性保护**
  - LangGraph 图状态与业务数据分别持久化在本地 SQLite，进程中断或重启后可从最近检查点无缝续跑，已提交章节与事实绝不重复写入。
  - **思考模型输出保护**：过滤 Gemini/Vertex 思考片段防泄露，截断时自动扩容重试（最高 65,536 tokens）且到顶即停不空转。
  - **Provider 熔断与可选降级**：按供应商与端点隔离熔断（连续 3 次 5xx/超时/网络故障进入冷却并支持单探测恢复）；默认严格拦截，也支持按需开启审校超次降级落库（`needs_review`）。
- **成书评估与评估后修订 v2**
  - 支持分段精读 + 深度评估，评估完成后可进入「评估后修订」：原稿审计 → 锁定全书台账（大报告自动分批与逐批缓存）→ 分卷隔离改写 → 硬检与独立终审 → 合稿交付。
  - 审校发现新跨章联动时支持局部增量升版，仅重跑受影响章节，保留其余批次缓存；内置「问总编」协作台用于故障诊断与人工定口径。
  - 支持导出 **TXT / Markdown / 番茄投稿 DOCX**。
- **多供应商与零密钥试用**
  - 支持 DeepSeek、OpenAI / OpenAI 兼容网关、Google Gemini、Google Vertex AI（支持多项目独立配置）与 Anthropic。
  - 内置 **Mock 模型**，无须配置 API Key 即可跑通完整创作与修订流程。

---

## 快速开始

### 环境要求

- **Python** 3.11+
- **Node.js** 用于首次构建或开发 React 前端。已有 `web-gemini/dist` 时，运行工作台只需 Python。

### 安装与构建（首次运行）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm --prefix web-gemini ci
npm --prefix web-gemini run build
```

### 启动工作台

```powershell
.\.venv\Scripts\python.exe -B -m novel_forge
```

打开浏览器访问 `http://127.0.0.1:3457`。Windows 也可直接双击 `start-novelforge.bat`（服务就绪后自动打开浏览器；若修改代码需要重启，请先关闭旧的命令行窗口再重新启动）。

### 前端开发模式

同时运行后端与前端开发服务器：

```powershell
.\.venv\Scripts\python.exe -B -m novel_forge
npm --prefix web-gemini run dev
```

---

## 模型配置

1. 打开界面中的「模型管理」，选择供应商并填写连接地址与 API 密钥（已保存密钥仅展示掩码，保存在本地 `data/config.json`）。
2. 支持目录拉取的供应商可直接同步可用模型；官方 Vertex API Key 连接可直接使用预设与手动登记的模型 ID，「Google Vertex AI」与「Google Vertex AI · 新项目」相互独立。
3. 建议为主力创作配置高质量模型，为辅助任务（`lightModel`，用于审校、字数校准与每 10 章卷归档）配置响应更快的模型，可显著降低长篇耗时。
4. 如仅需体验功能，可直接使用内置 **Mock 模型**。

---

## 架构与数据说明

FastAPI 在一个 Python 进程中运行 LangGraph、模型网关、评估与修订服务。数据存放在本地 `data/` 目录。

- `novel_forge/`：FastAPI 路由、LangGraph 写作工作流（`workflow.py`）、写作流水线（`pipeline.py`）、模型网关（`llm.py`）、成书评估（`evaluator.py`）、修订 v2（`revision_v2.py`）及总编（`editor_desk.py`）。
- `web-gemini/`：React + TypeScript + Vite 前端工作台。
- 本地 SQLite 存储：
  - `data/novel-forge.sqlite3`：作品任务、章节正文、规范事实与业务断点。
  - `data/workflow.sqlite3`：LangGraph 节点执行状态检查点。
  - `data/revision.sqlite3`：评估后修订原稿快照、台账版本、分批回执与候选稿。
  - `data/editor-desk.sqlite3`：「问总编」对话与故障诊断记录。

---

## 常用环境变量

所有增强功能均支持通过环境变量独立开关或回滚：

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `3457` | Web 服务监听端口 |
| `NOVEL_FORGE_HOST` | `127.0.0.1` | Web 服务监听地址 |
| `NOVEL_FORGE_DATA_DIR` | `./data` | 数据存储目录 |
| `NOVEL_FORGE_BACKGROUND_FEEDS` | `1` | 设为 `0` 可跳过后台热点/热梗刷新（离线环境推荐） |
| `NOVEL_FORGE_WRITE_AI_GATE` | `1` | 写中 AI 味门禁（设为 `0` 关闭） |
| `NOVEL_FORGE_CHAPTER_INPUT_BUDGET` | `16384` | 写章估算输入 Token 上限（设为 `0` 关闭） |
| `NOVEL_FORGE_DRAFT_QUALITY` | `1` | 风格宪法、写前反 AI 禁令与写后衔接清洗（设为 `0` 关闭） |
| `NOVEL_FORGE_LONG_MEMORY` | `1` | 跨章动态记忆与每 10 章卷归档（设为 `0` 关闭） |
| `NOVEL_FORGE_OUTPUT_GUARD` | `1` | 思考模型输出截断保护与自动扩容（设为 `0` 关闭） |
| `NOVEL_FORGE_PROVIDER_CIRCUIT` | `1` | Provider 连续故障熔断器（设为 `0` 关闭） |
| `NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED` | `0` | 审校返修耗尽时是否允许非致命文风问题降级为 `needs_review` |
| `NOVEL_FORGE_LEDGER_BATCH_THRESHOLD` | `30` | 修订台账超过该意见条数时自动分批生成（设为 `0` 退回单次） |
| `NOVEL_FORGE_REVISION_SOFT_FALLBACK` | `1` | 修订 v2 第三层局部安全降级（设为 `0` 关闭降级，保留格式容错与 3 次校验） |

---

## 测试验证

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_*.py' -v
```

测试覆盖 FastAPI 路由、LangGraph 阶段回环、进程重启续跑、写作质量控制、模型保护、评估与修订 v2、总编和 DOCX 导出。集成测试使用临时数据库、Mock 模型或本地模拟接口。

更多架构决策与长篇压测故障复盘见 [docs/ENGINEERING_NOTES.md](docs/ENGINEERING_NOTES.md)。

---

## 开源协议

本项目基于 [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE) 开源。任何对本项目的修改、二次分发或作为网络服务对外提供时，均须按 AGPL-3.0 协议开放完整源代码。

# 配置与供应商接入

> 上游是 [design.md](design.md) §6.1。

---

## 1. 配置优先级

从低到高，后面的覆盖前面的：

```
内置默认值  →  ~/.config/llm/keys.env  →  项目 .env  →  环境变量  →  命令行参数
```

**唯一例外**：密钥（API Key）**只**从环境变量读（含被 source 进环境的
`keys.env`），命令行**不提供** `--api-key` 参数 —— 那会让密钥进 shell history 和
`ps` 输出。

---

## 2. 密钥：真值集中在一处，项目内不存

本机约定（跨项目统一）：

```
~/.config/llm/keys.env      # 权限 600，永不进 git，由 ~/.zshrc / ~/.zprofile 加载
```

项目内**只允许**出现非敏感配置（`base_url`、`model` 名），且 `.env` 一律被
`.gitignore` 忽略。换 key 只改一处，新项目零配置。

### 2.1 当前本机已有的密钥

| 变量 | 状态 |
|---|---|
| `DEEPSEEK_API_KEY` | 存在（⚠️ 2026-09-15 曾实测返回 401，使用前请确认已补填有效 Key） |
| `OPENCODE_API_KEY` | 存在 |

### 2.2 缺密钥的行为

**启动时立即报错并退出，退出码 3。不进入任何阶段。**

```
错误：未找到 DEEPSEEK_API_KEY。
     请设置环境变量，或写入 ~/.config/llm/keys.env

     export DEEPSEEK_API_KEY=sk-...
     或
     echo 'DEEPSEEK_API_KEY=sk-...' >> ~/.config/llm/keys.env
```

不提示"将以离线模式继续"，也不回退到别的供应商 ——
这个工具的全部价值是产出可信的设计文档，缺模型就没有产出，硬跑毫无意义。

---

## 3. 供应商接入

协议：**OpenAI 兼容** `POST {base_url}/chat/completions`，`stream=true` + SSE。
`config.py` 按 `base_url` 判定该发哪些服务商专属参数。

本机已确认的常用地址（完整 40+ 家清单见 memory 知识库 `infra-llm-api`）：

| 供应商 | Base URL | 密钥变量名 |
|---|---|---|
| DeepSeek 官方 | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` |
| OpenCode Zen | `https://opencode.ai/zen/go/v1` | `OPENCODE_API_KEY` |
| Kimi / Moonshot（CN） | `https://api.moonshot.cn/v1` | `KIMI_API_KEY` |
| 智谱 GLM（CN） | `https://open.bigmodel.cn/api/paas/v4` | `GLM_API_KEY` |
| 通义 Qwen（CN） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `QWEN_API_KEY` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | — |
| OpenRouter | `https://openrouter.ai/api/v1` | — |
| Ollama | `https://ollama.com/v1` | `OLLAMA_API_KEY` |

**默认**：`base_url = https://api.deepseek.com/v1`，
`model = deepseek-flash`（DeepSeek 官方当前正名，见 §6 已知坑 3）。

---

## 4. 配置项

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-url` | `https://api.deepseek.com/v1` | 供应商地址 |
| `--api-key-env` | `DEEPSEEK_API_KEY` | 从哪个**环境变量名**读密钥（不是密钥本身） |
| `--model` | `deepseek-flash` | 默认模型 |
| `--model-strong` | 同 `--model` | 组阁 / 定稿用（这两步最吃能力） |
| `--model-fast` | 同 `--model` | 例行发言用 |
| `--panel-size` | auto | 人数，覆盖自动推断 |
| `--max-rounds` | `4` | 轮数硬上限 |
| `--budget-tokens` | 未设 | 总 token 硬上限 |
| `--concurrency` | `4` | 同阶段并发度 |
| `--context-dir` | 无 | 本地资料目录，按维度分配给各人设 |
| `--dimensions-file` | 无 | 追加自定义维度库（YAML） |
| `--personas-file` | 无 | 固定评审团（YAML），跳过 S1 生成 |
| `--out` | `.sparky/sessions/` | 会话根目录 |
| `--web` / `--no-web` | `--web` | 是否启动本地网页 |
| `--port` | `8765` | 网页端口（占用时**报错**，不静默换端口） |
| `--timeout` | `120` | 单次 LLM 请求超时（秒） |
| `--resume <session_id>` | 无 | 从已有会话续跑 |

### 4.1 关于 `--model-strong` / `--model-fast`

分组讨论里，**组阁与定稿**决定成败（人设造歪了全场白聊，文档写砸了前面白干），
而**例行发言**只是产出可被反驳的立场。分层用模型是省钱的主要手段。

默认三者相同，是为了让第一次运行的行为最简单可预测。

---

## 5. `.env` 里放什么

项目内 `.env` 只允许非敏感项：

```bash
# 可以
SPARKY_BASE_URL=https://api.deepseek.com/v1
SPARKY_MODEL=deepseek-flash

# 禁止（密钥只从环境变量读，见 §2）
# SPARKY_API_KEY=sk-...      ← 不要这样
```

---

## 6. 已知坑（来自本机实测，必须遵守）

1. **部分网关会拦截"脚本味"的 User-Agent。**
   Cloudflare 前置的端点对 `Python-urllib/3.14` 这类默认 UA 直接返回
   `403 + error code 1010`，而同样的请求体换 `curl/x.y` 或浏览器 UA 都是 200。
   → **`llm.py` 必须显式设置一个正常 UA**，不能依赖库的默认值。

2. **`GET /models` 能通，不代表 `POST /chat/completions` 能通。**
   两者可能挂在不同规则后面。验证连通性必须打**真正的推理端点**。

3. **模型正名与别名不等价。**
   DeepSeek 官方 `/models` 现在只返回 `deepseek-flash` 与 `deepseek-v4-pro`；
   旧名 `deepseek-chat` 仍返回 200，但响应里的 `model` 字段会回落成 `deepseek-flash`，
   且**不带 thinking 参数时的行为并不一致**。
   → 判断"这家现在有哪些模型"以 `/models` 为准，不要凭记忆或硬编码。

4. **同名 id 不等于同一个东西。**
   `deepseek-flash` 在 DeepSeek 官方和 OpenCode Zen 都存在，但那是两家各自的模型。
   切了 `base_url` 必须连模型名一起切。

5. **服务商专属参数必须按 `base_url` 判定。**
   `thinking` / `reasoning_effort` 只发给 DeepSeek；OpenCode 缺 `x-opencode-session`
   会报 `400 MissingSessionID`。发错参数不是被忽略，是会报错。

6. **推理模型要给足输出预算与超时。**
   响应里含大段 reasoning token（实测一次简单调用 `completion_tokens=106` 中
   有 `reasoning_tokens=90`）。`max_tokens` 给小了会被截断成半句话，
   而且**截断的 JSON 解析必然失败**。

7. **CN 与 Global 是不同域名，不能互换。**
   Moonshot 是 `.cn` / `.ai`，MiniMax 是 `minimaxi.com` / `minimax.io`，
   GLM 是 `open.bigmodel.cn` / `api.z.ai`，Qwen 是 `dashscope.aliyuncs.com` /
   `dashscope-intl.aliyuncs.com`。用错会认证失败或产生跨境计费。

8. **密钥不进 git、不进日志、不进事件流。**
   `llm-calls/` 下的请求快照在落盘前必须**擦除 Authorization 头**。

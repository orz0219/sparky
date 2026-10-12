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

### 2.1 本机密钥现状（2026-10-12 实测）

| 变量 | 来源 | 状态 |
|---|---|---|
| `DEEPSEEK_API_KEY` | `~/.config/llm/keys.env` | ❌ **401 Authentication Fails**（key 尾号 8313）—— 需要重新填 |
| `DEEPSEEK_API_KEY` | `~/.dsh/.credentials.yaml` | ✅ 可用，`deepseek-flash` 与 `deepseek-v4-pro` 均可调 |
| `OPENCODE_API_KEY` 等三把 | 两处 | ⚠️ 均返回 `400 MissingSessionID` —— 该端点要求 `x-opencode-session` 头，本项目暂未支持 |
| `COMMANDCODES_API_KEY` | `~/.dsh/.credentials.yaml` | ❌ `insufficient credits` |

**sparky 只从环境变量读密钥**（含被 shell 加载进来的 `keys.env`），
刻意不去读 `~/.dsh/.credentials.yaml` —— 那是 DSH 的内部文件，
让一个独立工具依赖它会把两者耦合在一起。

所以本机要跑 sparky，需要把可用的那把 key 补进 `~/.config/llm/keys.env`：

```bash
# 把 ~/.dsh/.credentials.yaml 里 DEEPSEEK_API_KEY 的值填进去，然后
export DEEPSEEK_API_KEY=sk-...
```

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

标记 `M1` 的已经可用。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-url` `M1` | `https://api.deepseek.com/v1` | 供应商地址 |
| `--api-key-env` `M1` | `DEEPSEEK_API_KEY` | 从哪个**环境变量名**读密钥（不是密钥本身） |
| `--model` `M1` | `deepseek-flash` | 默认模型 |
| `--model-strong` `M1` | 同 `--model` | 组阁 / 定稿用（这两步最吃能力） |
| `--model-fast` `M1` | 同 `--model` | 例行发言与调研用 |
| `--panel-size` `M1` | auto | 指定目标人数（覆盖自动推断的公式），合法范围 3–7。**实际人数以维度覆盖为准**，不一致时会告警而不是强行裁剪 |
| `--concurrency` `M1` | `4` | 同阶段并发度 |
| `--context-dir` `M1` | 无 | 本地资料目录，其中的文本材料按设计维度分配给不同专家 |
| `--out` `M1` | `.sparky/sessions` | 会话根目录 |
| `--max-tokens` `M1` | `16000` | 单次调用输出预算（**含推理 token**）。带下限校验 256，见 §6 已知坑 6 |
| `--temperature` `M1` | `0.7` | 采样温度（定稿各节固定用 0.3） |
| `--timeout` `M1` | `180` | 单次请求超时秒数 |
| `--quiet` `M1` | 关 | 只输出最终结果与错误 |
| `--max-rounds` | `4` | 轮数硬上限（S3/S4 尚未实现，暂隐藏） |
| `--budget-tokens` | 未设 | 总 token 硬上限（同上） |
| `--dimensions-file` | 无 | 追加自定义维度库（M4，见 [`sparky/personas/README.md`](../sparky/personas/README.md)） |
| `--personas-file` | 无 | 固定评审团（M4，同上） |
| `--web` / `--no-web` `M3` | `--web` | 是否启动本地网页并实时呈现讨论 |
| `--port` `M3` | `8765` | 网页端口。**被占用时报错退出，不会自动换端口** |
| `--no-browser` `M3` | 关 | 启动网页但不自动打开浏览器（远程/脚本里用） |
| `--resume <session_id>` | — | 从已有会话续跑（M4） |

### 4.0 开关的暴露原则

**没有实现的开关不暴露** —— 暴露一个只会报错的开关，比不暴露更糟：
用户会以为是自己的用法有问题，去翻文档、试参数，而不是去做该做的事。

M1 因此刻意没有 `--web` / `--port` / `--resume`；
M3 落地时一次性加上了网页相关的三个。`--resume` 仍在等 M4。

### 4.1 关于 `--model-strong` / `--model-fast`

分组讨论里，**组阁与定稿**决定成败（人设造歪了全场白聊，文档写砸了前面白干），
而**例行发言与调研**只是产出可被反驳的立场。分层用模型是省钱的主要手段。

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

6. **推理模型的输出预算会被 reasoning 吃光。**
   `deepseek-flash` 默认走思考，且 `max_tokens` 卡的是**推理 + 正文**的总和。
   2026-10-12 实测：

   | 场景 | `max_tokens` | `completion_tokens` | 其中 reasoning | 正文 |
   |---|---|---|---|---|
   | 简单问答 | 8 | 8 | — | **空** |
   | 简单问答 | 40 | 40 | **40** | **空** |
   | 简单问答 | 200 | ~100–150 | 96–145 | 正常 |
   | 定稿「候选方案对比」（长输入） | 8000 | 8000 | **8000** | **空** |

   即预算会**完全**被思考吃掉，而且 `finish_reason` 未必是 `length`。
   三层防护：
   - `config.py` 对 `--max-tokens` 设了 256 的下限，默认 16000
   - `llm.py` 显式检测 `finish_reason=length` 与空正文，两者都硬报错
   - **机械性任务关掉推理**（见 §6.1）

### 6.1 关掉推理：`thinking: {"type": "disabled"}`

对**不需要产生新判断**的调用（转写、制表、压缩、排期），可以关掉推理。
同一次简单问答实测：completion token **188 → 22**，耗时减半，且不再有空正文风险。

本项目在 `synthesis.py` 的 `SECTION_THINKING` 里逐节声明：

| 节 | 开推理 | 理由 |
|---|---|---|
| 摘要 / 问题与目标 / 候选方案对比 / 里程碑 | 否 | 材料已由 S2 推理过，再推一遍是浪费 |
| 最终方案 / 风险与失败模式 | **是** | 这两节是真正在做设计 |

**`thinking` 是 DeepSeek 系专属参数**（见已知坑 5）：`llm.py` 按 `base_url`
判断是否发送，非 DeepSeek 端点不会带上它 —— 这是**预期行为**，不是降级，所以不发告警。

7. **CN 与 Global 是不同域名，不能互换。**
   Moonshot 是 `.cn` / `.ai`，MiniMax 是 `minimaxi.com` / `minimax.io`，
   GLM 是 `open.bigmodel.cn` / `api.z.ai`，Qwen 是 `dashscope.aliyuncs.com` /
   `dashscope-intl.aliyuncs.com`。用错会认证失败或产生跨境计费。

8. **OpenCode Zen / Go 必须带 `x-opencode-session` 头**（2026-10-12 实测）。
   缺了它直接 `400 MissingSessionID` —— 这不是可选优化，是**能不能连通**的前提。
   该头由 `llm.py` 按 `base_url` 判定后自动加上（用 Sparky 自己的 session_id）。
   实测 `OPENCODE_GO_API_KEY` 与 `OPENCODES_API_KEY` 在带上该头之后**可用**。

   顺带一条：`~/.dsh/.credentials.yaml` 里的 `DEEPSEEK_API_KEY` 在
   2026-10-12 用尽余额（`402 Insufficient Balance`），
   所以本机当前可用的供应商其实是 OpenCode。切过去只要：

   ```bash
   python3 -m sparky "…" --base-url https://opencode.ai/zen/go/v1 \
     --api-key-env OPENCODE_GO_API_KEY --model deepseek-v4.1-flash
   ```

9. **密钥不进 git、不进日志、不进事件流。**
   `llm-calls/` 下的请求快照在落盘前必须**擦除 Authorization 头**。

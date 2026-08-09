# NorthJetty Publish Traces

一个零数据、可迁移的 Codex Skill，用来把 PyTorch Profiler / Chrome Trace 构建成浏览器可直接查看的 Perfetto 站点，并按需通过 NorthJetty 私有发布。

```text
*.trace.json(.gz)
  -> 8 MB 分块 + manifest.json
  -> 静态 Trace Viewer
  -> NorthJetty 鉴权 URL
  -> 浏览器内 Perfetto
```

## 特点

- Skill 仓库本身不包含任何 trace、生成分块或访问凭证。
- 新环境没有 trace 时，也能生成 `0 groups / 0 traces` 的空 Viewer。
- trace 只在运行时通过绝对路径传入，不会复制回 Skill 仓库。
- 自动递归发现 `.trace.json` 和 `.trace.json.gz`。
- 自动按 8,000,000 bytes 分块，避开当前 NorthJetty 单响应大小限制。
- 支持按场景分组，并从文件名识别 `TP/PP`、`TP/DP` 或 `rank`。
- Viewer 使用可搜索的场景下拉框，内部按浏览器本地日期分区并滚动；日期倒序，同一天按最新采集时间倒序。
- Rank 与 Trace 使用紧凑下拉框；当前 `scene`、`rank`、`trace` 会写入 URL，刷新或分享链接后可恢复定位。
- 发布器对 Viewer HTML 和 `manifest.json` 返回 `no-store`，但保留 trace 分块的正常缓存，避免 UI 更新后仍命中旧页面。
- 构建时优先从文件名提取 Unix 时间戳；旧命名不带时间时，自动使用原始 trace 的 mtime。
- 发布前校验 manifest、分块路径、分块大小和总字节数。
- 默认建议使用 NorthJetty `--require-auth`。

## 安装

这是公开仓库，无需 GitHub 登录即可克隆到 Codex Skill 目录：

```bash
SKILLS_ROOT="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$SKILLS_ROOT"
git clone https://github.com/china-qijizhifeng/northjetty-publish-traces.git \
  "$SKILLS_ROOT/northjetty-publish-traces"
```

新开一个 Codex 任务后即可通过 `$northjetty-publish-traces` 调用。

## 在 Codex 中使用

初始化一个没有任何 trace 的空站点：

```text
使用 $northjetty-publish-traces 初始化一个空的 Torch Trace Viewer，
输出到 /absolute/path/trace-site，暂时不要发布。
```

加入运行时 trace 并发布：

```text
使用 $northjetty-publish-traces，把 /absolute/path/prefill-traces 作为 prefill group，
构建到 /absolute/path/trace-site，校验通过后发布为 team-torch-trace，需要访问码。
```

## 手动构建

先指定安装路径和站点输出路径：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/northjetty-publish-traces"
TRACE_SITE=/absolute/path/trace-site
```

### 空站点

```bash
python3 "$SKILL_DIR/scripts/build_trace_site.py" \
  --output "$TRACE_SITE" \
  --title "Team Torch Trace Viewer"
```

这是一个完整的可用结果：页面可以直接拖入本地 trace，但服务器端不包含任何 trace 数据。

### 加入 trace

```bash
python3 "$SKILL_DIR/scripts/build_trace_site.py" \
  --output "$TRACE_SITE" \
  --title "Model Traces" \
  --group prefill=/absolute/path/prefill-traces \
  --group decode=/absolute/path/decode-traces \
  --group-label "prefill=Prefill" \
  --group-label "decode=Decode"
```

同一 group 可以重复指定多个文件或目录。若使用其他命名方式，可通过重复的 `--pattern` 覆盖默认发现规则。

### 日期与排序

生成的 `manifest.json` 会为每条 trace 写入 `timestamp_epoch` 和 `timestamp_source`：

- 文件名含 10 位 Unix 秒时间戳时，以它作为采集时间；
- 文件名没有时间戳时，以原始 trace 文件的 mtime 作为回退；
- 页面使用浏览器本地时区显示日期和时间，不会固定按 UTC 切日；
- 场景先按日期分区、日期从新到旧，同一天按场景内最新 trace 的时间从新到旧；选中 rank 后，trace 列表采用同样的倒序规则。

因此后续只需正常重建站点，新加入的 trace 会自动落到正确日期，不需要手工维护页面顺序。

### 浏览与分享定位

- 点击“场景”即可展开日期分组列表；直接输入场景名或 group id 可以本地搜索，也支持方向键、Enter 和 Escape。
- 场景列表固定最大高度并独立滚动，trace 数量继续增长时不会挤占 Perfetto 的查看空间。
- Rank 和 Trace 收在同一行；Trace 下拉框内部也按日期分组，并默认显示最新记录在前。
- 页面把选择写入 `?scene=...&rank=...&trace=...`，同时保留 URL 中其他参数。复制当前地址即可把同一定位发给同事。
- “收起选择”会把顶部区域压缩为单行，并在当前浏览器中记住状态。

### 校验

每次构建后都应执行：

```bash
python3 "$SKILL_DIR/scripts/validate_trace_site.py" "$TRACE_SITE"
```

空环境的预期结果：

```text
Groups: 0
Traces: 0
Parts: 0
Trace bytes: 0
Validation: OK
```

## 发布到 NorthJetty

发布操作会创建外部 route。执行前需要从 NorthJetty 管理员处获得发布 API Key：

```bash
python3 "$SKILL_DIR/scripts/nj-publish.py" start \
  "$TRACE_SITE" \
  --alias team-torch-trace \
  --require-auth
```

未设置 `NORTHJETTY_API_KEY` 时，客户端会无回显地提示输入。成功后会输出：

- NorthJetty 公网 URL；
- 当前 route 独立的访问码。

发布 API Key 用于创建和删除 route，访问码用于打开 Viewer。两者都不应写进仓库、命令参数或聊天记录。

常用管理命令：

```bash
python3 "$SKILL_DIR/scripts/nj-publish.py" list
python3 "$SKILL_DIR/scripts/nj-publish.py" logs team-torch-trace
python3 "$SKILL_DIR/scripts/nj-publish.py" stop team-torch-trace
```

站点发布后，重新构建同一个输出目录并刷新浏览器即可看到新 trace，通常不需要重建 route。
新版发布器会让 HTML 与 manifest 每次重新获取；已由旧版发布器启动的 route 需要重启一次，新的缓存响应头才会生效。

## 生成目录

```text
trace-site/
├── .northjetty-trace-site
├── index.html
├── manifest.json
└── data/
    ├── prefill/
    │   └── *.part000
    └── decode/
        └── *.part000
```

构建器只会替换空目录，或由它自己创建并带有 `.northjetty-trace-site` 标记的目录。它会拒绝覆盖无标记的非空目录，也拒绝把生成结果写进 Skill 自身。

## 安全与容量边界

- 默认使用 `--require-auth`；除非明确接受公开暴露，否则不要关闭鉴权。
- Viewer 加载 `https://ui.perfetto.dev`，并在浏览器中把选中的 trace buffer 传给 Perfetto iframe；高度敏感的数据应自托管 Perfetto。
- 分块解决的是 NorthJetty 边缘响应大小问题，不会降低浏览器内存占用。浏览器仍需拼接完整 trace，再交给 Perfetto 解析。
- `nj-publish.py` 运行所在的机器或 pod 必须持续存活；宿主退出后 route 会失效。

## 仓库结构

```text
.
├── SKILL.md
├── agents/openai.yaml
├── assets/index.html
├── references/operations.md
└── scripts/
    ├── build_trace_site.py
    ├── validate_trace_site.py
    ├── nj-publish.py
    └── njsite.py
```

详细的 Agent 执行约束见 [`SKILL.md`](./SKILL.md)，运维说明见 [`references/operations.md`](./references/operations.md)。

# 拆书 Agent · 集百家之所长

一个融合三大流派的 AI 拆书工具，把电子书拆解成**思维导图、交互式知识图谱、对抗式总结、可执行技能包、可视化卡片**等多种知识形态。

## 融合了哪些项目的长处

| 流派 | 借鉴项目 | 借鉴点 |
|------|----------|--------|
| 思维导图派 | ebook-to-mindmap | 全书/章节双层导图、自定义提示词 |
| 知识图谱派 | SpineDigest | 三阶段流水线、`.bda` 存档可二次导出 |
| 技能化派 | Cangjie Skill | 把方法论提炼成 AI 可执行技能包 |
| 摘要派 | TextDistiller | 逐章/全书摘要 |

## 核心架构

```
输入(PDF/EPUB/MD/TXT)
   ↓
[阶段一] 知识单元提取 —— 逐块喂 LLM，抽取离散知识单元（带标签）
   ↓
[阶段二] 知识图谱构建 —— TF-IDF + 余弦相似度聚类，社区发现（纯算法）
   ↓
[阶段三] 对抗式总结   —— 一稿 → N 审稿人挑刺 → 修订，反复迭代
   ↓
[附加]   技能包提炼   —— 把知识单元变成 AI 可执行指令集
[附加]   可视化卡片   —— 为核心概念生成深度解读卡片
   ↓
输出: 思维导图(md/html) · 知识图谱(html) · 总结(md) · 技能包(yaml) · 综合报告(html)
```

## 安装

```bash
cd book-digest-agent
pip install -r requirements.txt --break-system-packages
```

## 用法

### CLI

```bash
# 拆解一本书
python run_cli.py digest book.pdf \
  --api-key sk-xxx \
  --base-url https://api.openai.com/v1 \
  --model gpt-4o-mini

# 用 DeepSeek
python run_cli.py digest book.epub \
  -k sk-xxx \
  --base-url https://api.deepseek.com/v1 \
  -m deepseek-chat

# 指定侧重点（只关注系统设计权衡）
python run_cli.py digest book.pdf -k sk-xxx --focus "关注系统设计权衡"

# 从存档二次导出（不重跑 LLM）
python run_cli.py rerun output/book.bda.json

# 启动 Web 服务
python run_cli.py serve --port 8000
```

### Web

```bash
python run_server.py
# 浏览器打开 http://localhost:8000
```

上传文件 → 填 API Key → 开始拆书 → 实时进度 → 预览/下载各类输出。

## 配置（环境变量）

| 变量 | 说明 | 默认 |
|------|------|------|
| `BD_API_KEY` | LLM API Key | - |
| `BD_BASE_URL` | API 地址 | https://api.openai.com/v1 |
| `BD_MODEL` | 模型名 | gpt-4o-mini |
| `BD_CACHE_DIR` | 缓存目录 | .book_digest_cache |
| `BD_OUTPUT_DIR` | 输出目录 | output |

## 支持的模型

任何 OpenAI 兼容接口：OpenAI / DeepSeek / Gemini(兼容) / Moonshot 等，配置 `base_url` + `api_key` 即可。

## 输出形态

| 形态 | 文件 | 说明 |
|------|------|------|
| 思维导图 | `*.mindmap.md` / `.html` | markmap 兼容，可交互折叠 |
| 知识图谱 | `*.graph.html` | pyvis 力导向图，按主题聚类上色 |
| 总结 | `*.summary.md` | 章节要点 + 全书总结 |
| 技能包 | `*.skill.yaml` | 结构化可执行指令集 |
| 综合报告 | `*.report.html` | 上述全部整合的单页 |
| 存档 | `*.bda.json` | 完整知识结构，供二次导出 |

## 关键设计

- **断点续传**：每个 chunk 的处理结果缓存，中途中断下次可续跑
- **对抗式总结**：多智能体互批 + 修订循环，提升准确度
- **二次导出**：`.bda` 存档可换格式/换侧重点重新导出，无需重跑 LLM
- **隐私**：API 请求直连供应商，不经第三方中转

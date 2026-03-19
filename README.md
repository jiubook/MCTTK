# MinecraftTranslationToolkit

> MCTTK

> Minecraft 新闻自动爬取 + 翻译 + 发布

自动从 Minecraft 官方网站爬取最新新闻，通过 AI 翻译为中文，

转换为 BBCode/Markdown 格式，并自动发布到 MCBBS 论坛。

## 功能特性

- 🔄 **自动爬取**：从 Minecraft 官方 API 获取最新新闻
- 🌐 **AI 翻译**：调用 OpenAI 兼容 API 翻译为简体中文
- 📝 **格式转换**：自动生成 BBCode（MCBBS）和 Markdown 格式
- 📤 **自动发布**：登录 MCBBS 论坛自动发帖（支持图片上传）
- 🎯 **类型过滤**：通过配置控制只发布指定类型的新闻
- ⏰ **定时运行**：GitHub Actions 每 6 小时自动执行
- 🛡️ **安全去重**：基于 URL 的状态追踪，不会重复爬取或发布

## 项目结构

```
MCTTK/
（原 JBAiGNN）
├── main.py                  # 编排器：串联爬取→翻译→转换→发布
├── scraper.py               # 爬取与翻译模块
├── converter.py             # JSON → BBCode/Markdown 转换器（原 J2MM）
├── poster.py                # MCBBS 发帖模块
├── config.json              # 统一配置文件
├── .github/workflows/
│   └── scrape.yml           # GitHub Actions 定时任务
├── output/                  # 输出目录（自动生成，不提交）
│   ├── .state.json          # 处理状态（URL 级别去重）
│   ├── .posted.json         # MCBBS 发布状态
│   ├── news_*.json          # 翻译后的文章数据
│   ├── news_*.txt           # BBCode 格式（用于 MCBBS）
│   ├── news_*.md            # Markdown 格式
│   └── news_*.jpg           # 文章头图
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
pip install requests beautifulsoup4
```

### 2. 配置

编辑 `config.json`，至少配置以下内容：

```json
{
  "openai_compat": {
    "host": "api.your-provider.com",
    "api_key": "sk-xxx",
    "model": "gpt-4o"
  }
}
```

如果需要自动发布到 MCBBS：

```json
{
  "mcbbs": {
    "enabled": true,
    "username": "你的用户名",
    "password": "你的密码",
    "forum_fid": 2,
    "sortid_map": {
      "java_snapshot": 1,
      "java_release": 2,
      "bedrock_release": 3,
      "bedrock_beta": 4
    }
  }
}
```

### 3. 运行

```bash
# 全流程：爬取 → 翻译 → 转换 → 发布
python main.py

# 仅检测新新闻（不实际处理）
python main.py --dry-run

# 只爬取翻译，不发布
python main.py --scrape-only

# 只发布 output 目录中已翻译但未发布的文件
python main.py --post-only
```

## 新闻类型过滤

在 `config.json` 的 `news_types` 中控制只处理哪些类型的新闻：

```json
{
  "news_types": {
    "java_release":    true,   // Java 正式版资讯
    "java_snapshot":    true,   // Java 快照版
    "java_prerelease":  false,  // Java 预发布版（不处理）
    "java_rc":          false,  // Java 候选版（不处理）
    "bedrock_release":  true,   // 基岩版正式版
    "bedrock_beta":     true    // 基岩版测试版
  }
}
```

类型自动根据文章标题关键词判断：
- 包含 `Snapshot` → `java_snapshot`
- 包含 `Pre-Release` → `java_prerelease`
- 包含 `Release Candidate` → `java_rc`
- 包含 `Java Edition` → `java_release`
- 包含 `Bedrock` → `bedrock_release`
- 包含 `Beta` / `Preview` → `bedrock_beta`

## GitHub Actions 部署

### 设置 Secrets

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 说明 |
|---|---|
| `OPENAI_API_KEY` | AI 翻译 API 密钥 |
| `MCBBS_USERNAME` | MCBBS 论坛用户名 |
| `MCBBS_PASSWORD` | MCBBS 论坛密码 |
| `MCBBS_CAPTCHA_ANSWER` | 验证码备用答案（可选） |

### 自动运行

Workflow 默认每 6 小时运行一次（UTC 0:00, 6:00, 12:00, 18:00）。

也可手动触发：Actions → Minecraft News Scraper → Run workflow。

## 模块说明

### scraper.py

- `get_latest_news_list()` — 从 API 获取新闻列表
- `classify_news_type(title)` — 根据标题判断新闻类型
- `parse_article_page(url)` — 解析文章页面，提取结构化内容
- `translate_text(text)` — 调用 AI API 翻译单段文本
- `translate_blocks(blocks)` — 批量翻译内容块（支持并发）
- `process_article(news_item)` — 完整处理单篇文章
- `save_article_json(data, save_dir)` — 安全保存 JSON（自动去重文件名）

### converter.py

- `J2MMConverter` — 主转换器类
  - `convert_to_bbcode(json_data)` — 转为 MCBBS BBCode 格式
  - `convert_to_markdown(json_data)` — 转为 Markdown 格式
- `convert_json_file(json_path)` — 文件级别转换

也可独立使用：
```bash
python converter.py news_xxx.json          # 同时输出 .txt 和 .md
python converter.py --batch ./output/      # 批量转换目录
python converter.py news.json --bbcode-only  # 仅输出 BBCode
```

### poster.py

- `MCBBSPoster` — 发帖器类
  - `login()` — 登录 MCBBS
  - `upload_image(path)` — 上传图片
  - `post_thread(title, message)` — 发帖
  - `post_news_file(stem, txt, json, dir)` — 发布单个新闻文件

也可独立使用：
```bash
python poster.py                          # 发布所有未发布的文件
python poster.py --dry-run                # 预览模式
python poster.py news_Minecraft_xxx       # 发布指定文件
```

## 处理流程

```
Minecraft 官网 API
       ↓
  获取新闻列表
       ↓
  按类型过滤 (news_types)
       ↓
  检查已处理状态 (.state.json)
       ↓
  [对每篇新文章]
       ↓
  解析文章页面 → 提取结构化 blocks
       ↓
  AI 翻译标题 + 内容
       ↓
  保存 JSON (output/news_*.json)
       ↓
  转换 BBCode (output/news_*.txt)
       ↓
  转换 Markdown (output/news_*.md)
       ↓
  下载头图 (output/news_*.jpg)
       ↓
  登录 MCBBS → 上传图片 → 发帖
       ↓
  记录已发布状态
```

## 配置参考

```json
{
  "openai_compat": {
    "host": "api.example.com",
    "endpoint": "/v1/chat/completions",
    "api_key_env": "OPENAI_API_KEY",
    "api_key": "",
    "model": "gpt-4o",
    "max_tokens": 10000,
    "timeout": 120
  },
  "minecraft_api": {
    "search_url": "https://net-secondary.web.minecraft-services.net/api/v1.0/zh-cn/search",
    "pageSize": 10,
    "sortType": "Recent",
    "category": "News"
  },
  "news_types": {
    "java_release": true,
    "java_snapshot": true,
    "java_prerelease": true,
    "java_rc": true,
    "bedrock_release": true,
    "bedrock_beta": true
  },
  "mcbbs": {
    "enabled": false,
    "base_url": "https://www.mcbbs.co",
    "forum_fid": 2,
    "username": "",
    "password": "",
    "captcha_answer": "",
    "sortid_map": {}
  },
  "output": {
    "save_dir": "output"
  },
  "concurrency": {
    "translation_workers": 3
  }
}
```

## 注意事项

- API Key 支持通过环境变量 `OPENAI_API_KEY` 传入（优先级高于 config.json）
- MCBBS 账号密码支持通过环境变量 `MCBBS_USERNAME` / `MCBBS_PASSWORD` 传入
- 输出文件名自动处理非法字符，同名文件自动加序号避免冲突
- `output/` 目录下的文件不会被自动清理，需手动管理磁盘空间
- `output/.state.json` 记录已处理的 URL，删除后会重新处理所有新闻

# MinecraftTranslationToolkit

> 矿译通 MCTTK

> Minecraft 新闻自动爬取 + 翻译 + 发布

自动从 Minecraft 官方网站及 Feedback 网站爬取最新新闻，通过 AI 翻译为中文，转换为 BBCode/Markdown 格式，并自动发布到 MCBBS 论坛。

## 功能特性

- **自动爬取**：从 Minecraft 官方 API 获取最新新闻，同时支持从 Feedback 网站爬取更新日志
- **Cloudflare 绕过**：使用 `curl_cffi` 模拟真实浏览器，绕过 Feedback 网站的 Cloudflare 防护
- **AI 翻译**：调用 OpenAI 兼容 API 翻译为简体中文，支持并发批量翻译
- **智能词汇表**：动态检测专业术语，自动添加译名对照到提示词（`glossary.json`）
- **三层去重**：连续去重 + 大段重复检测 + 长文本去重，避免内容重复
- **格式转换**：自动生成 BBCode（MCBBS）和 Markdown 格式
- **模块系统**：通过 `modules_config.json` 配置帖子头部/尾部模板，按新闻类型自动匹配
- **自动发布**：登录 MCBBS 论坛自动发帖，支持图片上传和 JSON 附件
- **类型过滤**：通过配置控制只发布指定类型的新闻
- **安全去重**：基于 URL 的状态追踪，不会重复爬取或发布
- **首次运行保护**：首次运行时自动将所有现有新闻标记为已处理，避免刷屏

## 项目结构

```
MCTTK/
├── main.py              # 编排器：串联爬取→翻译→转换→发布
├── scraper.py           # 爬取与翻译模块（含 Feedback 爬虫）
├── converter.py         # JSON → BBCode/Markdown 转换器
├── poster.py            # MCBBS 发帖模块（含验证码识别）
├── scheduler.py         # 定时调度器（每 10 分钟运行一次）
├── init_state.py        # 初始化状态工具（测试用）
├── config.json          # 统一配置文件
├── glossary.json        # 专业术语词汇表
├── modules_config.json  # 帖子模板模块配置
├── requirements.txt     # Python 依赖
├── .env                 # 本地环境变量（不提交）
├── output/              # 输出目录（自动生成，不提交）
│   ├── .state.json      # 处理状态（URL 级别去重）
│   ├── .posted.json     # MCBBS 发布状态
│   ├── news_*.json      # 翻译后的文章数据
│   ├── news_*.txt       # BBCode 格式（用于 MCBBS）
│   ├── news_*.md        # Markdown 格式
│   └── news_*.jpg       # 文章头图
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

或手动安装：

```bash
pip install requests beautifulsoup4 curl_cffi urllib3 ddddocr
```

> `curl_cffi` 用于绕过 Feedback 网站的 Cloudflare 防护，如不需要爬取 Feedback 可跳过。
> `ddddocr` 用于 MCBBS 登录验证码自动识别，如不需要自动发帖可跳过。

### 2. 配置

编辑 `config.json`，至少配置 AI 翻译接口：

```json
{
  "openai_compat": {
    "host": "api.your-provider.com",
    "endpoint": "/v1/chat/completions",
    "api_key": "sk-xxx",
    "model": "gpt-4o",
    "max_tokens": 10000,
    "timeout": 120
  }
}
```

如果需要自动发布到 MCBBS：

```json
{
  "mcbbs": {
    "enabled": true,
    "base_url": "https://www.mcbbs.co",
    "username": "你的用户名",
    "password": "你的密码",
    "forum_fid": 45,
    "attach_json": true,
    "sortid_map": {
      "java_snapshot": 4,
      "java_prerelease": 4,
      "java_rc": 4,
      "java_release": 4,
      "bedrock_beta": 20,
      "bedrock_release": 20
    }
  }
}
```

也可以通过 `.env` 文件或环境变量传入敏感信息（优先级高于 config.json）：

```
OPENAI_API_KEY=sk-xxx
MCBBS_USERNAME=your_username
MCBBS_PASSWORD=your_password
MCBBS_CAPTCHA_ANSWER=备用验证码
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

# 发布时跳过图片上传
python main.py --no-image

# 发布时跳过 JSON 附件
python main.py --no-json
```

## 新闻来源

### Minecraft 官方 API

从 `https://net-secondary.web.minecraft-services.net/api/v1.0/zh-cn/search` 获取最新新闻，支持按类型过滤。

### Feedback 网站

从 `https://feedback.minecraft.net` 爬取更新日志，在 `config.json` 的 `feedback_site` 中配置：

```json
{
  "feedback_site": {
    "enabled": true,
    "base_url": "https://feedback.minecraft.net",
    "sections": [
      {
        "name": "Release Changelogs",
        "name_cn": "基岩正式版更新日志",
        "section_id": "360001186971",
        "enabled": true,
        "articles_count": 3
      },
      {
        "name": "Beta and Preview Information and Changelogs",
        "name_cn": "基岩测试版更新日志",
        "section_id": "360001185332",
        "enabled": true,
        "articles_count": 3
      }
    ]
  }
}
```

Feedback 文章不受 `news_types` 过滤控制，由各 section 的 `enabled` 字段单独控制。

## 新闻类型过滤

在 `config.json` 的 `news_types` 中控制只处理哪些类型的官方 API 新闻：

```json
{
  "news_types": {
    "java_release":   true,
    "java_snapshot":  true,
    "java_prerelease": false,
    "java_rc":        false,
    "bedrock_release": true,
    "bedrock_beta":   true
  }
}
```

类型根据文章标题关键词自动判断：

| 关键词 | 类型 |
|---|---|
| `Snapshot` | `java_snapshot` |
| `Pre-Release` / `Prerelease` | `java_prerelease` |
| `Release Candidate` | `java_rc` |
| `Java Edition` / 版本号如 `1.21` | `java_release` |
| `Bedrock` | `bedrock_release` |
| `Beta` / `Preview` | `bedrock_beta` |

## 模块系统

`modules_config.json` 定义帖子的头部/尾部模板，按新闻类型自动匹配插入。

- `default_modules`：内置模板（Java 快照、预发布、RC、正式版；基岩测试版、正式版；时评；普通资讯）
- `custom_modules`：自定义模板，始终插入（位于内容与尾部之间）
- `position`：`"start"` 插入头部，`"end"` 插入尾部
- `enabled`：`true` 表示对所有类型生效，`false` 表示仅对匹配类型生效
- `order`：排序权重，数字越小越靠前

每个模块支持 `content`（BBCode）、`bbcode_content`、`markdown_content` 字段，转换时自动选择对应格式。

## 智能词汇表

编辑 `glossary.json` 添加或修改术语：

```json
{
  "terms": {
    "Snapshot": "快照",
    "Pre-Release": "预发布版",
    "Release Candidate": "候选版本",
    "Bedrock Edition": "基岩版",
    "Java Edition": "Java版",
    "Game Drop": "游戏更新"
  }
}
```

翻译时自动扫描文本，只将相关术语添加到提示词中，批量翻译时按批次检测。

## 智能去重机制

为避免网页结构问题导致的内容重复，系统采用三层去重：

1. **连续去重**：去除相邻的重复 block
2. **大段重复检测**：检测连续 5 个以上 block 的重复序列，移除整段重复内容
3. **长文本去重**：对超过 80 字符的长文本进行跟踪，15 个 block 内再次出现则认为是异常重复（列表项除外）

## 模块说明

### scraper.py

- `get_latest_news_list()` — 从官方 API 获取新闻列表
- `classify_news_type(title)` — 根据标题判断新闻类型
- `parse_article_page(url)` — 解析文章页面，提取结构化内容块
- `translate_text(text)` — 调用 AI API 翻译单段文本（支持词汇表）
- `translate_blocks(blocks)` — 批量翻译内容块（支持并发）
- `process_article(news_item)` — 完整处理单篇官方 API 文章
- `FeedbackScraper` — Feedback 网站爬虫类（使用 curl_cffi 绕过 Cloudflare）
- `process_feedback_news(news_item, config)` — 完整处理单篇 Feedback 文章
- `save_article_json(data, save_dir)` — 安全保存 JSON（自动处理文件名冲突）

### converter.py

- `J2MMConverter` — 主转换器类
  - `convert_to_bbcode(json_data)` — 转为 MCBBS BBCode 格式
  - `convert_to_markdown(json_data)` — 转为 Markdown 格式
- `convert_json_file(json_path)` — 文件级别转换

也可独立使用：

```bash
python converter.py news_xxx.json            # 同时输出 .txt 和 .md
python converter.py --batch ./output/        # 批量转换目录
python converter.py news.json --bbcode-only  # 仅输出 BBCode
python converter.py news.json --markdown-only
```

### poster.py

- `MCBBSPoster` — 发帖器类
  - `login()` — 登录 MCBBS（支持验证码自动识别，最多重试 5 次）
  - `upload_image(path)` — 上传图片附件
  - `upload_file(path)` — 上传普通文件附件（如 JSON）
  - `post_thread(title, message)` — 发帖
  - `post_news_file(stem, txt, json, dir)` — 发布单个新闻文件（含图片和 JSON 附件）

也可独立使用：

```bash
python poster.py                          # 发布所有未发布的文件
python poster.py --dry-run                # 预览模式
python poster.py news_Minecraft_xxx       # 发布指定文件
python poster.py --no-image               # 跳过图片上传
python poster.py --no-json                # 跳过 JSON 附件
```

### init_state.py

测试辅助工具。运行后列出所有当前新闻，选择保留一条不标记为已处理，其余全部标记为已处理，避免测试时触发全量翻译。

```bash
python init_state.py
```

## 处理流程

```
Minecraft 官方 API          Feedback 网站
       ↓                          ↓
  获取新闻列表              获取各 section 文章列表
       ↓                          ↓
  按类型过滤 (news_types)    按 section.enabled 过滤
       └──────────┬───────────────┘
                  ↓
         检查已处理状态 (.state.json)
                  ↓
         [对每篇新文章]
                  ↓
         解析文章页面 → 提取结构化 blocks
                  ↓
         AI 翻译标题 + 内容（并发批量）
                  ↓
         保存 JSON (output/news_*.json)
                  ↓
         转换 BBCode (output/news_*.txt)
         转换 Markdown (output/news_*.md)
                  ↓
         下载头图 (output/news_*.jpg)
                  ↓
         登录 MCBBS → 上传图片 → 上传 JSON → 发帖
                  ↓
         记录已发布状态 (.posted.json)
```

## GitHub Actions 部署

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 说明 |
|---|---|
| `OPENAI_API_KEY` | AI 翻译 API 密钥 |
| `MCBBS_USERNAME` | MCBBS 论坛用户名 |
| `MCBBS_PASSWORD` | MCBBS 论坛密码 |
| `MCBBS_CAPTCHA_ANSWER` | 验证码备用答案（可选） |

Workflow 默认每 6 小时运行一次（UTC 0:00, 6:00, 12:00, 18:00），也可手动触发。

## 配置参考

完整的 `config.json` 结构：

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
    "pageSize": 5,
    "sortType": "Recent",
    "category": "News",
    "site_base": "https://www.minecraft.net"
  },
  "feedback_site": {
    "enabled": true,
    "base_url": "https://feedback.minecraft.net",
    "knowledge_base_url": "https://feedback.minecraft.net/hc/en-us/categories/115000410252-Knowledge-Base",
    "timeout": 30,
    "sections": []
  },
  "http": {
    "verify_ssl": false,
    "user_agent": "Mozilla/5.0 ...",
    "proxies": { "http": "", "https": "" },
    "timeout": 120
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
    "forum_fid": 45,
    "username": "",
    "password": "",
    "captcha_answer": "",
    "attach_json": true,
    "sortid_map": {}
  },
  "output": {
    "save_dir": "output"
  },
  "retry": {
    "translation": { "max_retries": 3 },
    "download": { "max_retries": 3 }
  },
  "concurrency": {
    "translation_workers": 3,
    "batch_max_chars": 1000,
    "batch_max_items": 10
  }
}
```

## 注意事项

- **首次运行**：程序会自动将当前所有新闻标记为已处理，下次运行才开始处理真正的新新闻，避免刷屏
- **API Key**：支持通过环境变量 `OPENAI_API_KEY` 传入，优先级高于 config.json
- **MCBBS 账号**：支持通过环境变量 `MCBBS_USERNAME` / `MCBBS_PASSWORD` 传入
- **验证码**：登录时若遇到验证码，会自动使用 `ddddocr` 识别，最多重试 5 次；识别失败时使用 `captcha_answer` 备用答案
- **输出文件**：文件名自动处理非法字符，同名文件自动加序号避免冲突
- **磁盘管理**：`output/` 目录下的文件不会被自动清理，需手动管理
- **状态重置**：删除 `output/.state.json` 后会重新处理所有新闻；删除 `output/.posted.json` 后会重新发布所有已翻译文件

## 许可证

本工具以 [GPL-3.0](https://www.gnu.org/licenses/gpl-3.0.zh-cn.html) 协议发布。

AI 翻译作品以 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hans) 协议发布。

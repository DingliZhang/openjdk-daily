# OpenJDK Daily Report Starter

一个可直接部署到 GitHub Pages 的免费 OpenJDK PR 日报项目。

## 它会做什么

1. GitHub Actions 每天定时运行。
2. 调用 GitHub REST API 获取指定日期的 OpenJDK PR 活动。
3. 程序完成架构、模块、贡献者和状态统计。
4. 调用 GitHub Models 生成技术观察。
5. 把 Markdown 日报提交到仓库。
6. 使用 MkDocs Material 构建并发布 GitHub Pages 网站。
7. AI 调用失败时仍发布基础日报。


## v2 稳定性改进

此版本额外包含：

- 固定使用 MkDocs 1.x，避免未来自动升级到不兼容的 MkDocs 2.x。
- 在 GitHub Actions 中关闭 Material for MkDocs 的 MkDocs 2 提示。
- AI 输入优先选择 RISC-V、已合并以及讨论较多的 PR。
- 限制发送给 GitHub Models 的 PR 数量、正文长度和变更文件数量。
- 遇到 `413 Payload Too Large` 时自动以 30、15、8 个 PR 的三级策略缩小并重试。
- AI 最终仍不可用时继续生成基础日报，不影响网站发布。

## 首次部署

### 1. 创建公开仓库

建议仓库名为 `openjdk-daily`。

### 2. 上传本项目全部文件

可使用命令行：

```bash
git init
git add .
git commit -m "Initial OpenJDK daily report site"
git branch -M main
git remote add origin git@github.com:YOUR_NAME/openjdk-daily.git
git push -u origin main
```

也可以直接在 GitHub 网页上传解压后的全部文件。

### 3. 允许 Workflow 写入仓库

进入：

`Settings → Actions → General → Workflow permissions`

选择：

`Read and write permissions`

然后保存。

### 4. 启用 GitHub Pages

进入：

`Settings → Pages → Build and deployment → Source`

选择：

`GitHub Actions`

### 5. 手工执行一次

进入：

`Actions → OpenJDK Daily Report → Run workflow`

第一次建议输入一个过去日期，例如昨天，格式为 `YYYY-MM-DD`。

执行成功后，网站地址通常是：

`https://YOUR_NAME.github.io/openjdk-daily/`

## 修改运行时间

编辑 `.github/workflows/daily-report.yml`：

```yaml
- cron: "37 0 * * *"
```

Cron 使用 UTC。当前配置对应北京时间 08:37、日本时间 09:37。

## 增加仓库

修改 Workflow 中的：

```yaml
OPENJDK_REPOSITORIES: openjdk/jdk
```

例如：

```yaml
OPENJDK_REPOSITORIES: openjdk/jdk,openjdk/jdk21u,openjdk/jdk17u
```

仓库越多，GitHub API 请求和模型输入越大。建议先只运行 `openjdk/jdk`。

## 配置厂商映射

编辑 `config/contributors.yml`：

```yaml
contributors:
  user-a: Alibaba
  user-b: Huawei
  user-c: Institute of Software, Chinese Academy of Sciences
```

未配置的用户显示为“未归属”。

## 修改 AI 模型

编辑 Workflow：

```yaml
AI_MODEL: openai/gpt-4.1-mini
```

模型不可用或免费额度耗尽时，脚本会自动降级，继续发布不含 AI 深度分析的基础日报。

## 本地测试

需要一个具有公开仓库读取权限和 Models 权限的 GitHub Token：

```bash
export GITHUB_TOKEN=...
python -m pip install -r requirements.txt
python scripts/generate_report.py \
  --date 2026-07-20 \
  --timezone Asia/Shanghai \
  --repositories openjdk/jdk
mkdocs serve
```

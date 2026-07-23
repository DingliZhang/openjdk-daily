#!/usr/bin/env python3
"""Generate an OpenJDK daily PR report and MkDocs archive pages."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
import yaml
from zoneinfo import ZoneInfo


GITHUB_API = "https://api.github.com"
MODELS_API = "https://models.github.ai/inference/chat/completions"
ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DATA = ROOT / "data"
CONTRIBUTORS_FILE = ROOT / "config" / "contributors.yml"

ARCH_RULES: dict[str, tuple[str, ...]] = {
    "RISC-V": ("riscv", "rvv", "zvfh", "zicond", "rva23", "rva22", "zalasr"),
    "AArch64": ("aarch64", "arm64", "sve", "neon"),
    "x86": ("x86", "x86_64", "amd64", "avx", "sse"),
    "PPC": ("ppc", "powerpc"),
    "s390": ("s390",),
    "LoongArch": ("loongarch",),
    "macOS": ("macos", "darwin"),
    "Windows": ("windows", "win32"),
}

MODULE_RULES: dict[str, tuple[str, ...]] = {
    "C2 编译器": (
        "src/hotspot/share/opto/",
        "src/hotspot/cpu/",
        "compiler",
        "c2",
        "superword",
        "matcher",
        "macroassembler",
    ),
    "C1 编译器": ("src/hotspot/share/c1/", "c1 compiler", "client compiler"),
    "GC": (
        "src/hotspot/share/gc/",
        "garbage collector",
        "g1",
        "zgc",
        "shenandoah",
        "parallelgc",
    ),
    "运行时与诊断": (
        "src/hotspot/share/runtime/",
        "src/hotspot/share/services/",
        "runtime",
        "jfr",
        "serviceability",
        "diagnostic",
    ),
    "Vector API": (
        "src/jdk.incubator.vector/",
        "test/jdk/jdk/incubator/vector/",
        "vector api",
        "vectorapi",
    ),
    "核心类库": (
        "src/java.base/",
        "src/java.",
        "core-libs",
        "java.base",
        "class library",
    ),
    "构建与工具链": (
        "make/",
        "build",
        "configure",
        "toolchain",
        "autoconf",
    ),
    "测试": ("test/", "jtreg", "test fix", "testing"),
    "Valhalla/JEP": ("valhalla", "inline class", "value class", "jep"),
}


@dataclass
class PullActivity:
    repository: str
    number: int
    title: str
    url: str
    author: str
    vendor: str
    state: str
    activity: str
    created_at: str
    updated_at: str
    merged_at: str | None
    closed_at: str | None
    labels: list[str]
    modules: list[str]
    architectures: list[str]
    comments_today: int
    reviews_today: int
    changed_files: list[str]
    body_excerpt: str


class GitHubClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "openjdk-daily-report",
            }
        )

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> Any:
        url = path_or_url if path_or_url.startswith("http") else f"{GITHUB_API}{path_or_url}"
        response = self.session.get(url, params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def paged(self, path: str, params: dict[str, Any] | None = None, limit: int = 300) -> list[Any]:
        result: list[Any] = []
        page = 1
        while len(result) < limit:
            merged = dict(params or {})
            merged.update({"per_page": 100, "page": page})
            chunk = self.get(path, merged)
            if not isinstance(chunk, list):
                raise RuntimeError(f"Expected list response from {path}")
            result.extend(chunk)
            if len(chunk) < 100:
                break
            page += 1
        return result[:limit]

    def search_prs(self, repository: str, report_day: date) -> list[dict[str, Any]]:
        # GitHub Search 的日期过滤基于 UTC 日期。取前后各一天，再由脚本按目标时区过滤。
        start = report_day - timedelta(days=1)
        end = report_day + timedelta(days=1)
        query = f"repo:{repository} is:pr updated:{start.isoformat()}..{end.isoformat()}"
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self.get(
                "/search/issues",
                {"q": query, "sort": "updated", "order": "desc", "per_page": 100, "page": page},
            )
            chunk = payload.get("items", [])
            items.extend(chunk)
            if len(chunk) < 100 or len(items) >= min(payload.get("total_count", 0), 1000):
                break
            page += 1
        return items


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="Report date in YYYY-MM-DD; defaults to yesterday")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--repositories", default="openjdk/jdk")
    parser.add_argument("--model", default="openai/gpt-4.1-mini")
    return parser.parse_args()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_in_day(value: str | None, day_start: datetime, day_end: datetime) -> bool:
    parsed = parse_iso(value)
    return bool(parsed and day_start <= parsed < day_end)


def safe_text(value: str | None, limit: int = 600) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value[:limit]


def markdown_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", r"\|").replace("\n", "<br>")


def classify(text: str, rules: dict[str, tuple[str, ...]], fallback: str) -> list[str]:
    haystack = text.lower()
    matches = [name for name, keywords in rules.items() if any(key.lower() in haystack for key in keywords)]
    return matches or [fallback]


def load_contributors() -> dict[str, str]:
    if not CONTRIBUTORS_FILE.exists():
        return {}
    payload = yaml.safe_load(CONTRIBUTORS_FILE.read_text(encoding="utf-8")) or {}
    contributors = payload.get("contributors", {})
    return {str(key): str(value) for key, value in contributors.items()}


def determine_activity(
    pull: dict[str, Any],
    comments_today: int,
    reviews_today: int,
    day_start: datetime,
    day_end: datetime,
) -> str:
    events: list[str] = []
    if is_in_day(pull.get("created_at"), day_start, day_end):
        events.append("新建")
    if is_in_day(pull.get("merged_at"), day_start, day_end):
        events.append("已合并")
    elif is_in_day(pull.get("closed_at"), day_start, day_end):
        events.append("已关闭")
    if reviews_today:
        events.append(f"{reviews_today} 个 Review")
    if comments_today:
        events.append(f"{comments_today} 条评论")
    if not events:
        events.append("有更新")
    return "、".join(events)


def collect_pull(
    client: GitHubClient,
    repository: str,
    item: dict[str, Any],
    contributors: dict[str, str],
    day_start: datetime,
    day_end: datetime,
) -> PullActivity | None:
    number = int(item["number"])
    pull = client.get(f"/repos/{repository}/pulls/{number}")

    issue_comments = client.paged(
        f"/repos/{repository}/issues/{number}/comments",
        limit=200,
    )
    reviews = client.paged(
        f"/repos/{repository}/pulls/{number}/reviews",
        limit=200,
    )
    files = client.paged(
        f"/repos/{repository}/pulls/{number}/files",
        limit=300,
    )

    comments_today = sum(
        1 for comment in issue_comments if is_in_day(comment.get("created_at"), day_start, day_end)
    )
    reviews_today = sum(
        1 for review in reviews if is_in_day(review.get("submitted_at"), day_start, day_end)
    )

    has_day_activity = any(
        (
            is_in_day(pull.get("created_at"), day_start, day_end),
            is_in_day(pull.get("updated_at"), day_start, day_end),
            is_in_day(pull.get("merged_at"), day_start, day_end),
            is_in_day(pull.get("closed_at"), day_start, day_end),
            comments_today > 0,
            reviews_today > 0,
        )
    )
    if not has_day_activity:
        return None

    filenames = [entry.get("filename", "") for entry in files]
    labels = [entry.get("name", "") for entry in pull.get("labels", [])]
    body_excerpt = safe_text(pull.get("body"))
    combined = " ".join(
        [
            pull.get("title", ""),
            body_excerpt,
            " ".join(filenames),
            " ".join(labels),
        ]
    )
    author = pull.get("user", {}).get("login", "unknown")
    vendor = contributors.get(author, "未归属")

    architectures = classify(combined, ARCH_RULES, "跨平台/未分类")
    modules = classify(combined, MODULE_RULES, "其他")

    state = "已合并" if pull.get("merged_at") else ("开启" if pull.get("state") == "open" else "已关闭")
    activity = determine_activity(pull, comments_today, reviews_today, day_start, day_end)

    return PullActivity(
        repository=repository,
        number=number,
        title=pull.get("title", ""),
        url=pull.get("html_url", ""),
        author=author,
        vendor=vendor,
        state=state,
        activity=activity,
        created_at=pull.get("created_at", ""),
        updated_at=pull.get("updated_at", ""),
        merged_at=pull.get("merged_at"),
        closed_at=pull.get("closed_at"),
        labels=labels,
        modules=modules,
        architectures=architectures,
        comments_today=comments_today,
        reviews_today=reviews_today,
        changed_files=filenames[:40],
        body_excerpt=body_excerpt,
    )


def counter_table(counter: Counter[str], total: int) -> str:
    lines = ["| 分类 | PR 数量 | 占比 |", "|---|---:|---:|"]
    if not counter:
        lines.append("| 无 | 0 | 0% |")
        return "\n".join(lines)
    for name, count in counter.most_common():
        percentage = (count / total * 100) if total else 0
        lines.append(f"| {markdown_cell(name)} | {count} | {percentage:.1f}% |")
    return "\n".join(lines)


def pull_table(pulls: Iterable[PullActivity], include_vendor: bool = False) -> str:
    pulls = list(pulls)
    if include_vendor:
        lines = [
            "| 仓库 | PR | 作者 | 厂商/组织 | 状态 | 当日活动 | 模块 | 描述 |",
            "|---|---:|---|---|---|---|---|---|",
        ]
    else:
        lines = [
            "| 仓库 | PR | 作者 | 状态 | 当日活动 | 模块 | 描述 |",
            "|---|---:|---|---|---|---|---|",
        ]

    if not pulls:
        span = 8 if include_vendor else 7
        lines.append("| " + "暂无 |" * span)
        return "\n".join(lines)

    for pull in pulls:
        pr_link = f"[#{pull.number}]({pull.url})"
        modules = "、".join(pull.modules)
        description = pull.title
        if include_vendor:
            row = [
                pull.repository,
                pr_link,
                f"@{pull.author}",
                pull.vendor,
                pull.state,
                pull.activity,
                modules,
                description,
            ]
        else:
            row = [
                pull.repository,
                pr_link,
                f"@{pull.author}",
                pull.state,
                pull.activity,
                modules,
                description,
            ]
        lines.append("| " + " | ".join(markdown_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def prioritize_model_pulls(pulls: list[PullActivity]) -> list[PullActivity]:
    """Choose the most useful PRs for AI analysis without changing report statistics."""
    return sorted(
        pulls,
        key=lambda pull: (
            "RISC-V" not in pull.architectures,
            pull.state != "已合并",
            -(pull.reviews_today + pull.comments_today),
            -pull.number,
        ),
    )


def build_model_input(
    pulls: list[PullActivity],
    max_pulls: int,
    max_json_bytes: int,
) -> tuple[list[dict[str, Any]], int]:
    """Build a compact JSON input and enforce a UTF-8 byte limit."""
    compact: list[dict[str, Any]] = []

    for pull in prioritize_model_pulls(pulls)[:max_pulls]:
        item = {
            "repository": pull.repository,
            "number": pull.number,
            "title": pull.title[:180],
            "url": pull.url,
            "author": pull.author,
            "vendor": pull.vendor,
            "state": pull.state,
            "activity": pull.activity,
            "modules": pull.modules[:3],
            "architectures": pull.architectures[:3],
            "labels": pull.labels[:5],
            "body_excerpt": pull.body_excerpt[:160],
            "changed_files": pull.changed_files[:5],
        }

        candidate = compact + [item]
        encoded = json.dumps(
            candidate,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        if len(encoded) > max_json_bytes:
            break
        compact.append(item)

    encoded_size = len(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return compact, encoded_size


def call_model(token: str, model: str, report_day: date, pulls: list[PullActivity]) -> str:
    system_prompt = """你是 OpenJDK 技术日报编辑。只能根据输入 JSON 中的事实写作。
不得虚构 PR、Issue、作者、公司、性能数据、评审结论或技术影响。
厂商字段为“未归属”时，不得猜测作者所属公司。
以中文 Markdown 输出，不要输出总标题，不要重复程序已生成的统计表。
输出且只输出以下结构：

## 三、当日技术观察

### 值得关注的变化
用 3～8 条列表，优先讨论有明确技术含义的变化，并附 PR 链接。

### RISC-V 观察
有 RISC-V 活动时进行具体归纳；没有时明确写“当日未发现 RISC-V 相关 PR 活动”。

### 风险与回归
只列输入能够支持的风险、测试关注点或待确认事项；证据不足时明确说证据不足。

### 后续关注
列出 2～6 个后续值得跟踪的 PR 或讨论方向。
"""

    # Start conservatively. If GitHub Models still returns HTTP 413,
    # retry with progressively smaller requests.
    attempts = (
        (30, 48_000),
        (15, 24_000),
        (8, 12_000),
    )
    last_error = ""

    for attempt_number, (max_pulls, max_json_bytes) in enumerate(attempts, start=1):
        compact, compact_bytes = build_model_input(
            pulls,
            max_pulls=max_pulls,
            max_json_bytes=max_json_bytes,
        )

        user_prompt = (
            f"报告日期：{report_day.isoformat()}\n"
            f"程序共采集到 {len(pulls)} 个有活动的 PR。"
            f"以下按 RISC-V、已合并以及讨论热度优先选取 {len(compact)} 个供技术观察。\n"
            f"PR 活动 JSON：\n"
            f"{json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}"
        )

        request_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1800,
        }
        request_bytes = len(
            json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

        print(
            "GitHub Models request "
            f"{attempt_number}/{len(attempts)}: "
            f"{len(compact)}/{len(pulls)} PRs, "
            f"{compact_bytes} JSON bytes, "
            f"{request_bytes} total request bytes"
        )

        response = requests.post(
            MODELS_API,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=request_payload,
            timeout=120,
        )

        if response.status_code == 413:
            last_error = (
                f"HTTP 413 on attempt {attempt_number}: "
                f"{safe_text(response.text, 500) or 'Payload Too Large'}"
            )
            print(
                f"Warning: {last_error}; retrying with a smaller payload.",
                file=sys.stderr,
            )
            continue

        if not response.ok:
            raise RuntimeError(
                f"GitHub Models returned HTTP {response.status_code}: "
                f"{safe_text(response.text, 1000)}"
            )

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("GitHub Models returned no choices")

        content = choices[0].get("message", {}).get("content", "").strip()
        if not content:
            raise RuntimeError("GitHub Models returned empty content")
        return content

    raise RuntimeError(
        "GitHub Models request remained too large after three reductions. "
        f"Last error: {last_error}"
    )


def fallback_observation(pulls: list[PullActivity], error: str) -> str:
    riscv = [pull for pull in pulls if "RISC-V" in pull.architectures]
    merged = [pull for pull in pulls if pull.state == "已合并"]
    lines = [
        "## 三、当日技术观察",
        "",
        "!!! warning \"AI 分析暂不可用\"",
        f"    本次 GitHub Models 调用失败，已保留确定性统计和 PR 明细。错误摘要：`{safe_text(error, 220)}`",
        "",
        "### 值得关注的变化",
        "",
    ]
    if merged:
        for pull in merged[:8]:
            lines.append(f"- [#{pull.number}]({pull.url}) {pull.title}（{pull.repository}，已合并）")
    elif pulls:
        for pull in pulls[:8]:
            lines.append(f"- [#{pull.number}]({pull.url}) {pull.title}（{pull.activity}）")
    else:
        lines.append("- 当日未采集到 PR 活动。")

    lines.extend(["", "### RISC-V 观察", ""])
    if riscv:
        for pull in riscv[:10]:
            lines.append(f"- [#{pull.number}]({pull.url}) {pull.title}（{pull.activity}）")
    else:
        lines.append("- 当日未发现 RISC-V 相关 PR 活动。")

    lines.extend(
        [
            "",
            "### 风险与回归",
            "",
            "- AI 分析不可用，建议直接查看 PR 描述、变更文件和 Review 讨论。",
            "",
            "### 后续关注",
            "",
            "- 等待下一次定时运行重试 GitHub Models。",
        ]
    )
    return "\n".join(lines)


def write_report(
    report_day: date,
    timezone_name: str,
    repositories: list[str],
    pulls: list[PullActivity],
    ai_section: str,
) -> Path:
    total = len(pulls)
    merged = [pull for pull in pulls if pull.state == "已合并"]
    opened_today = [pull for pull in pulls if "新建" in pull.activity]
    riscv = [pull for pull in pulls if "RISC-V" in pull.architectures]

    architecture_counter: Counter[str] = Counter()
    module_counter: Counter[str] = Counter()
    vendor_counter: Counter[str] = Counter()
    for pull in pulls:
        architecture_counter.update(pull.architectures)
        module_counter.update(pull.modules)
        vendor_counter.update([pull.vendor])

    year_dir = DOCS / "reports" / str(report_day.year)
    year_dir.mkdir(parents=True, exist_ok=True)
    report_path = year_dir / f"{report_day.isoformat()}.md"

    top_pulls = sorted(
        pulls,
        key=lambda item: (
            item.state != "已合并",
            -(item.reviews_today + item.comments_today),
            item.number,
        ),
    )

    content = f"""# OpenJDK 社区日报（{report_day.isoformat()}）

> 数据范围：`{", ".join(repositories)}`  
> 报告时区：`{timezone_name}`  
> 生成方式：GitHub API 确定性统计 + GitHub Models 技术观察

## 一、当日概览

| 指标 | 数量 |
|---|---:|
| 有活动的 PR | {total} |
| 当日新建 | {len(opened_today)} |
| 当日或当前已合并 | {len(merged)} |
| RISC-V 相关 | {len(riscv)} |
| 活跃贡献者 | {len({pull.author for pull in pulls})} |

### 架构分布

{counter_table(architecture_counter, total)}

### 模块分布

{counter_table(module_counter, total)}

### 厂商/组织统计

{counter_table(vendor_counter, total)}

!!! note
    厂商/组织只来自 `config/contributors.yml` 的人工映射；未配置的作者统一显示为“未归属”，不会让 AI 猜测。

## 二、当日详细进展

### RISC-V 提交明细

{pull_table(riscv, include_vendor=True)}

### 已合并的重要 PR

{pull_table(merged[:30], include_vendor=False)}

### 全部 PR 活动

{pull_table(top_pulls[:100], include_vendor=False)}

{ai_section}

## 四、数据说明

- “有活动”包括报告日期内的新建、合并、关闭、Issue 评论、Review 或更新时间变化。
- 模块和架构由标题、PR 描述、标签与变更文件路径的规则匹配得到，可能存在多标签。
- AI 只接收程序采集后的结构化信息；所有统计表均由程序生成。
- 原始结构化数据保存在仓库的 `data/{report_day.year}/{report_day.isoformat()}.json`。
"""
    report_path.write_text(content, encoding="utf-8")
    return report_path


def write_data(report_day: date, pulls: list[PullActivity]) -> Path:
    year_dir = DATA / str(report_day.year)
    year_dir.mkdir(parents=True, exist_ok=True)
    path = year_dir / f"{report_day.isoformat()}.json"
    payload = {
        "date": report_day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pulls": [asdict(pull) for pull in pulls],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def discover_reports() -> list[tuple[date, Path]]:
    reports: list[tuple[date, Path]] = []
    for path in (DOCS / "reports").glob("*/*.md"):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        reports.append((day, path))
    return sorted(reports, key=lambda item: item[0], reverse=True)


def update_archive(latest_day: date, latest_pulls: list[PullActivity]) -> None:
    reports = discover_reports()
    archive_lines = [
        "# 日报归档",
        "",
        f"共收录 **{len(reports)}** 期日报。",
        "",
        "| 日期 | 报告 |",
        "|---|---|",
    ]
    for day, path in reports:
        relative = path.relative_to(DOCS / "reports")
        archive_lines.append(
            f"| {day.isoformat()} | [OpenJDK 社区日报（{day.isoformat()}）]({relative.as_posix()}) |"
        )
    (DOCS / "reports" / "index.md").write_text("\n".join(archive_lines) + "\n", encoding="utf-8")

    riscv = [pull for pull in latest_pulls if "RISC-V" in pull.architectures]
    merged = [pull for pull in latest_pulls if pull.state == "已合并"]
    latest_link = f"reports/{latest_day.year}/{latest_day.isoformat()}.md"

    highlights = []
    for pull in (riscv + merged + latest_pulls)[:8]:
        entry = f"- [#{pull.number}]({pull.url}) {pull.title}（{pull.activity}）"
        if entry not in highlights:
            highlights.append(entry)

    homepage = f"""# OpenJDK 社区日报

由 GitHub Actions 定时采集 OpenJDK Pull Request 活动，并由 AI 生成技术观察。整个流程运行在 GitHub 云端，不依赖个人电脑在线。

## 最新日报

### [{latest_day.isoformat()}]({latest_link})

| 指标 | 数量 |
|---|---:|
| 有活动的 PR | {len(latest_pulls)} |
| 已合并 PR | {len(merged)} |
| RISC-V 相关 PR | {len(riscv)} |
| 活跃贡献者 | {len({pull.author for pull in latest_pulls})} |

## 今日关注

{chr(10).join(highlights) if highlights else "- 当日未采集到 PR 活动。"}

[查看完整日报]({latest_link}) · [浏览全部历史](reports/index.md)
"""
    (DOCS / "index.md").write_text(homepage, encoding="utf-8")


def main() -> int:
    args = parse_args()
    try:
        zone = ZoneInfo(args.timezone)
    except Exception as exc:
        print(f"Invalid timezone {args.timezone}: {exc}", file=sys.stderr)
        return 2

    today_local = datetime.now(zone).date()
    try:
        report_day = date.fromisoformat(args.date) if args.date.strip() else today_local - timedelta(days=1)
    except ValueError:
        print("--date must use YYYY-MM-DD", file=sys.stderr)
        return 2

    day_start_local = datetime.combine(report_day, time.min, tzinfo=zone)
    day_end_local = day_start_local + timedelta(days=1)
    day_start = day_start_local.astimezone(timezone.utc)
    day_end = day_end_local.astimezone(timezone.utc)

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    repositories = [part.strip() for part in args.repositories.split(",") if part.strip()]
    contributors = load_contributors()
    client = GitHubClient(token)

    pulls: list[PullActivity] = []
    failures: list[str] = []
    seen: set[tuple[str, int]] = set()

    for repository in repositories:
        print(f"Searching {repository} for {report_day.isoformat()} activity...")
        try:
            items = client.search_prs(repository, report_day)
        except Exception as exc:
            failures.append(f"{repository}: search failed: {exc}")
            continue

        for item in items:
            key = (repository, int(item["number"]))
            if key in seen:
                continue
            seen.add(key)
            try:
                activity = collect_pull(
                    client,
                    repository,
                    item,
                    contributors,
                    day_start,
                    day_end,
                )
                if activity:
                    pulls.append(activity)
            except Exception as exc:
                failures.append(f"{repository}#{item.get('number')}: {exc}")
                print(f"Warning: failed to collect {repository}#{item.get('number')}: {exc}", file=sys.stderr)

    pulls.sort(key=lambda item: (item.repository, -item.number))
    print(f"Collected {len(pulls)} active PRs.")

    try:
        ai_section = call_model(token, args.model, report_day, pulls)
        if not ai_section.startswith("## 三、当日技术观察"):
            ai_section = "## 三、当日技术观察\n\n" + ai_section
    except Exception as exc:
        failures.append(f"GitHub Models: {exc}")
        print(f"Warning: model call failed: {exc}", file=sys.stderr)
        ai_section = fallback_observation(pulls, str(exc))

    report_path = write_report(report_day, args.timezone, repositories, pulls, ai_section)
    data_path = write_data(report_day, pulls)
    update_archive(report_day, pulls)

    if failures:
        failure_path = DATA / str(report_day.year) / f"{report_day.isoformat()}-warnings.txt"
        failure_path.write_text("\n".join(failures) + "\n", encoding="utf-8")

    print(f"Wrote {report_path.relative_to(ROOT)}")
    print(f"Wrote {data_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

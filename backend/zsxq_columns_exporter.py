"""专栏批量导出为 Markdown 的引擎。

将指定群组的全部专栏导出为按专栏分组的 Markdown 文件:
- 每个专栏一个文件夹,文章按加入专栏时间从早到晚编号
- 长文章(托管在 articles.zsxq.com)会带 Cookie 抓取全文,替换正文中的简介
- 文章图片与头像下载到各专栏 assets/ 目录,Markdown 使用相对路径
- 正文为 PDF 等附件的文章,通过文件接口下载附件并放在文章旁

被 backend.main 的导出接口与 scripts/export_columns.py 共用。
"""

import hashlib
import json
import random
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

from backend.db_path_manager import get_db_path_manager
from backend.image_cache_manager import get_image_cache_manager
from backend.zsxq_columns_database import ZSXQColumnsDatabase
from backend.zsxq_markdown_exporter import (
    article_html_to_markdown,
    column_topic_detail_to_markdown,
    html_to_markdown,
    safe_filename,
)
from backend.zsxq_request_profiles import build_zsxq_mobile_headers

LogFunc = Callable[[str], None]


def hydrate_column_topic_detail(detail: Dict) -> Dict:
    """补全 Q&A / talk 类型文章的正文字段(与 backend.main 中同名逻辑一致)。"""
    if not detail.get("raw_json"):
        return detail
    try:
        raw = json.loads(detail["raw_json"])
    except (json.JSONDecodeError, TypeError):
        return detail

    topic_type = raw.get("type", "")
    if topic_type == "q&a":
        question = raw.get("question", {})
        answer = raw.get("answer", {})
        detail["question"] = {
            "text": question.get("text", ""),
            "owner": question.get("owner"),
            "images": question.get("images", []),
        }
        detail["answer"] = {
            "text": answer.get("text", ""),
            "owner": answer.get("owner"),
            "images": answer.get("images", []),
        }
        if not detail.get("full_text") and answer.get("text"):
            detail["full_text"] = answer.get("text", "")
    elif topic_type == "talk":
        talk = raw.get("talk", {})
        if not detail.get("full_text") and talk.get("text"):
            detail["full_text"] = talk.get("text", "")
    return detail


def get_article_url(detail: Dict) -> str:
    """长文章的正文托管在 articles.zsxq.com,链接存于 raw_json 的 talk.article 中。"""
    if not detail.get("raw_json"):
        return ""
    try:
        raw = json.loads(detail["raw_json"])
    except (json.JSONDecodeError, TypeError):
        return ""
    article = (raw.get("talk") or {}).get("article") or raw.get("article") or {}
    return article.get("article_url") or article.get("inline_article_url") or ""


def get_attached_files(detail: Dict) -> List[Dict]:
    """部分文章的正文是 PDF 等附件,而非长文章网页。"""
    if not detail.get("raw_json"):
        return []
    try:
        raw = json.loads(detail["raw_json"])
    except (json.JSONDecodeError, TypeError):
        return []
    talk = raw.get("talk") or {}
    return [f for f in (talk.get("files") or raw.get("files") or []) if f.get("file_id")]


def fetch_article_markdown(article_url: str, headers: Dict[str, str], fallback_title: str) -> str:
    """抓取长文章网页并转为 Markdown;失败返回空串。"""
    if not article_url:
        return ""
    parsed = urlparse(article_url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    try:
        response = requests.get(article_url, headers=headers, timeout=30, allow_redirects=True)
        response.raise_for_status()
        if "json" in response.headers.get("content-type", ""):
            return ""
        response.encoding = response.encoding or "utf-8"
        return article_html_to_markdown(response.text, fallback_title=fallback_title)
    except Exception:
        return ""


def strip_article_header(article_md: str) -> str:
    """去掉文章页自带的 H1 标题与模板头部(来自链接/头像/作者/日期)。

    渲染器输出的 Markdown 已包含标题与元信息行,保留会重复。
    """
    article_md = re.sub(r"^#\s+.+?\n", "", article_md.strip(), count=1)
    article_md = re.sub(
        r"^\s*\[来自.*?\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}[^\n]*\n?",
        "", article_md, count=1, flags=re.S,
    )
    return article_md.lstrip()


def download_attached_file(file_id, name: str, cookie: str, group_id: str, save_path: Path,
                           log: Optional[LogFunc] = None, max_attempts: int = 3) -> bool:
    """通过星球文件接口下载附件:先取临时下载链接,再下载文件本体。

    下载链接接口偶发限流,失败时自动重试。
    """
    headers = build_zsxq_mobile_headers(cookie, str(group_id))
    api = f"https://api.zsxq.com/v2/files/{file_id}/download_url"

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(api, headers=headers, timeout=30)
            dl_url = (resp.json().get("resp_data") or {}).get("download_url")
            if not dl_url:
                raise RuntimeError(f"未获取到下载链接: HTTP {resp.status_code}")
            with requests.get(dl_url, timeout=180, stream=True) as r:
                r.raise_for_status()
                with open(save_path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        fh.write(chunk)
            if save_path.exists() and save_path.stat().st_size > 0:
                return True
            raise RuntimeError("下载数据为空")
        except Exception as e:
            if log:
                log(f"⚠️ 附件下载失败(第{attempt}/{max_attempts}次): {name}, error={e}")
            if attempt < max_attempts:
                time.sleep(random.uniform(5, 10))
    return False


def build_asset_resolver(cache_manager, assets_dir: Path):
    """下载图片到本地 assets/ 并返回 markdown 相对路径;失败时退化为远程 URL。"""
    url_to_relpath: Dict[str, str] = {}

    def resolver(url: str, kind: str) -> str:
        if not url:
            return url
        cached = url_to_relpath.get(url)
        if cached is not None:
            return cached

        local_path = None
        try:
            success, path, _err = cache_manager.download_and_cache(url, timeout=20)
            if success and path:
                local_path = Path(path)
        except Exception:
            local_path = None

        if local_path is None or not local_path.exists():
            url_to_relpath[url] = url
            return url

        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        ext = local_path.suffix
        if not ext:
            m = re.search(r"\.(jpe?g|png|gif|webp|bmp|svg)(?:\?|$)", url, flags=re.IGNORECASE)
            ext = "." + m.group(1).lower() if m else ".jpg"
        target = assets_dir / f"{digest}{ext}"
        if not target.exists():
            try:
                assets_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(local_path), str(target))
            except Exception:
                url_to_relpath[url] = url
                return url

        rel = f"assets/{target.name}"
        url_to_relpath[url] = rel
        return rel

    return resolver


def run_columns_export(
    group_id: str,
    out_root: Path,
    cookie: str,
    headers: Dict[str, str],
    log: Optional[LogFunc] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, int]:
    """执行整组专栏导出,返回统计信息。

    Args:
        group_id: 群组 ID
        out_root: 导出输出目录
        cookie: 登录 Cookie(用于附件下载)
        headers: 带 Cookie 的网页请求头(用于抓取长文章全文)
        log: 进度日志回调
        should_stop: 返回 True 时尽快中止导出
    """
    say = log or (lambda msg: print(msg))
    stopped = should_stop or (lambda: False)
    stats = {
        "columns": 0,
        "articles": 0,
        "fulltext_fetched": 0,
        "fulltext_failed": 0,
        "files_downloaded": 0,
        "files_failed": 0,
        "skipped_no_detail": 0,
    }

    path_manager = get_db_path_manager()
    db_path = path_manager.get_columns_db_path(str(group_id))
    db = ZSXQColumnsDatabase(db_path)
    try:
        columns = db.get_columns(int(group_id))
        if not columns:
            raise ValueError("该群组没有专栏数据,请先采集专栏")

        cache_manager = get_image_cache_manager(str(group_id))
        out_root.mkdir(parents=True, exist_ok=True)
        index_lines = [f"# 专栏导出索引(群组 {group_id})", ""]

        for col in columns:
            if stopped():
                say("🛑 收到停止请求,导出中止")
                break
            column_id = col.get("column_id")
            column_name = safe_filename(str(col.get("name") or f"column_{column_id}"))[:60]
            col_dir = out_root / column_name
            assets_dir = col_dir / "assets"
            resolver = build_asset_resolver(cache_manager, assets_dir)

            # get_column_topics 按加入专栏时间倒序返回,反转为从早到晚的阅读顺序
            topics = list(reversed(db.get_column_topics(column_id)))
            say(f"▶ 专栏《{col.get('name')}》共 {len(topics)} 篇")
            index_lines.append(f"- **{col.get('name')}**({len(topics)} 篇)")
            index_lines.append(f"  → `{column_name}/`")
            stats["columns"] += 1

            used_names: set = set()
            for idx, topic in enumerate(topics, start=1):
                if stopped():
                    break
                topic_id = topic.get("topic_id")
                if not topic.get("has_detail"):
                    stats["skipped_no_detail"] += 1
                    say(f"  ⚠ [{idx}/{len(topics)}] {topic_id} 无详情,跳过(采集可能未完成)")
                    continue

                detail = db.get_topic_detail(topic_id)
                if not detail:
                    stats["skipped_no_detail"] += 1
                    continue
                detail = hydrate_column_topic_detail(detail)

                # 长文章:抓取 articles.zsxq.com 全文替换简介
                article_url = get_article_url(detail)
                if article_url and headers:
                    article_md = fetch_article_markdown(
                        article_url, headers, detail.get("title") or "")
                    if article_md:
                        detail["full_text"] = (
                            strip_article_header(article_md).rstrip()
                            + f"\n\n---\n\n原文: [{detail.get('title') or article_url}]({article_url})\n"
                        )
                        stats["fulltext_fetched"] += 1
                    else:
                        stats["fulltext_failed"] += 1
                        say(f"  ⚠ [{idx}/{len(topics)}] 全文抓取失败,仅导出简介")
                    time.sleep(random.uniform(1.5, 3.0))

                title = detail.get("title") or topic.get("title") or f"topic_{topic_id}"
                stem = f"{idx:03d}_{safe_filename(str(title))[:80]}"
                if stem in used_names:
                    stem = f"{stem}_{topic_id}"
                used_names.add(stem)

                markdown = column_topic_detail_to_markdown(detail, asset_resolver=resolver)

                # 附件下载:这类文章没有长文章网页,正文就是 PDF 等附件
                file_links = []
                for f in get_attached_files(detail):
                    file_name = safe_filename(str(f.get("name") or f"file_{f.get('file_id')}"))
                    save_path = col_dir / f"{stem}_{file_name}"
                    if download_attached_file(f.get("file_id"), file_name, cookie,
                                              group_id, save_path, log=say):
                        size_mb = (f.get("size") or 0) / (1024 * 1024)
                        file_links.append(
                            f"- [{file_name}]({save_path.name})({size_mb:.1f}MB)")
                        stats["files_downloaded"] += 1
                        say(f"  📎 已下载附件: {file_name}")
                    else:
                        file_links.append(f"- {file_name}(下载失败)")
                        stats["files_failed"] += 1
                    time.sleep(random.uniform(1.5, 3.0))
                if file_links:
                    markdown += "\n**附件**:\n" + "\n".join(file_links) + "\n"

                (col_dir / f"{stem}.md").write_text(markdown, encoding="utf-8")
                stats["articles"] += 1
                if idx % 10 == 0 or idx == len(topics):
                    say(f"  ✅ {col.get('name')}: {idx}/{len(topics)}")

        (out_root / "README.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    finally:
        db.close()
    return stats

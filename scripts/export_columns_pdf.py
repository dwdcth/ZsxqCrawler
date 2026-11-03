"""将 columns_export 导出的专栏 Markdown 合集转换为 PDF。

每个专栏文件夹 → 一个 PDF:<前缀>-<专栏名>.pdf
- 文章按编号顺序合并,每篇新起一页
- 长文章正文中的远程图片先下载到 assets/(按 URL md5 缓存),连同本地图片
  一并以 base64 内嵌,PDF 完全离线可读
- 站点内部资源引用(如 /assets_dweb/logo@1x.png)自动剔除,避免破图
- 使用 Chrome 无头模式打印,中文字体 Noto Sans CJK

依赖: pip install markdown requests(或用 uv run --with markdown --with requests)

用法:
    python scripts/export_columns_pdf.py <markdown导出目录> <PDF输出目录> [文件名前缀]
"""

import base64
import hashlib
import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

import markdown
import requests

CSS = """
@page { size: A4; margin: 18mm 16mm; }
body {
    font-family: "Noto Sans CJK SC", "Noto Sans CJK", "WenQuanYi Zen Hei",
                 "Segoe UI Emoji", "Noto Color Emoji", sans-serif;
    font-size: 11pt; line-height: 1.75; color: #1a1a1a;
}
h1 { font-size: 17pt; margin: 0.4em 0 0.6em; }
h2 { font-size: 14pt; }
h3 { font-size: 12pt; }
img { max-width: 100%; height: auto; }
blockquote {
    margin: 0.6em 0; padding: 0.5em 1em;
    border-left: 3px solid #d0d0d0; color: #444; background: #f7f7f8;
}
blockquote img { width: 20px; border-radius: 50%; vertical-align: middle; }
hr { border: none; border-top: 1px solid #ddd; margin: 1.4em 0; }
code { background: #f2f2f2; padding: 1px 4px; border-radius: 3px; }
.article { page-break-before: always; }
/* header 之后的第一篇文章不翻页,与专栏抬头同页 */
div.article:first-of-type { page-break-before: auto; }
.meta-line { color: #666; font-size: 10pt; }
"""

IMG_RE = re.compile(r'src="(assets/[^"]+)"')
# 抓取文章页时混入的站点内部资源(如 /assets_dweb/logo@1x.png),离线渲染必为破图
SITE_ASSET_RE_MD = re.compile(r"!\[[^\]]*\]\(/[^)]+\)\s*")
SITE_ASSET_RE_HTML = re.compile(r"<img[^>]+src=\"/([^\"]+)\"[^>]*/?>\s*")


def download_remote_images(text: str, col_dir: Path) -> str:
    """把长文章正文里的远程图片下载到专栏 assets/ 并改写为本地路径。

    按 URL 的 md5 命名,同一 URL 只下载一次;失败保留远程 URL。
    """
    urls = set(re.findall(r"(?:!\[[^\]]*\]\(|<img[^>]+src=[\"'])(https?://[^)\s\"']+)", text))
    for url in urls:
        name = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        target = col_dir / "assets" / name
        # 已有同名缓存(允许不同扩展名)则直接复用
        existing = list(col_dir.glob(f"assets/{name}.*"))
        if existing:
            local = f"assets/{existing[0].name}"
        else:
            try:
                resp = requests.get(
                    url, timeout=20, stream=True,
                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/131.0.0.0"},
                )
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                ext = ".png" if "png" in ctype else ".gif" if "gif" in ctype else \
                    ".webp" if "webp" in ctype else ".jpg"
                target = col_dir / f"assets/{name}{ext}"
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() or target.stat().st_size == 0:
                    with open(target, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            fh.write(chunk)
                local = f"assets/{target.name}"
            except Exception:
                continue  # 下载失败:保留远程 URL,由渲染时的浏览器加载
        text = text.replace(url, local)
    return text


def embed_assets(html_text: str, col_dir: Path) -> str:
    """把 assets/ 相对路径替换为 base64 data URI。"""
    cache: dict = {}

    def _to_data_uri(match: re.Match) -> str:
        rel = match.group(1)
        if rel not in cache:
            p = (col_dir / rel).resolve()
            # 防路径逃逸
            if not p.is_file() or col_dir.resolve() not in p.parents:
                cache[rel] = None
                return match.group(0)
            mime = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
            }.get(p.suffix.lower(), "application/octet-stream")
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            cache[rel] = f"data:{mime};base64,{b64}"
        if cache[rel] is None:
            return match.group(0)
        return f'src="{cache[rel]}"'

    return IMG_RE.sub(_to_data_uri, html_text)


def column_to_pdf(col_dir: Path, out_pdf: Path, chrome: str, prefix: str) -> None:
    md_files = sorted(col_dir.glob("*.md"), key=lambda p: p.name)
    parts = []
    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        # 下载远程图片到本地(转 base64 内嵌,保证离线可读)
        text = download_remote_images(text, col_dir)
        # 剔除站点内部资源引用(如 /assets_dweb/logo@1x.png)
        text = SITE_ASSET_RE_MD.sub("", text)
        body = markdown.markdown(text, extensions=["extra", "sane_lists"])
        # 文内本地附件链接改为纯文本文件名(PDF 中无文件可链)
        body = re.sub(r'\[(.+?)\]\(([^)]+\.pdf)\)', r'\1', body)
        parts.append(f'<div class="article">{body}</div>')

    content = "".join(parts)
    # 剔除站点内部资源引用(md 阶段已删大部分;HTML 内联 <img> 在此兜底)
    content = SITE_ASSET_RE_HTML.sub("", content)
    html_doc = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(f'{prefix}-{col_dir.name}')}</title>"
        f"<style>{CSS}</style></head><body>"
        f"<header><h1 style='font-size:20pt;color:#b45309'>{html.escape(col_dir.name)}</h1>"
        f"<p class='meta-line'>{html.escape(prefix)} · 知识星球专栏合辑 · 共 {len(md_files)} 篇</p><hr></header>"
        + embed_assets(content, col_dir)
        + "</body></html>"
    )

    tmp_html = out_pdf.with_suffix(".tmp.html")
    tmp_html.write_text(html_doc, encoding="utf-8")
    # 渲染耗时与文章数成正比,给足余量
    timeout = max(300, len(md_files) * 20)
    try:
        result = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--no-pdf-header-footer", f"--print-to-pdf={out_pdf}", str(tmp_html)],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0 or not out_pdf.exists():
            raise RuntimeError(result.stderr[-400:])
    finally:
        tmp_html.unlink(missing_ok=True)


def find_chrome() -> str:
    """在常见路径中寻找可用的 Chrome/Chromium。"""
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    print("✗ 未找到 Chrome/Chromium,请先安装(google-chrome-stable 或 chromium)")
    sys.exit(1)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    export_dir = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    prefix = sys.argv[3] if len(sys.argv) > 3 else "复利人生"
    chrome = find_chrome()

    if not export_dir.is_dir():
        print(f"✗ 导出目录不存在: {export_dir}")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    col_dirs = sorted(d for d in export_dir.iterdir() if d.is_dir() and any(d.glob("*.md")))
    if not col_dirs:
        print("✗ 没有找到专栏文件夹")
        return 1

    for col_dir in col_dirs:
        out_pdf = out_dir / f"{prefix}-{col_dir.name}.pdf"
        print(f"▶ 生成 {out_pdf.name} ({len(list(col_dir.glob('*.md')))} 篇)...")
        column_to_pdf(col_dir, out_pdf, chrome, prefix)
        size_mb = out_pdf.stat().st_size / (1024 * 1024)
        print(f"  ✓ 完成 ({size_mb:.1f} MB)")

    print(f"\n===== 完成 =====\n输出目录: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

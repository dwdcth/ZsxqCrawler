"""将 columns_export 导出的专栏 Markdown 合集转换为 PDF(命令行入口)。

每个专栏文件夹 → 一个 PDF:<前缀>-<专栏名>.pdf。
渲染逻辑位于 backend.zsxq_pdf_exporter,与 Web 界面的 PDF 导出功能共用:
- 长文章正文的远程图片先下载到 assets/(按 URL md5 缓存),与本地图片
  一并 base64 内嵌,PDF 完全离线可读
- 站点内部资源引用(如 /assets_dweb/logo@1x.png)自动剔除,避免破图
- 使用系统 Chrome/Chromium 无头打印,中文与 emoji 正常渲染

用法:
    python scripts/export_columns_pdf.py <markdown导出目录> <PDF输出目录> [文件名前缀]

依赖即项目依赖(markdown、requests),无需额外安装。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.zsxq_pdf_exporter import export_group_pdfs  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    export_dir = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    prefix = sys.argv[3] if len(sys.argv) > 3 else "复利人生"

    if not export_dir.is_dir():
        print(f"✗ 导出目录不存在: {export_dir}")
        return 1

    try:
        result = export_group_pdfs(export_dir, out_dir, prefix)
    except (RuntimeError, ValueError) as e:
        print(f"✗ {e}")
        return 1

    print(f"\n===== 完成 =====")
    print(f"输出目录: {out_dir}")
    print(f"共 {len(result['pdfs'])} 个 PDF,合计 {result['total_mb']} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

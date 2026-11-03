"""批量导出指定群组的全部专栏文章为 Markdown(命令行入口)。

用法:
    uv run scripts/export_columns.py <group_id> [输出目录]

导出逻辑位于 backend.zsxq_columns_exporter,与 Web 界面的导出功能共用。
"""

import sys
from pathlib import Path

from backend.main import get_cookie_for_group, build_stealth_headers
from backend.zsxq_columns_exporter import run_columns_export


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print(__doc__)
        return 1
    group_id = sys.argv[1]

    cookie = get_cookie_for_group(str(group_id))
    if not cookie:
        print("✗ 未找到可用Cookie,请先配置账号(长文章将只能导出简介)")
    headers = build_stealth_headers(cookie) if cookie else {}

    project_root = Path(__file__).resolve().parent.parent
    out_root = Path(sys.argv[2]) if len(sys.argv) > 2 else project_root / "output" / f"columns_export_{group_id}"

    stats = run_columns_export(group_id, out_root, cookie or "", headers, log=print)

    print("\n===== 完成 =====")
    print(f"输出目录: {out_root}")
    print(f"专栏: {stats['columns']} 个,导出文章: {stats['articles']} 篇,无详情跳过: {stats['skipped_no_detail']} 篇")
    print(f"长文章全文抓取: 成功 {stats['fulltext_fetched']} 篇,失败 {stats['fulltext_failed']} 篇")
    print(f"附件下载: 成功 {stats['files_downloaded']} 个,失败 {stats['files_failed']} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())

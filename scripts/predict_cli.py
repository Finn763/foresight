"""python scripts/predict_cli.py "标题" [--closes YYYY-MM-DD] [--public] [--db path]

建题（缺省草稿 is_public=False）→ 跑完整管线 → 打印 JSON。
供 Pi Extension 经 pi.exec 调用；输出恒为单行 JSON，机器可解析。

用法：
  python scripts/predict_cli.py "美联储9月会加息吗" --closes 2026-09-17
  python scripts/predict_cli.py --publish 3          # 草稿转公开（审核门）

真实实现已迁入 predictor.cli（`foresight` 命令共用同一入口），本文件仅做兼容转发。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predictor.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

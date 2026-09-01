"""PyInstaller 打包入口（npm 分发的同一入口：predictor.cli:main）。

用法：
  pyinstaller --onefile --name foresight --paths src \
    --collect-all duckdb --collect-all pydantic_core --hidden-import dotenv \
    packaging/entry.py
"""

import sys

from predictor.cli import main

if __name__ == "__main__":
    sys.exit(main())

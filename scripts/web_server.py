"""预测结果展示页启动入口（跟随 scripts/*.py 约定，与 evolve.py 平行）。

用法：
  python scripts/web_server.py                # 内部模式 127.0.0.1:8765
  python scripts/web_server.py --mode public  # 对外战绩榜
  python scripts/web_server.py --host 0.0.0.0 --mode public  # 对外演示（注意安全）
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

from predictor.web.server import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Foresight 预测结果展示页")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mode", choices=["internal", "public"], default="internal")
    ap.add_argument("--db", default=None, help="DuckDB 路径（默认 Settings().db_path）")
    args = ap.parse_args()

    app = create_app(mode=args.mode)
    if args.db:
        app.state.db_path = Path(args.db)  # db_dep 闭包读 app.state.db_path（Task 2）
    print(f"Foresight 展示页 {args.mode} 模式: http://{args.host}:{args.port}")
    if args.mode == "public" and args.host != "127.0.0.1":
        print("注意: 对外模式 + 非本机绑定 = 局域网可见，仅限演示信任网段")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

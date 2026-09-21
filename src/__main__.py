"""命令行入口：python -m src --host 0.0.0.0 --port 8080 --db data/facility.db"""
from __future__ import annotations

import argparse

from .http_app import make_server


def main() -> None:
    parser = argparse.ArgumentParser(description="社区设施报修统筹服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/facility.db")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    httpd = make_server(args.host, args.port, args.db, debug=args.debug)
    print(f"设施报修统筹服务已启动: http://{args.host}:{args.port}  数据库: {args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.service.store.close()


if __name__ == "__main__":
    main()

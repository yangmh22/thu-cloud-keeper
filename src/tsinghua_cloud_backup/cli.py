from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="THU Cloud Keeper")
    parser.add_argument(
        "--frontend",
        choices=("tk", "tkinter", "web", "webui"),
        default="tkinter",
        help="选择前端界面。默认使用 Tkinter；使用 webui 启动本地 Web 控制台。",
    )
    parser.add_argument(
        "--webui",
        action="store_const",
        const="webui",
        dest="frontend",
        help="等同于 --frontend webui。",
    )
    parser.add_argument(
        "--tk",
        action="store_const",
        const="tkinter",
        dest="frontend",
        help="等同于 --frontend tkinter。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="WebUI 监听地址，仅在 --frontend webui 时生效。")
    parser.add_argument("--port", type=int, default=8765, help="WebUI 起始端口，仅在 --frontend webui 时生效。")
    parser.add_argument("--no-browser", action="store_true", help="启动 WebUI 时不自动打开浏览器。")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    frontend = "webui" if args.frontend in {"web", "webui"} else "tkinter"

    if frontend == "webui":
        from .web_console import main as web_main

        web_args = ["--host", args.host, "--port", str(args.port)]
        if args.no_browser:
            web_args.append("--no-browser")
        web_main(web_args)
        return

    from .app import main as tk_main

    tk_main()


if __name__ == "__main__":
    main()

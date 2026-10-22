"""SolidWorks automation over the Model Context Protocol."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="solidworks-automation", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server (default).")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")

    bridge = sub.add_parser("bridge", help="Expose SolidWorks on this Windows machine to a remote MCP server.")
    bridge.add_argument("--host", default="127.0.0.1", help="Use 0.0.0.0 to accept connections from the host Mac.")
    bridge.add_argument("--port", type=int, default=8765)
    bridge.add_argument("--token", help="Shared secret (default: SW_BRIDGE_TOKEN or a generated one).")

    args = parser.parse_args()
    if args.command == "bridge":
        from .bridge import run_bridge

        run_bridge(args.host, args.port, args.token)
        return

    from .server import mcp

    mcp.run(transport=getattr(args, "transport", "stdio"))

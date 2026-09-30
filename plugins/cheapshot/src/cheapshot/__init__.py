import sys


def main() -> None:
    # os.kill(pid, 0), used to check whether a claim's process is alive, sends CTRL_C_EVENT on
    # Windows instead of probing, and the claude CLI settings in claude.py are untested there.
    if sys.platform == "win32":
        sys.exit("cheapshot supports macOS and Linux only.")

    from cheapshot.server import mcp

    mcp.run()

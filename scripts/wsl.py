"""从 Windows 运行当前项目的 Linux 脚本，避免多层 Shell 引号改变命令。"""
import os
import subprocess
import sys


def decode(data):
    # wsl.exe 的提示为 UTF-16，Linux stderr 为 UTF-8；二者可能混在同一流中。
    if data.startswith(b"w\x00s\x00l\x00"):
        end = data.find(b"\n\x00")
        if end >= 0:
            return data[:end + 2].decode("utf-16-le", errors="replace") + decode(data[end + 2:])
    while data.startswith((b"\r\x00", b"\n\x00")):
        data = data[2:]
    return data.decode("utf-8", errors="replace")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if args == ["--stdin"]:
        command = ["bash", "-s"]
        content = sys.stdin.buffer.read().replace(b"\r\n", b"\n")
    elif args:
        command = args
        content = None
    else:
        raise SystemExit("usage: python scripts/wsl.py --stdin | <Linux command> [args]")
    result = subprocess.run(
        ["wsl.exe", "-d", os.environ.get("KND_WSL_DISTRO", "Ubuntu"), "-u", "root", "--exec", *command],
        input=content, capture_output=True,
    )
    sys.stdout.write(decode(result.stdout))
    sys.stderr.write(decode(result.stderr))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

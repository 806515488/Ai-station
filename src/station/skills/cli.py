"""技能 CLI 桥：宿主定位入口，具体命令参数与执行流程仍由技能自己实现。"""
from __future__ import annotations

import argparse

from station.skills.registry import get_registry


def run(skill_id: str, argv=None):
    entry = get_registry().entry_module(skill_id)
    command = getattr(entry, "cli", None)
    if not callable(command):
        raise ValueError(f"技能没有命令行入口：{skill_id}")
    return command(argv)


def archive_main():
    """保留原 archive-cli 命令名；业务实现随 archive 技能加载。"""
    return run("archive")


def main(argv=None):
    parser = argparse.ArgumentParser(description="执行一个已安装技能的命令行入口")
    parser.add_argument("skill_id", help="技能 ID，例如 archive")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="传给技能的原始参数")
    args = parser.parse_args(argv)
    return run(args.skill_id, args.arguments)


if __name__ == "__main__":
    main()

"""联网准备阶段下载文档处理资源；运行时 Loader 不允许临时联网补包。

用法：项目 Python scripts/prepare_document_resources.py --output-dir <本地目录>
设置 STATION_DOCUMENT_NLTK_DATA 指向该目录后，复制依赖/资源即可离线执行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="预置附件本地 NLP 资源")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "data/station/document-resources/nltk")
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    import nltk
    for package in ("punkt_tab", "averaged_perceptron_tagger_eng"):
        if not nltk.download(package, download_dir=str(directory), raise_on_error=True):
            raise RuntimeError(f"资源下载失败：{package}")
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            h = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(block)
            files.append({"path": path.relative_to(directory).as_posix(),
                          "bytes": path.stat().st_size, "sha256": h.hexdigest()})
    (directory / "manifest.json").write_text(json.dumps({"nltk_version": nltk.__version__, "files": files},
                                           ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已准备 {len(files)} 个资源文件：{directory}")


if __name__ == "__main__":
    main()

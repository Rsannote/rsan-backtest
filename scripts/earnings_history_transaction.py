"""T13b 多文件发布事务：先备齐全部新字节，失败恢复所有原文件。"""
import os
from pathlib import Path
import tempfile


def commit_files(root, changes):
    root = Path(root).resolve()
    updates = {}
    for path, content in changes.items():
        path = Path(path)
        if not path.is_absolute():
            path = root / path
        if path.is_symlink():raise ValueError('事务目标不能为符号链接')
        path=path.resolve()
        if not path.is_relative_to(root) or not isinstance(content, bytes):
            raise ValueError('事务路径或字节无效')
        if path.exists() and path.read_bytes() == content:
            continue
        updates[path] = content
    applied, created_dirs = [], []
    with tempfile.TemporaryDirectory(prefix='.t13b-transaction-', dir=root) as directory:
        stage = Path(directory)
        previous = {}
        for i, (path, content) in enumerate(updates.items()):
            target = stage / (str(i) + '.new')
            target.write_bytes(content)
            if path.exists():
                backup = stage / (str(i) + '.old')
                backup.write_bytes(path.read_bytes())
                previous[path] = backup
            else:
                previous[path] = None
        try:
            for i, path in enumerate(updates):
                missing = []
                parent = path.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for parent in reversed(missing):
                    parent.mkdir()
                    created_dirs.append(parent)
                os.replace(stage / (str(i) + '.new'), path)
                applied.append(path)
        except BaseException:
            for path in reversed(applied):
                if previous[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(previous[path], path)
            for directory in reversed(created_dirs):
                directory.rmdir()
            raise

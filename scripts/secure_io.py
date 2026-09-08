"""Bounded local file access, restrictive atomic writes and process-lifetime locks."""
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import stat
import sys


def checked_path(value, root=None):
    path = Path(value).expanduser()
    if '..' in path.parts:
        raise ValueError('PATH_UNSAFE: 路径不能包含 ..')
    path = Path(os.path.abspath(path))
    # macOS system aliases are trusted; arbitrary user/task symlinks are not.
    if sys.platform == 'darwin':
        for alias in ('/tmp', '/var', '/etc'):
            if path == Path(alias) or Path(alias) in path.parents:
                if Path(alias).is_symlink() and Path(alias).resolve() == Path('/private' + alias):
                    path = Path('/private' + str(path))
                break
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('PATH_UNSAFE: 拒绝符号链接或重解析点')
    if root is not None:
        base = checked_path(root)
        if path != base and base not in path.parents:
            raise ValueError('PATH_OUTSIDE: 路径超出指定目录')
    return path


@contextmanager
def parent_handle(path, create=False):
    """POSIX directory descriptors prevent following replaced path components."""
    path = checked_path(path)
    if os.name == 'nt':
        if create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        checked_path(path)
        yield None, path
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, path
    finally:
        os.close(fd)


def read_bytes(value, limit=32 * 1024 * 1024):
    with parent_handle(value) as (directory, path):
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(path if directory is None else path.name, flags, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('FILE_LIMIT: 文件类型或大小不受支持')
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError('FILE_LIMIT: 文件超过大小上限')
            return data


def atomic_write(value, data, *, overwrite=True):
    with parent_handle(value, create=True) as (directory, path):
        name = '.yintian-tmp-' + secrets.token_hex(12)
        target = path if directory is None else path.name
        temporary = path.parent / name if directory is None else name
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        fd = os.open(temporary, flags, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            checked_path(path)
            if overwrite:
                os.replace(temporary, target, src_dir_fd=directory, dst_dir_fd=directory)
            elif os.name != 'nt':
                os.link(temporary, target, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.unlink(temporary, dir_fd=directory)
            else:
                os.rename(temporary, target)  # Windows rename refuses existing targets.
            if directory is not None:
                os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


@contextmanager
def file_lock(value):
    """Persistent lock inode: never unlink it, even when no process holds the lock."""
    with parent_handle(value, create=True) as (directory, path):
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        fd = os.open(path if directory is None else path.name, flags, 0o600, dir_fd=directory)
    with os.fdopen(fd, 'r+b', buffering=0) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('PATH_UNSAFE: 锁文件必须是普通文件')
        acquired = False
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b'\0')
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            stream.seek(0)
            old = stream.read(256).decode('ascii', errors='replace')
            if old.startswith('pid='):
                # Respect a live writer running an older release.
                try:
                    pid = int(old.split()[0][4:])
                    if os.name == 'nt':
                        raise RuntimeError('TASK_BUSY: 旧版锁需先经 cleanup 检查')
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                except (ValueError, PermissionError):
                    raise RuntimeError('TASK_BUSY: 旧版任务锁无法确认，请先检查') from None
                else:
                    raise RuntimeError('TASK_BUSY: 旧版写入进程仍在运行')
            stream.seek(0)
            stream.write(b'yintian-os-lock/1\n')
            stream.truncate()
            yield
        except (BlockingIOError, PermissionError):
            raise RuntimeError('TASK_BUSY: 任务正由另一进程操作，请稍后重试') from None
        finally:
            if acquired:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

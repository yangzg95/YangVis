"""服务端粘贴（复制/剪切）的测试。

不连真实 SSH：用 FakeSftp 模拟 ``exists/rename/copy/remove`` 的语义，验证
同名避让的命名规则、目录自我嵌套的拦截，以及 rename 被拒时的复制回退。
依赖里没有 pytest-asyncio，统一在同步测试里 ``asyncio.run()``。
"""
import asyncio

import asyncssh
import pytest

from app.services.ops_sftp import SftpError, paste, paste_candidate


def run(coro):
    return asyncio.run(coro)


class FakeSftp:
    """只实现 ``paste`` 用到的那几个方法。``existing`` 是已占用的路径集合。"""

    def __init__(self, existing=(), dirs=(), home="/home/u"):
        self.existing = set(existing)
        self.dirs = set(dirs)
        self._home = home
        self.copied = []
        self.renamed = []
        self.removed = []

    async def getcwd(self):
        return self._home

    async def exists(self, path):
        return path in self.existing

    async def isdir(self, path):
        return path in self.dirs

    async def rename(self, src, dst):
        self.renamed.append((src, dst))

    async def copy(self, src, dst, **kwargs):
        self.copied.append((src, dst, kwargs))

    async def remove(self, path):
        self.removed.append(path)

    async def rmtree(self, path):
        self.removed.append(path)

    async def exit(self):
        return True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """``open_session`` 只需要 ``start_sftp_client()`` 返回一个 awaitable。"""

    def __init__(self, sftp):
        self._sftp = sftp

    def start_sftp_client(self):
        async def _go():
            return self._sftp

        return _go()


def do_paste(sftp, src, dst_dir, *, move=False):
    return run(paste(FakeConn(sftp), src, dst_dir, move=move))


# ---- 命名 ---------------------------------------------------------------------


def test_paste_candidate_keeps_extension_at_the_end():
    assert paste_candidate("report.pdf", 0) == "report_副本.pdf"
    assert paste_candidate("data", 0) == "data_副本"
    assert paste_candidate(".gitignore", 0) == ".gitignore_副本"
    assert paste_candidate("app.tar.gz", 2) == "app.tar_副本2.gz"


def test_free_name_is_used_as_is():
    assert do_paste(FakeSftp(), "/tmp/a.log", "/data") == "/data/a.log"


def test_collision_renames_with_copy_suffix():
    sftp = FakeSftp(existing={"/data/a.log", "/data/a_副本.log"})
    assert do_paste(sftp, "/tmp/a.log", "/data") == "/data/a_副本2.log"


def test_empty_dst_dir_means_home():
    assert do_paste(FakeSftp(), "/tmp/a.log", "") == "/home/u/a.log"


# ---- 拦截 ---------------------------------------------------------------------


def test_cannot_paste_a_directory_into_itself():
    with pytest.raises(SftpError, match="它自己"):
        do_paste(FakeSftp(), "/data/dir", "/data/dir/sub")


def test_cannot_paste_the_root():
    with pytest.raises(SftpError, match="根目录"):
        do_paste(FakeSftp(), "/", "/data")


def test_moving_into_its_own_directory_is_rejected_not_renamed():
    sftp = FakeSftp(existing={"/data/a.log"})
    with pytest.raises(SftpError, match="已经在这个目录"):
        do_paste(sftp, "/data/a.log", "/data", move=True)
    # 复制同样位置就是「在这目录里再来一份」，改名是预期行为。
    assert do_paste(sftp, "/data/a.log", "/data") == "/data/a_副本.log"


# ---- 复制 / 剪切 --------------------------------------------------------------


def test_copy_recurses_and_preserves():
    sftp = FakeSftp()
    do_paste(sftp, "/tmp/dir", "/data")
    assert sftp.copied == [("/tmp/dir", "/data/dir", {"recurse": True, "preserve": True})]
    assert not sftp.removed


def test_move_uses_rename():
    sftp = FakeSftp()
    do_paste(sftp, "/tmp/a.log", "/data", move=True)
    assert sftp.renamed == [("/tmp/a.log", "/data/a.log")]
    assert not sftp.copied


def test_move_falls_back_to_copy_and_remove_across_filesystems():
    sftp = FakeSftp()

    async def deny(*args):
        raise asyncssh.SFTPPermissionDenied(3, "")

    sftp.rename = deny
    assert do_paste(sftp, "/tmp/big.log", "/data", move=True) == "/data/big.log"
    assert sftp.copied
    assert sftp.removed == ["/tmp/big.log"]


def test_move_fallback_removes_a_copied_directory_tree():
    sftp = FakeSftp(dirs={"/tmp/tree"})

    async def deny(*args):
        raise asyncssh.SFTPFailure(4, "")

    sftp.rename = deny
    assert do_paste(sftp, "/tmp/tree", "/data", move=True) == "/data/tree"
    assert sftp.removed == ["/tmp/tree"]

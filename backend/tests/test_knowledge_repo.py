"""知识库 · 代码仓库：地址解析、凭据/TOFU、代码切片、增量对账与同步。

重点钉的是「错了也不报错、只是悄悄变烂」的边界：

- 地址：只放行 ssh 与匿名 http(s)，凭据不进 URL，危险协议/option 注入拒绝。
- 切片：分片正文必须是原文件对应行的逐字切片，行号对得上；任何分支不丢非空行。
- 对账：blob 没变就是零 embedding 调用；删除/跳过要连向量一起回收。
- 凭据：私钥只以密文落库、任何响应里不出现；同步时落盘 0600，三条退出路径必删。
- 级联：删仓库带走类型/文档/分片；越权读作 404；仓库文件不能单删。

git 相关用例用本机 ``git init`` 的本地仓库 + monkeypatch 地址解析跑真实的
clone/fetch/ls-tree（生产入口只放 ssh/http(s)，测试里把规范地址指到本地路径），
不起 ssh 服务；本机没有 git 时仅跳过这几条。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import (
    KbChunk,
    KbDocument,
    KbProject,
    KbRepo,
    KnowledgeType,
    ModelConfig,
)
from app.models.schemas import (
    CurrentUser,
    DocumentStatus,
    ModelPurpose,
    RepoCreate,
    RepoUpdate,
)
from app.routers import knowledge as knowledge_router
from app.services.knowledge import KnowledgeService, code_chunk, repo_sync, vectorstore
from app.services.knowledge import service as kb
from app.services.knowledge.repo_sync import (
    GitTarget,
    RepoError,
    RepoService,
    RepoStatus,
    RepoSyncError,
    gc_orphan_repo_dirs,
    known_host_fingerprints,
    parse_git_url,
    reset_stale_repo_syncs,
    sync_repository,
)

OWNER = 1
OTHER = 2
DIM = 4

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="本机没有 git，仓库集成用例跳过"
)


# ---- 通用夹具（与 test_knowledge 同构，独立维护）----------------------------


class _Embeddings:
    def __init__(self):
        self.doc_calls = 0

    async def aembed_documents(self, texts):
        self.doc_calls += 1
        return [[0.1] * DIM for _ in texts]

    async def aembed_query(self, text):
        return [0.1] * DIM


class _VectorStore:
    def __init__(self):
        self.deleted: list[int] = []
        self.upserted = 0

    async def delete_by_doc(self, name, owner_id, doc_id):
        self.deleted.append(doc_id)

    async def ensure_collection(self, name, vector_size, *, recreate=False):
        return None

    async def upsert_points(self, name, *, point_ids, vectors, payloads):
        self.upserted += len(point_ids)

    async def search(self, *a, **k):
        return []


@pytest.fixture()
def engine(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'repo.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=db_engine)
    yield db_engine
    db_engine.dispose()


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, future=True)


@pytest.fixture()
def db(maker, monkeypatch):
    session = maker()
    # 后台同步自己开 session：两个模块都用 from-import 绑了名字，都得替。
    monkeypatch.setattr("app.database.SessionLocal", maker)
    monkeypatch.setattr(repo_sync, "SessionLocal", maker)
    monkeypatch.setattr(kb, "SessionLocal", maker)
    yield session
    session.close()


@pytest.fixture()
def svc(db):
    return KnowledgeService(db, OWNER)


@pytest.fixture()
def repo_svc(db):
    return RepoService(db, OWNER)


@pytest.fixture()
def embeddings(monkeypatch):
    fake = _Embeddings()
    monkeypatch.setattr(kb, "build_embeddings", lambda config: fake)
    return fake


@pytest.fixture()
def vs(monkeypatch):
    fake = _VectorStore()
    monkeypatch.setattr(vectorstore, "delete_by_doc", fake.delete_by_doc)
    monkeypatch.setattr(vectorstore, "ensure_collection", fake.ensure_collection)
    monkeypatch.setattr(vectorstore, "upsert_points", fake.upsert_points)
    monkeypatch.setattr(vectorstore, "search", fake.search)
    return fake


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    path = tmp_path / "repos"
    monkeypatch.setattr(repo_sync.settings, "KB_REPO_DATA_DIR", str(path))
    return path


@pytest.fixture()
def project(db):
    item = KbProject(owner_id=OWNER, name="项目", description=None)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@pytest.fixture()
def embedding_model(db):
    config = ModelConfig(
        owner_id=OWNER,
        purpose=ModelPurpose.EMBEDDING.value,
        title="向量",
        model_name="bge-m3",
        base_url="https://embed.test/v1",
        is_default=True,
        vector_size=DIM,
        last_test_ok=True,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


# ---- 地址解析 ---------------------------------------------------------------


def test_parse_git_url_accepts_ssh_and_scp_like():
    t = parse_git_url("ssh://git@github.com:2222/org/repo.git")
    assert (t.protocol, t.host, t.port, t.user, t.path) == (
        "ssh",
        "github.com",
        2222,
        "git",
        "/org/repo.git",
    )
    assert t.canonical_url == "ssh://git@github.com:2222/org/repo.git"

    t2 = parse_git_url("git@github.com:org/repo.git")
    assert t2.protocol == "ssh"
    assert t2.user == "git"
    assert t2.canonical_url == "git@github.com:org/repo.git"

    # 省略用户时补 git。
    assert parse_git_url("github.com:org/repo").user == "git"


def test_parse_git_url_keeps_http_explicit_port():
    t = parse_git_url("https://gitlab.internal:8443/group/repo.git")
    assert t.protocol == "https"
    assert t.port == 8443
    assert t.canonical_url == "https://gitlab.internal:8443/group/repo.git"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "http://u:p@host/repo.git",  # 内嵌凭据
        "https://user@host/repo.git",
        "file:///srv/git/repo.git",  # 本地协议
        "ext::something",
        "git://host/repo",  # 非允许协议
        "-oMalicious",
        "ssh://-evil/repo",
        "https:///repo",
        "https://host/x\ncurl evil",
        r"C:\windows\path",
    ],
)
def test_parse_git_url_rejects_dangerous_shapes(raw):
    with pytest.raises(RepoError):
        parse_git_url(raw)


def test_host_allowlist_is_suffix_aware(monkeypatch):
    monkeypatch.setattr(
        repo_sync.settings, "KB_REPO_HOST_ALLOWLIST", "github.com,gitlab.internal"
    )
    parse_git_url("git@github.com:org/repo.git")
    parse_git_url("https://hq.gitlab.internal/g/r.git")
    with pytest.raises(RepoError):
        parse_git_url("git@evil.com:org/repo.git")
    # 不能用 evilgithub.com 冒充子域。
    with pytest.raises(RepoError):
        parse_git_url("https://evilgithub.com/org/repo")


# ---- deploy key 与主机指纹 ---------------------------------------------------


def test_generate_and_validate_keypair_roundtrip():
    priv, pub = repo_sync.generate_keypair_sync()
    assert "OPENSSH PRIVATE KEY" in priv
    assert pub.startswith("ssh-ed25519 ")

    canonical, public_line = repo_sync.validate_private_key_sync(priv)
    assert public_line == pub
    assert "OPENSSH PRIVATE KEY" in canonical

    with pytest.raises(RepoError):
        repo_sync.validate_private_key_sync("not a key")


def test_known_host_fingerprints_parses_real_ed25519_blob():
    import asyncssh
    import base64
    import hashlib

    key = asyncssh.generate_private_key("ssh-ed25519")
    public_line = key.export_public_key().decode().strip()  # ssh-ed25519 AAAA…
    blob = public_line.split()[1]
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    expected = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")

    known = f"gitlab.internal ssh-ed25519 {blob}\n# comment\n"
    assert known_host_fingerprints(known) == [expected]
    assert known_host_fingerprints("# only comment\n") == []


def test_ssh_command_pins_host_key_and_quotes_paths(tmp_path):
    key = tmp_path / "id key"
    known = tmp_path / "known hosts"
    first = repo_sync._ssh_command(key, known, accept_new=True)
    pinned = repo_sync._ssh_command(key, known, accept_new=False)
    assert "StrictHostKeyChecking=accept-new" in first
    assert "StrictHostKeyChecking=yes" in pinned
    assert "IdentitiesOnly=yes" in first and "BatchMode=yes" in first
    # 带空格的路径必须被引号包住，且反斜杠不能当转义符（posix 形式）。
    assert f'-i "{key.as_posix()}"' in first
    assert f'UserKnownHostsFile="{known.as_posix()}"' in first


def test_validate_http_token_rejects_blank_and_control_chars():
    assert repo_sync.validate_http_token("  tok ") == "tok"
    for bad in (None, "", "   ", "tok\n", "tok\rcurl x", "a\tb"):
        with pytest.raises(RepoError):
            repo_sync.validate_http_token(bad)
    assert repo_sync.normalize_http_username(None) is None
    assert repo_sync.normalize_http_username("   ") is None
    assert repo_sync.normalize_http_username(" ci-bot ") == "ci-bot"
    with pytest.raises(RepoError):
        repo_sync.normalize_http_username("bot\n")


def test_http_credential_file_and_env_keep_token_out_of_argv(tmp_path):
    target = parse_git_url("https://gitlab.internal:8443/group/repo.git")
    cred = tmp_path / "http_cred"
    repo_sync._write_http_credentials(cred, target, "ci-bot", "s3cret")
    text = cred.read_text(encoding="utf-8")
    # git credential-store 只认一行 URL 形态；显式端口的冒号百分号编码。
    assert text == "https://ci-bot:s3cret@gitlab.internal%3a8443\n"

    # 用户名缺省退化；默认端口不出现，特殊字符全部百分号编码（含 @ : / + =）。
    cred2 = tmp_path / "http_cred2"
    target2 = parse_git_url("https://github.com/org/repo.git")
    repo_sync._write_http_credentials(cred2, target2, None, "ab+/=CD.@ef")
    assert cred2.read_text() == (
        f"https://{repo_sync.DEFAULT_HTTP_USERNAME}:ab%2B%2F%3DCD.%40ef@github.com\n"
    )

    env = repo_sync._git_env(cwd_home=tmp_path, http_credentials=cred2)
    # argv 里永远没有令牌：只有指向 0600 文件的路径；system/global 配置已指空，
    # 这是唯一的 credential helper。
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == f"store --file={cred2.as_posix()}"
    assert all("s3cret" not in v and "ghp_x" not in v for v in env.values())


@requires_git
def test_git_credential_helper_serves_token_from_store_file(tmp_path):
    """真正跑一次 git credential fill：store 文件经 GIT_CONFIG_* 被消费。"""
    target = parse_git_url("https://gitee.test/group/repo.git")
    cred = tmp_path / "http_cred"
    repo_sync._write_http_credentials(cred, target, "luke", "gitee-token")
    env = repo_sync._git_env(cwd_home=tmp_path, http_credentials=cred)

    filled = subprocess.run(
        ["git", "credential", "fill"],
        input=b"protocol=https\nhost=gitee.test\n\n",
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        check=False,
    )
    out = filled.stdout.decode()
    assert "username=luke" in out
    assert "password=gitee-token" in out

    # 不匹配的主机拿不到凭据（防止一份令牌被发给错误主机）。
    miss = subprocess.run(
        ["git", "credential", "fill"],
        input=b"protocol=https\nhost=evil.example\n\n",
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        check=False,
    )
    assert "password=" not in miss.stdout.decode()


# ---- 代码切片：行号必须可信 --------------------------------------------------


def _line_map_is_verbatim(text, chunks):
    source_lines = text.rstrip("\n").split("\n")
    covered: set[int] = set()
    for chunk in chunks:
        body = "\n".join(chunk.content.splitlines()[1:])  # 去情报头
        expected = "\n".join(source_lines[chunk.line_start - 1 : chunk.line_end]).rstrip()
        assert body == expected, (chunk.symbol, repr(body), repr(expected))
        assert chunk.line_end >= chunk.line_start
        covered.update(range(chunk.line_start, chunk.line_end + 1))
    return covered


def _nonblank_line_numbers(text):
    return {
        index
        for index, line in enumerate(text.rstrip("\n").split("\n"), start=1)
        if line.strip()
    }


def test_split_python_symbols_and_line_accuracy():
    text = (
        '"""mod."""\n'
        "import os\n\n"
        "@decorator\n"
        "async def fetch(x):\n"
        "    return x\n\n\n"
        "class Service:\n"
        "    field = 1\n\n"
        "    def m(self):\n"
        "        return 1\n\n"
        "    def n(self):\n"
        "        return 2\n"
    )
    chunks = code_chunk.split_code(text, "python", "svc.py")
    symbols = [c.symbol for c in chunks]
    assert "fetch" in symbols
    assert "Service" in symbols  # 这个类没超预算，整体一片
    covered = _line_map_is_verbatim(text, chunks)
    # 装饰器属于函数片：fetch 片从 @decorator 行开始。
    fetch = next(c for c in chunks if c.symbol == "fetch")
    assert text.splitlines()[fetch.line_start - 1] == "@decorator"
    # 块边界处的空行允许不收，但任何有内容的行都必须在某个片里。
    assert _nonblank_line_numbers(text) <= covered


def test_split_oversized_python_class_breaks_into_methods():
    text = (
        "class Big:\n"
        "    field = 0\n\n"
        "    def one(self):\n"
        + "\n".join(f"        v{i} = {i}" for i in range(50))
        + "\n\n    def two(self):\n"
        + "\n".join(f"        w{i} = {i}" for i in range(50))
        + "\n"
    )
    chunks = code_chunk.split_code(text, "python", "big.py")
    method_symbols = {c.symbol for c in chunks}
    assert "Big.one" in method_symbols and "Big.two" in method_symbols
    assert "Big" in method_symbols  # 类头单独成片
    covered = _line_map_is_verbatim(text, chunks)
    assert _nonblank_line_numbers(text) <= covered


def test_split_brace_family_blocks_and_tail():
    text = (
        "package main\n\n"
        'import "fmt"\n\n'
        "// Foo doc.\n"
        "func Foo() {\n"
        "    if true {\n"
        '        fmt.Println("}")  // 字符串/注释里的括号不能影响深度\n'
        "    }\n"
        "}\n\n"
        "var Tail = 1\n"
    )
    chunks = code_chunk.split_code(text, "brace", "main.go")
    foo = next(c for c in chunks if c.symbol == "Foo")
    assert foo.line_start == 6 and foo.line_end == 10
    # 函数后的顶层 var 走声明式兜底，不丢。
    assert any(c.symbol is None for c in chunks)
    covered = _line_map_is_verbatim(text, chunks)
    assert _nonblank_line_numbers(text) <= covered


def test_split_huge_function_hard_splits_with_overlap():
    inner = "\n".join(f"    line{i} = {i}" for i in range(200))
    text = "def big():\n" + inner + "\n"
    chunks = code_chunk.split_code(text, "python", "big.py")
    # 单函数 201 行 > 80 行预算，被硬切成多片且都挂同一符号。
    big = [c for c in chunks if c.symbol == "big"]
    assert len(big) >= 3
    # 重叠：第二片起点 = 上一片断点 + 1 - 10。
    assert big[1].line_start == big[0].line_end + 1 - code_chunk.CODE_BLOCK_OVERLAP_LINES
    _line_map_is_verbatim(text, chunks)


def test_declarative_fallback_covers_every_line():
    text = "\n".join(f"key{i} = value{i}" for i in range(200)) + "\n"
    chunks = code_chunk.split_code(text, "declarative", "app.properties")
    assert all(c.symbol is None for c in chunks)
    covered = _line_map_is_verbatim(text, chunks)
    assert covered == set(range(1, 201))


def test_inspect_source_gates():
    lang, _ = code_chunk.inspect_source("a/b.py", b"x = 1\n", max_file_bytes=1000)
    assert lang == "python"

    for path, raw in (
        ("node_modules/x/a.js", b"y=2\n"),
        ("dist/app.min.js", b"z=3\n"),
        ("logo.png", b"\x89PNG\n"),
        ("a.lock", b"{}\n"),
        ("empty.py", b""),
        ("bin.py", b"\x00\x01\x02\n"),
    ):
        with pytest.raises(code_chunk.SourceSkip):
            code_chunk.inspect_source(path, raw, max_file_bytes=1000)

    with pytest.raises(code_chunk.SourceSkip):
        code_chunk.inspect_source("big.py", b"x = 1\n", max_file_bytes=4)
    with pytest.raises(code_chunk.SourceSkip):
        code_chunk.inspect_source(
            "min.py", ("a" * 3000 + "\n").encode(), max_file_bytes=10**6
        )

    # 用户 glob 与内置排除取并集。
    with pytest.raises(code_chunk.SourceSkip):
        code_chunk.inspect_source(
            "gen/a.py", b"x=1\n", max_file_bytes=1000, user_globs=["gen/*.py"]
        )


def test_decode_source_gb18030_fallback():
    raw = "# 中文注释\nx = 1\n".encode("gb18030")
    _lang, text = code_chunk.inspect_source("a.py", raw, max_file_bytes=1000)
    assert "中文注释" in text


# ---- 增量对账：四路 + 零嵌入 -----------------------------------------------


_PY_A = "def a():\n    return 1\n" * 3
_PY_B = "def b():\n    return 2\n" * 3
_PY_C = "def c():\n    return 3\n" * 3


def _repo_row(db, project, *, globs=None, name="demo", status=RepoStatus.READY):
    kb_type = KnowledgeType(
        owner_id=OWNER, project_id=project.id, name=name, description=None
    )
    db.add(kb_type)
    db.commit()
    db.refresh(kb_type)
    repo = KbRepo(
        owner_id=OWNER,
        type_id=kb_type.id,
        name=name,
        auth_type="none",
        remote_url=f"https://example.test/{name}.git",
        branch=None,
        exclude_globs=globs,
        status=status,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def _reconcile(monkeypatch, svc, repo, entries, files):
    async def fake_reader(work_dir, env, sha):
        return files[sha]

    monkeypatch.setattr(repo_sync, "_read_tree_file", fake_reader)
    return asyncio.run(
        repo_sync._reconcile(
            service=svc,
            repo=repo,
            work_dir=Path("."),
            git_env={},
            entries=entries,
            user_globs=code_chunk.parse_user_globs(repo.exclude_globs),
            config=svc.require_embedding_config(),
        )
    )


def test_reconcile_lifecycle_added_unchanged_changed_removed(
    svc, db, project, embedding_model, embeddings, vs, monkeypatch
):
    repo = _repo_row(db, project)
    sha_a, sha_b, sha_c = "a" * 40, "b" * 40, "c" * 40
    files = {sha_a: _PY_A.encode(), sha_b: _PY_B.encode(), sha_c: _PY_C.encode()}

    first = _reconcile(
        monkeypatch,
        svc,
        repo,
        [("100644", sha_a, "a.py"), ("100644", sha_b, "pkg/b.py")],
        files,
    )
    assert (first.added, first.changed, first.removed, first.skipped) == (2, 0, 0, 0)
    assert first.indexed_chunks >= 2
    assert embeddings.doc_calls == 2
    docs = svc.repo_doc_index(repo.id)
    assert set(docs) == {"a.py", "pkg/b.py"}
    assert docs["a.py"][1] == sha_a

    # 同样的树：blob 未变，一次 embedding 都不该再花。
    embeddings.doc_calls = 0
    second = _reconcile(
        monkeypatch,
        svc,
        repo,
        [("100644", sha_a, "a.py"), ("100644", sha_b, "pkg/b.py")],
        files,
    )
    assert (second.added, second.changed, second.removed) == (0, 0, 0)
    assert embeddings.doc_calls == 0

    # a 改了、c 新增、b 删除。index_document 每次索引会先清旧向量，故记录切点。
    sha_a2 = "a2" * 20
    files[sha_a2] = (_PY_A + "def a2():\n    return 9\n").encode()
    removed_doc_id = docs["pkg/b.py"][0].id
    deletes_before = list(vs.deleted)
    third = _reconcile(
        monkeypatch,
        svc,
        repo,
        [("100644", sha_a2, "a.py"), ("100644", sha_c, "c.py")],
        files,
    )
    assert (third.added, third.changed, third.removed) == (1, 1, 1)
    assert embeddings.doc_calls == 2
    # 对账删除那一步确实回收了 b 的向量（与索引前自清区分）。
    assert removed_doc_id in vs.deleted[len(deletes_before) :]
    docs = svc.repo_doc_index(repo.id)
    assert set(docs) == {"a.py", "c.py"}
    assert docs["a.py"][1] == sha_a2

    # chunk 行带源码定位。
    chunk = db.scalars(
        select(KbChunk).where(KbChunk.doc_id == docs["a.py"][0].id)
    ).first()
    assert chunk.line_start and chunk.line_end and chunk.symbol


def test_reconcile_skip_removes_doc_that_newly_matches_exclusion(
    svc, db, project, embedding_model, embeddings, vs, monkeypatch
):
    repo = _repo_row(db, project)
    sha = "d" * 40
    files = {sha: _PY_A.encode()}
    first = _reconcile(monkeypatch, svc, repo, [("100644", sha, "gen.py")], files)
    assert first.added == 1
    doc_id = svc.repo_doc_index(repo.id)["gen.py"][0].id

    # 第二次同步把 gen.py 加进用户排除：旧账要删掉并算 removed，不残留
    # （即使 blob 没变，unchanged 短路也不能让它活下来）。
    repo.exclude_globs = "gen.py"
    db.commit()
    deletes_before = list(vs.deleted)
    # 排除判定前置到 removed 循环：unchanged 短路之前就把旧账删掉，删除即
    # removed，候选循环里不会再为它计一次 skipped。
    second = _reconcile(monkeypatch, svc, repo, [("100644", sha, "gen.py")], files)
    assert (second.added, second.changed, second.removed, second.skipped) == (0, 0, 1, 0)
    assert db.get(KbDocument, doc_id) is None
    assert vs.deleted[len(deletes_before) :] == [doc_id]

    # 始终在排除规则内的文件只算 skipped，没有文档。
    third = _reconcile(
        monkeypatch,
        svc,
        repo,
        [("100644", sha, "gen.py"), ("100644", "e" * 40, "node_modules/x.js")],
        {**files, "e" * 40: b"var x = 1;\n"},
    )
    assert (third.added, third.removed) == (0, 0)
    assert third.skipped == 2


def test_reconcile_one_file_failure_does_not_kill_sync(
    svc, db, project, embedding_model, embeddings, vs, monkeypatch
):
    repo = _repo_row(db, project)

    async def fail_documents(texts):
        # a.py 的片情报头含路径，据此只炸 a.py。
        if any("a.py" in t for t in texts):
            raise RuntimeError("provider down")
        embeddings.doc_calls += 1
        return [[0.1] * DIM for _ in texts]

    embeddings.aembed_documents = fail_documents
    files = {"a" * 40: _PY_A.encode(), "b" * 40: _PY_B.encode()}
    result = _reconcile(
        monkeypatch,
        svc,
        repo,
        [("100644", "a" * 40, "a.py"), ("100644", "b" * 40, "b.py")],
        files,
    )
    assert result.added == 2
    assert result.skipped == 1  # a.py 单篇失败落成 skip
    docs = svc.repo_doc_index(repo.id)
    assert db.get(KbDocument, docs["a.py"][0].id).status == DocumentStatus.ERROR.value
    assert db.get(KbDocument, docs["b.py"][0].id).status == DocumentStatus.READY.value


# ---- 本地 git 仓库的真同步 ---------------------------------------------------


def _git(args, cwd):
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env=env,
    )


def _make_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "-q"], remote)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], remote)
    (remote / "a.py").write_text(_PY_A, encoding="utf-8")
    pkg = remote / "pkg"
    pkg.mkdir()
    (pkg / "b.py").write_text(_PY_B, encoding="utf-8")
    (remote / "notes.txt").write_text("not code\n", encoding="utf-8")
    _git(["add", "."], remote)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], remote)
    return remote


def _local_target(remote: Path, *, ssh: bool = False) -> GitTarget:
    # 直构目标对象，canonical_url 指向本地 posix 路径：git 自己完全支持，只是生产
    # 入口的地址解析不放行本地协议。用 posix 斜杠，避免 Windows 盘符被当成
    # scp-like 地址二次解析。
    canonical = remote.resolve().as_posix()
    if ssh:
        return GitTarget("ssh", "local.test", None, "git", str(remote), canonical)
    return GitTarget("http", "local.test", None, None, str(remote), canonical)


def _create_repo(
    monkeypatch,
    repo_svc,
    project,
    remote: Path,
    *,
    auth="none",
    key=None,
    token=None,
    username=None,
    globs=None,
    name="demo",
):
    monkeypatch.setattr(
        repo_sync,
        "parse_git_url",
        lambda raw: _local_target(remote, ssh=(auth == "ssh_key")),
    )
    payload = RepoCreate(
        project_id=project.id,
        remote_url="https://local.test/demo.git",
        auth_type=auth,
        private_key=key,
        http_token=token,
        http_username=username,
        name=name,
        exclude_globs=globs,
    )
    return repo_svc.create(payload)


@requires_git
def test_sync_end_to_end_incremental_and_cascade(
    monkeypatch,
    maker,
    db,
    repo_svc,
    project,
    embedding_model,
    embeddings,
    vs,
    data_dir,
    tmp_path,
):
    remote = _make_remote(tmp_path)
    repo = _create_repo(monkeypatch, repo_svc, project, remote)

    # 同步在另一个 session/连接里提交；SQLite 同一 session 的读事务可能持有旧
    # 快照，校验一律开新 session 看台账。
    def doc_paths(repo_id):
        with maker() as verify:
            return set(KnowledgeService(verify, OWNER).repo_doc_index(repo_id))

    assert asyncio.run(repo_sync.run_repo_sync(OWNER, repo.id)) is True
    db.refresh(repo)
    assert repo.status == RepoStatus.READY
    assert repo.head_commit and len(repo.head_commit) == 40
    assert repo.branch == "main"
    result = repo.last_sync_result
    assert result["added"] == 2 and result["removed"] == 0
    # notes.txt 不是受支持的代码扩展名，被跳过。
    assert doc_paths(repo.id) == {"a.py", "pkg/b.py"}

    # 工作区与 0700 根目录确实落在 data dir 下。
    root = repo_sync.repo_root(OWNER, repo.id)
    assert (root / "work" / ".git").exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700

    # 第二次同步：内容未变，零嵌入。
    embeddings.doc_calls = 0
    assert asyncio.run(repo_sync.run_repo_sync(OWNER, repo.id)) is True
    assert embeddings.doc_calls == 0

    # 远端改动：改 a.py、加 c.py、删 b.py。
    (remote / "a.py").write_text(_PY_A + "def newer():\n    return 0\n", encoding="utf-8")
    (remote / "c.py").write_text(_PY_C, encoding="utf-8")
    (remote / "pkg" / "b.py").unlink()
    _git(["add", "-A"], remote)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "change"], remote)

    assert asyncio.run(repo_sync.run_repo_sync(OWNER, repo.id)) is True
    db.refresh(repo)
    r = repo.last_sync_result
    assert (r["added"], r["changed"], r["removed"]) == (1, 1, 1)
    assert doc_paths(repo.id) == {"a.py", "c.py"}

    # 级联删除：类型/文档/分片/本地目录全收走。
    with maker() as verify:
        doc_id = KnowledgeService(verify, OWNER).repo_doc_index(repo.id)["a.py"][0].id
    deleted_id, doc_ids = repo_svc.delete(repo.id)
    assert deleted_id == repo.id and doc_id in doc_ids
    with maker() as verify:
        assert verify.get(KbRepo, repo.id) is None
        assert verify.get(KbDocument, doc_id) is None
    # 服务层 delete 不碰磁盘；本地目录由 DELETE 接口在后台调 rmtree_repo_dir 收走，
    # 这里按接口编排补上同样一步。
    repo_sync.rmtree_repo_dir(OWNER, repo.id)
    assert not root.exists()


@requires_git
def test_sync_self_heals_after_remote_branch_rename(
    monkeypatch, db, repo_svc, project, embedding_model, embeddings, vs, data_dir, tmp_path
):
    remote = _make_remote(tmp_path)
    repo = _create_repo(monkeypatch, repo_svc, project, remote)
    assert asyncio.run(repo_sync.run_repo_sync(OWNER, repo.id)) is True
    db.refresh(repo)
    assert repo.branch == "main"

    _git(["branch", "-m", "main", "trunk"], remote)
    # 注意 -m 必须吃紧跟的参数：-qm --allow-empty 会把 --allow-empty 当成提交信息。
    _git(
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
        remote,
    )

    # 旧分支名 fetch 失败 → 全新克隆；--branch main 也失败 → 退默认分支 trunk。
    assert asyncio.run(repo_sync.run_repo_sync(OWNER, repo.id)) is True
    db.refresh(repo)
    assert repo.status == RepoStatus.READY
    assert repo.branch == "trunk"


def test_run_repo_sync_returns_false_while_running():
    repo_id = 999001
    repo_sync._running.add(repo_id)
    try:
        assert asyncio.run(repo_sync.run_repo_sync(OWNER, repo_id)) is False
    finally:
        repo_sync._running.discard(repo_id)


def test_sync_error_path_deletes_key_and_marks_error(
    monkeypatch, db, repo_svc, project, embedding_model, data_dir, tmp_path
):
    """私钥落盘 0600，clone 失败后 id_key 必须消失、仓库行落 error。"""
    priv, _pub = repo_sync.generate_keypair_sync()
    remote = tmp_path / "nonexistent-origin"
    repo = _create_repo(
        monkeypatch, repo_svc, project, remote, auth="ssh_key", key=priv
    )

    seen: dict = {}

    async def fake_run(args, *, cwd=None, env=None, timeout=60.0):
        key = Path(env["GIT_SSH_COMMAND"].split('"')[1])
        seen["key_exists"] = key.exists()
        if sys.platform != "win32":
            seen["mode"] = stat.S_IMODE(key.stat().st_mode)
        return 1, b"", b"simulated clone failure"

    monkeypatch.setattr(repo_sync, "_run", fake_run)
    with pytest.raises(RepoSyncError):
        asyncio.run(sync_repository(OWNER, repo.id))

    db.refresh(repo)
    assert repo.status == RepoStatus.ERROR
    assert "simulated clone failure" in (repo.error_msg or "")
    assert seen["key_exists"] is True
    assert seen.get("mode") in (None, 0o600)
    assert not repo_sync.repo_root(OWNER, repo.id).joinpath("id_key").exists()


def test_sync_timeout_marks_error_and_removes_key(
    monkeypatch, db, repo_svc, project, embedding_model, data_dir, tmp_path
):
    priv, _pub = repo_sync.generate_keypair_sync()
    repo = _create_repo(
        monkeypatch, repo_svc, project, tmp_path / "nope", auth="ssh_key", key=priv
    )
    monkeypatch.setattr(repo_sync.settings, "KB_REPO_SYNC_TIMEOUT", 0.2)

    async def slow_run(args, *, cwd=None, env=None, timeout=60.0):
        await asyncio.sleep(1.0)
        return 0, b"", b""

    monkeypatch.setattr(repo_sync, "_run", slow_run)
    # task 外壳兜住总超时并落 error。
    asyncio.run(repo_sync.repo_sync_task(OWNER, repo.id))
    db.refresh(repo)
    assert repo.status == RepoStatus.ERROR
    assert not repo_sync.repo_root(OWNER, repo.id).joinpath("id_key").exists()


@requires_git
def test_probe_remote_against_local_repo(monkeypatch, db, data_dir, tmp_path):
    remote = _make_remote(tmp_path)
    monkeypatch.setattr(repo_sync, "parse_git_url", lambda raw: _local_target(remote))
    branch, sha, host, known_hosts, fingerprint = asyncio.run(
        repo_sync.probe_remote("https://local.test/demo.git", private_key=None)
    )
    assert branch == "main"
    assert len(sha) == 40
    assert host == "local.test"
    assert known_hosts == "" and fingerprint == ""


@requires_git
def test_probe_remote_with_http_token_uses_credential_store(
    monkeypatch, db, data_dir, tmp_path
):
    remote = _make_remote(tmp_path)
    monkeypatch.setattr(repo_sync, "parse_git_url", lambda raw: _local_target(remote))

    seen: dict = {}
    original_run = repo_sync._run

    async def spying_run(args, *, cwd=None, env=None, timeout=60.0):
        if args[:2] == ["git", "ls-remote"]:
            # 临时 credential 文件在探测期间存在，结束即随临时目录销毁。
            store = env["GIT_CONFIG_VALUE_0"].removeprefix("store --file=")
            seen["cred_during_probe"] = Path(store).read_text(encoding="utf-8")
        return await original_run(args, cwd=cwd, env=env, timeout=timeout)

    monkeypatch.setattr(repo_sync, "_run", spying_run)
    branch, sha, host, known_hosts, fingerprint = asyncio.run(
        repo_sync.probe_remote(
            "http://local.test/demo.git",
            http_token=" probe-tok ",
            http_username="ci",
        )
    )
    assert branch == "main" and len(sha) == 40 and host == "local.test"
    assert known_hosts == "" and fingerprint == ""  # http(s) 没有主机指纹
    # 一行 URL 形态：令牌经 strip 后落盘，绝不进 argv；文件只在探测期间存在。
    assert seen["cred_during_probe"] == "http://ci:probe-tok@local.test\n"

    # SSH 地址给令牌直接拒绝，不会去碰远端。
    monkeypatch.setattr(
        repo_sync, "parse_git_url", lambda raw: _local_target(remote, ssh=True)
    )
    with pytest.raises(RepoError):
        asyncio.run(
            repo_sync.probe_remote("ssh://local.test/demo.git", http_token="t")
        )


@requires_git
def test_sync_with_http_token_auth_against_local_repo(
    monkeypatch,
    maker,
    db,
    repo_svc,
    project,
    embedding_model,
    embeddings,
    vs,
    data_dir,
    tmp_path,
):
    """令牌认证同步真跑一遍：credential helper 配置不能影响本地克隆，凭据文件收尾删除。"""
    remote = _make_remote(tmp_path)
    repo = _create_repo(
        monkeypatch,
        repo_svc,
        project,
        remote,
        auth="http_token",
        token="sync-tok",
        username="robot",
    )
    assert asyncio.run(repo_sync.run_repo_sync(OWNER, repo.id)) is True
    db.refresh(repo)
    assert repo.status == RepoStatus.READY
    root = repo_sync.repo_root(OWNER, repo.id)
    assert not (root / "http_cred").exists()
    with maker() as verify:
        docs = KnowledgeService(verify, OWNER).repo_doc_index(repo.id)
    assert set(docs) == {"a.py", "pkg/b.py"}


def test_sync_error_path_deletes_http_credentials(
    monkeypatch, db, repo_svc, project, embedding_model, data_dir, tmp_path
):
    repo = _create_repo(
        monkeypatch,
        repo_svc,
        project,
        tmp_path / "nonexistent-origin",
        auth="http_token",
        token="tok",
    )
    seen: dict = {}

    async def fake_run(args, *, cwd=None, env=None, timeout=60.0):
        store = env.get("GIT_CONFIG_VALUE_0", "")
        if store.startswith("store --file="):
            cred = Path(store.split("=", 1)[1])
            seen["cred_exists"] = cred.exists()
            seen["content"] = cred.read_text(encoding="utf-8")
        return 1, b"", b"simulated clone failure"

    monkeypatch.setattr(repo_sync, "_run", fake_run)
    with pytest.raises(RepoSyncError):
        asyncio.run(sync_repository(OWNER, repo.id))

    db.refresh(repo)
    assert repo.status == RepoStatus.ERROR
    assert "simulated clone failure" in (repo.error_msg or "")
    assert seen["cred_exists"] is True
    assert seen["content"] == (
        f"http://{repo_sync.DEFAULT_HTTP_USERNAME}:tok@local.test\n"
    )
    assert not repo_sync.repo_root(OWNER, repo.id).joinpath("http_cred").exists()


def test_reset_stale_and_gc_orphan_dirs(db, project, data_dir):
    r1 = _repo_row(db, project, name="卡住的", status=RepoStatus.SYNCING)
    r2 = _repo_row(db, project, name="就绪的", status=RepoStatus.READY)
    # 一个台账已不存在的孤儿目录。
    orphan = repo_sync.repo_root(OWNER, 999999)
    orphan.mkdir(parents=True)
    (orphan / "work").mkdir()
    live = repo_sync.repo_root(OWNER, r1.id)
    live.mkdir(parents=True)

    assert reset_stale_repo_syncs(db) == 1
    db.refresh(r1)
    assert r1.status == RepoStatus.ERROR
    db.refresh(r2)
    assert r2.status == RepoStatus.READY

    assert gc_orphan_repo_dirs(db) == 1
    assert not orphan.exists()
    assert live.exists()


# ---- RepoService 台账规则 ----------------------------------------------------


def test_create_makes_one_to_one_type_and_unique_names(db, repo_svc, project):
    r1 = repo_svc.create(
        RepoCreate(
            project_id=project.id,
            remote_url="https://local.test/demo.git",
            auth_type="none",
        )
    )
    r2 = repo_svc.create(
        RepoCreate(
            project_id=project.id,
            remote_url="https://local.test/demo.git",
            auth_type="none",
        )
    )
    assert r1.type_id != r2.type_id
    assert repo_svc.get_by_type(r1.type_id).id == r1.id
    names = {t.name for t in db.scalars(select(KnowledgeType)).all()}
    assert {"demo", "demo-2"} <= names

    item = repo_svc.to_item(r1)
    assert "private_key" not in item and "private_key_enc" not in item
    assert item["has_credential"] is False
    assert item["project_id"] == project.id


def test_create_rejects_protocol_auth_mismatch(db, repo_svc, project):
    priv, _ = repo_sync.generate_keypair_sync()
    # http(s) 地址不许挂 key。
    with pytest.raises(RepoError):
        repo_svc.create(
            RepoCreate(
                project_id=project.id,
                remote_url="https://local.test/demo.git",
                auth_type="ssh_key",
                private_key=priv,
            )
        )
    # ssh 地址不许免认证。
    with pytest.raises(RepoError):
        repo_svc.create(
            RepoCreate(
                project_id=project.id,
                remote_url="git@local.test:o/d.git",
                auth_type="none",
            )
        )


def test_update_validates_auth_protocol_coherence(db, repo_svc, project):
    priv, _ = repo_sync.generate_keypair_sync()
    repo = repo_svc.create(
        RepoCreate(
            project_id=project.id,
            remote_url="git@local.test:o/d.git",
            auth_type="ssh_key",
            private_key=priv,
        )
    )
    with pytest.raises(RepoError):
        repo_svc.update(repo.id, RepoUpdate(auth_type="none"))
    # 失败的校验不 commit，但内存里的改动还挂在 session 上：真实请求结束即丢弃，
    # 这里显式回滚到持久化状态再做下一次操作。
    db.rollback()
    # 普通改名没问题。
    updated = repo_svc.update(repo.id, RepoUpdate(name="新名字"))
    assert updated.name == "新名字"


def test_create_with_http_token_encrypts_and_never_echoes(db, repo_svc, project):
    from app.crypto import decrypt

    repo = repo_svc.create(
        RepoCreate(
            project_id=project.id,
            remote_url="https://gitee.test/org/demo.git",
            auth_type="http_token",
            http_token="  gitee-token  ",
            http_username="luke",
        )
    )
    # 令牌去空白后 Fernet 落库；用户名是普通列。
    assert repo.http_token_enc is not None
    assert decrypt(repo.http_token_enc) == "gitee-token"
    assert repo.http_username == "luke"
    assert repo.private_key_enc is None

    item = repo_svc.to_item(repo)
    assert item["auth_type"] == "http_token"
    assert item["has_credential"] is True
    serialized = repr(item)
    assert "gitee-token" not in serialized
    assert "http_token" not in item and "http_token_enc" not in item

    # 用户名可缺省。
    other = repo_svc.create(
        RepoCreate(
            project_id=project.id,
            remote_url="https://gitlab.test/org/demo2.git",
            auth_type="http_token",
            http_token="glpat-x",
        )
    )
    assert other.http_username is None
    assert repo_svc.to_item(other)["has_credential"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        # 选了令牌认证却没给令牌
        {"remote_url": "https://x.test/d.git", "auth_type": "http_token"},
        # 匿名方式却带了令牌
        {
            "remote_url": "https://x.test/d.git",
            "auth_type": "none",
            "http_token": "t",
        },
        # SSH 地址配令牌
        {
            "remote_url": "git@x.test:d.git",
            "auth_type": "http_token",
            "http_token": "t",
        },
        # http(s) 地址配 SSH 私钥
        {
            "remote_url": "https://x.test/d.git",
            "auth_type": "http_token",
            "http_token": "t",
            "private_key": "k",
        },
        # 令牌含换行（credential-store 行格式注入）
        {
            "remote_url": "https://x.test/d.git",
            "auth_type": "http_token",
            "http_token": "t\npassword=evil",
        },
    ],
)
def test_create_rejects_invalid_token_combinations(db, repo_svc, project, kwargs):
    with pytest.raises(RepoError):
        repo_svc.create(RepoCreate(project_id=project.id, **kwargs))
    db.rollback()


def test_update_token_lifecycle_replace_switch_and_clear(db, repo_svc, project):
    repo = repo_svc.create(
        RepoCreate(
            project_id=project.id,
            remote_url="https://x.test/org/d.git",
            auth_type="none",
        )
    )

    # 只切认证方式不给令牌：自洽校验拒绝，不能存出同步必炸的行。
    with pytest.raises(RepoError):
        repo_svc.update(repo.id, RepoUpdate(auth_type="http_token"))
    db.rollback()

    # 令牌与认证方式一起给：切过去。
    repo_svc.update(
        repo.id,
        RepoUpdate(auth_type="http_token", http_token="tok-1", http_username="ci"),
    )
    db.refresh(repo)
    from app.crypto import decrypt

    assert repo.auth_type == "http_token" and repo.http_username == "ci"
    assert decrypt(repo.http_token_enc) == "tok-1"

    # 令牌缺省（None）=不变，只改用户名。
    repo_svc.update(repo.id, RepoUpdate(http_username="robot"))
    db.refresh(repo)
    assert repo.http_username == "robot"
    assert decrypt(repo.http_token_enc) == "tok-1"

    # 非空令牌=整体替换。
    repo_svc.update(repo.id, RepoUpdate(http_token="tok-2"))
    db.refresh(repo)
    assert decrypt(repo.http_token_enc) == "tok-2"

    # 切回匿名：令牌与用户名都必须清空。
    repo_svc.update(repo.id, RepoUpdate(auth_type="none"))
    db.refresh(repo)
    assert repo.auth_type == "none"
    assert repo.http_token_enc is None and repo.http_username is None

    # SSH 仓库拒绝接收令牌。
    ssh_repo = repo_svc.create(
        RepoCreate(
            project_id=project.id,
            remote_url="git@x.test:o/s.git",
            auth_type="ssh_key",
            private_key=repo_sync.generate_keypair_sync()[0],
        )
    )
    with pytest.raises(RepoError):
        repo_svc.update(ssh_repo.id, RepoUpdate(http_token="t"))
    db.rollback()


def test_owner_isolation_is_404(db, repo_svc, project):
    theirs_project = KbProject(owner_id=OTHER, name="p", description=None)
    db.add(theirs_project)
    db.commit()
    kb_type = KnowledgeType(
        owner_id=OTHER, project_id=theirs_project.id, name="t", description=None
    )
    db.add(kb_type)
    db.commit()
    repo = KbRepo(
        owner_id=OTHER,
        type_id=kb_type.id,
        name="t",
        auth_type="none",
        remote_url="https://x/y.git",
    )
    db.add(repo)
    db.commit()

    other_svc = RepoService(db, OTHER)
    with pytest.raises(LookupError):
        repo_svc.get(repo.id)
    assert other_svc.get(repo.id).id == repo.id


# ---- HTTP 接口 --------------------------------------------------------------


def _ready_project(db):
    project = KbProject(owner_id=OWNER, name="接口项目", description=None)
    db.add(project)
    db.commit()
    db.refresh(project)
    db.add(
        ModelConfig(
            owner_id=OWNER,
            purpose=ModelPurpose.EMBEDDING.value,
            title="向量",
            model_name="bge-m3",
            base_url="https://embed.test/v1",
            is_default=True,
            vector_size=DIM,
            last_test_ok=True,
        )
    )
    db.commit()
    return project


@pytest.fixture()
def client(db, monkeypatch):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=OWNER, username="tester"
    )
    queued: list[tuple] = []
    monkeypatch.setattr(
        knowledge_router,
        "repo_sync_task",
        lambda owner_id, repo_id: queued.append(("sync", owner_id, repo_id)),
    )
    monkeypatch.setattr(
        knowledge_router, "rmtree_repo_dir", lambda owner_id, repo_id: None
    )
    try:
        yield TestClient(app), queued
    finally:
        app.dependency_overrides.clear()


def test_api_keypair_and_create_never_echo_private_key(client, db):
    test_client, _queued = client
    project = _ready_project(db)

    body = test_client.post("/api/knowledge/repos/keypair").json()
    assert body["code"] == 0
    assert body["data"]["private_key"].startswith("-----BEGIN")

    priv, _pub = repo_sync.generate_keypair_sync()
    created = test_client.post(
        "/api/knowledge/repos",
        json={
            "project_id": project.id,
            "remote_url": "git@local.test:org/demo.git",
            "auth_type": "ssh_key",
            "private_key": priv,
            "name": "demo",
        },
    ).json()
    assert created["code"] == 0
    item = created["data"]
    assert item["has_credential"] is True
    assert "private_key" not in item
    assert item["public_key"].startswith("ssh-ed25519 ")
    assert item["status"] == RepoStatus.PENDING

    listed = test_client.get(f"/api/knowledge/repos?project_id={project.id}").json()
    assert listed["data"]["total"] == 1
    assert "private_key" not in listed["data"]["items"][0]


def test_api_create_repo_validates_input(client, db):
    test_client, _queued = client
    project = _ready_project(db)  # 先过门：embedding 就绪依赖在处理函数之前
    body = test_client.post(
        "/api/knowledge/repos",
        json={"project_id": project.id, "remote_url": "file:///etc/passwd"},
    ).json()
    assert body["code"] == -1
    assert body["data"] is None


def test_api_test_connection_returns_branch_and_sha(client, db, monkeypatch):
    test_client, _queued = client
    _ready_project(db)

    seen: dict = {}

    async def fake_probe(
        remote_url,
        *,
        private_key=None,
        branch=None,
        known_hosts_text=None,
        http_token=None,
        http_username=None,
    ):
        seen["token"] = http_token
        seen["username"] = http_username
        return "main", "a" * 40, "local.test", "known-line", "SHA256:abc"

    monkeypatch.setattr(knowledge_router, "probe_remote", fake_probe)
    body = test_client.post(
        "/api/knowledge/test-connection",
        json={"remote_url": "git@local.test:org/demo.git", "private_key": "k"},
    ).json()
    assert body["code"] == 0
    assert body["data"] == {
        "default_branch": "main",
        "head_sha": "a" * 40,
        "host": "local.test",
        "host_fingerprint": "SHA256:abc",
        "known_hosts": "known-line",
    }

    # 令牌探测：http_token/http_username 原样透传到传输层。
    token_body = test_client.post(
        "/api/knowledge/test-connection",
        json={
            "remote_url": "https://local.test/org/demo.git",
            "http_token": "pat-x",
            "http_username": "ci",
        },
    ).json()
    assert token_body["code"] == 0
    assert seen["token"] == "pat-x" and seen["username"] == "ci"


def test_api_create_http_token_repo_never_echoes_token(client, db):
    test_client, queued = client
    project = _ready_project(db)

    created = test_client.post(
        "/api/knowledge/repos",
        json={
            "project_id": project.id,
            "remote_url": "https://gitee.test/org/demo.git",
            "auth_type": "http_token",
            "http_token": "gitee-pat",
            "http_username": "luke",
            "name": "demo",
        },
    ).json()
    assert created["code"] == 0, created
    item = created["data"]
    assert item["auth_type"] == "http_token" and item["has_credential"] is True
    # 任何位置都不回传令牌或用户名（用户名也不进列表 schema）。
    assert "gitee-pat" not in repr(item)
    assert "http_token" not in item

    listed = test_client.get(f"/api/knowledge/repos?project_id={project.id}").json()
    row = listed["data"]["items"][0]
    assert "gitee-pat" not in repr(row)
    assert row["has_credential"] is True

    # 缺令牌的 http_token 创建是业务拒绝，且不排队同步。
    queued_before = len(queued)
    bad = test_client.post(
        "/api/knowledge/repos",
        json={
            "project_id": project.id,
            "remote_url": "https://gitee.test/org/demo2.git",
            "auth_type": "http_token",
        },
    ).json()
    assert bad["code"] == -1 and bad["data"] is None
    assert len(queued) == queued_before


def test_api_unknown_project_is_404(client, db):
    test_client, _ = client
    assert test_client.get("/api/knowledge/repos?project_id=9999").status_code == 404


def test_api_repo_lifecycle_queues_sync_and_retrust_failure(client, db, monkeypatch):
    test_client, queued = client
    project = _ready_project(db)
    priv, _ = repo_sync.generate_keypair_sync()
    created = test_client.post(
        "/api/knowledge/repos",
        json={
            "project_id": project.id,
            "remote_url": "git@local.test:org/demo.git",
            "private_key": priv,
        },
    ).json()["data"]
    repo_id = created["id"]
    assert queued == [("sync", OWNER, repo_id)]

    assert test_client.get(f"/api/knowledge/repos/{repo_id}").json()["code"] == 0
    updated = test_client.put(
        f"/api/knowledge/repos/{repo_id}", json={"name": "改名"}
    ).json()
    assert updated["data"]["name"] == "改名"

    # 再点同步：排队；已经在跑时静默返回 already_running。
    again = test_client.post(f"/api/knowledge/repos/{repo_id}/sync").json()
    assert again["data"]["started"] is True and again["data"]["already_running"] is False

    repo_sync._running.add(repo_id)
    try:
        busy = test_client.post(f"/api/knowledge/repos/{repo_id}/sync").json()
    finally:
        repo_sync._running.discard(repo_id)
    assert busy["data"]["started"] is False
    assert busy["data"]["already_running"] is True

    # retrust 探测失败只能 code=-1，不产生 500。
    async def boom(*a, **k):
        raise RepoSyncError("host key mismatch")

    monkeypatch.setattr(knowledge_router, "probe_remote", boom)
    bad = test_client.post(f"/api/knowledge/repos/{repo_id}/retrust")
    assert bad.json()["code"] == -1

    deleted = test_client.delete(f"/api/knowledge/repos/{repo_id}")
    assert deleted.json()["message"] == "deleted"
    assert test_client.get(f"/api/knowledge/repos/{repo_id}").status_code == 404


def test_api_upload_and_single_delete_rejected_for_repo_type(client, db, vs):
    test_client, _ = client
    project = _ready_project(db)
    priv, _ = repo_sync.generate_keypair_sync()
    repo = test_client.post(
        "/api/knowledge/repos",
        json={
            "project_id": project.id,
            "remote_url": "git@local.test:org/demo.git",
            "private_key": priv,
        },
    ).json()["data"]

    # 手动上传进仓库类型：拒绝。
    res = test_client.post(
        f"/api/knowledge/types/{repo['type_id']}/documents",
        files={"file": ("note.txt", "正文内容。".encode(), "text/plain")},
    ).json()
    assert res["code"] == -1
    assert "仓库" in res["message"]

    # 直接造一篇仓库文档：单删拒绝、批删跳过。
    svc = KnowledgeService(db, OWNER)
    doc = svc.create_repo_document(
        type_id=repo["type_id"],
        repo_id=repo["id"],
        rel_path="a.py",
        content=_PY_A,
        size=len(_PY_A),
        blob_hash="z" * 40,
        language="python",
    )
    upload_doc = svc.create_document(
        filename="u.txt",
        mime="text/plain",
        size=3,
        content="普通文档",
        type_id=repo["type_id"],
    )
    single = test_client.delete(f"/api/knowledge/documents/{doc.id}").json()
    assert single["code"] == -1
    assert "仓库" in single["message"]
    assert db.get(KbDocument, doc.id) is not None

    batch = test_client.post(
        "/api/knowledge/documents/batch-delete",
        json={"ids": [doc.id, upload_doc.id, 999999]},
    ).json()
    # 仓库文件跳过，别人的/不存在的跳过：只删掉那篇上传文档。
    assert batch["data"] == {"requested": 3, "done": 1}
    assert db.get(KbDocument, doc.id) is not None
    assert db.get(KbDocument, upload_doc.id) is None


def test_api_repo_is_owner_scoped_404(client, db):
    test_client, queued = client
    project = _ready_project(db)
    # 另一个用户也有就绪的向量模型，否则同步接口会先被 embedding 门挡住，
    # 走不到「查别人的仓库 → 404」这条分支。
    db.add(
        ModelConfig(
            owner_id=OTHER,
            purpose=ModelPurpose.EMBEDDING.value,
            title="向量",
            model_name="bge-m3",
            base_url="https://embed.test/v1",
            is_default=True,
            vector_size=DIM,
            last_test_ok=True,
        )
    )
    db.commit()
    priv, _ = repo_sync.generate_keypair_sync()
    repo = test_client.post(
        "/api/knowledge/repos",
        json={
            "project_id": project.id,
            "remote_url": "git@local.test:org/demo.git",
            "private_key": priv,
        },
    ).json()["data"]

    app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=OTHER, username="other"
    )
    assert test_client.get(f"/api/knowledge/repos/{repo['id']}").status_code == 404
    sync_resp = test_client.post(f"/api/knowledge/repos/{repo['id']}/sync")
    assert sync_resp.status_code == 404
    # 越权请求绝不能排上同步任务。
    assert all(item[1] != OTHER for item in queued)

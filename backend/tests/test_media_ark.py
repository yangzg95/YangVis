"""方舟客户端与产物落盘：请求形状、错误翻译、下载上限、路径守卫。

全程 ``httpx.MockTransport``，不出网也不花钱——这一层值得钉死的是**发出去的 JSON
形状**（方舟按它计费）和**错误怎么分类**（``retriable`` 决定作业是重排还是判死，判错
一次的代价是真金白银的抵扣）。作业状态机本身在 ``test_media_jobs.py``。

落盘那一段是注入面：扩展名只认文件头魔数，路径必须是自己写出来的那一种形状，
``/uploads`` 不带鉴权，所以这里的守卫不是「防出错」而是「防越权读盘」。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.crypto import encrypt
from app.models.entities import ModelConfig
from app.models.schemas import ModelPurpose
from app.services.media import ark, store

# MockTransport 看到的是**完整**路径：base_url 里那段 /api/v3 也在内，
# 而 ark.py 里的常量只是尾缀——前缀归用户的模型配置管。
IMAGES_PATH = "/api/v3/images/generations"
TASKS_PATH = "/api/v3/contents/generations/tasks"

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32
WEBM = b"\x1a\x45\xdf\xa3\x00\x00\x00\x00\x00\x00\x00\x1f" + b"0" * 32
NOT_MEDIA = b"<html><script>alert(1)</script></html>"


@pytest.fixture()
def config() -> ModelConfig:
    return ModelConfig(
        owner_id=1,
        purpose=ModelPurpose.IMAGE.value,
        title="测试图片模型",
        model_name="doubao-seedream-5-0-pro-260128",
        # 带尾斜杠：拼地址时要去掉，否则出来是 //images/generations。
        base_url="https://ark.cn-beijing.volces.com/api/v3/",
        api_key_enc=encrypt("sk-unit-test"),
    )


class Upstream:
    """把方舟那一侧伪造出来，并按 ``(method, path)`` 收下所有请求。

    同一个路由预备多份响应时按顺序取用，只剩最后一份就固定复用；没预备就立刻
    AssertionError——漏配必须显形，不能悄悄走到 200 空体去撞后面的断言。
    """

    def __init__(self, monkeypatch) -> None:
        self._responses: dict[tuple[str, str], list] = {}
        self._error: Exception | None = None
        self.requests: list[httpx.Request] = []
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self._error is not None:
                raise self._error
            key = (request.method, request.url.path)
            queue = self._responses.get(key)
            if not queue:
                raise AssertionError(f"用例没有为 {key} 预备响应")
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    def route(self, method: str, path: str, *payloads) -> "Upstream":
        self._responses.setdefault((method, path), []).extend(payloads)
        return self

    def fail_with(self, exc: Exception) -> "Upstream":
        self._error = exc
        return self

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content)

    @property
    def headers(self) -> httpx.Headers:
        return self.requests[0].headers


@pytest.fixture()
def ark_server(monkeypatch) -> Upstream:
    return Upstream(monkeypatch)


# ---- 请求拼装 ---------------------------------------------------------------


def test_image_data_url_is_lowercase_and_rejects_empty():
    url = ark.image_data_url(PNG, "image/png")
    assert url.startswith("data:image/png;base64,")
    with pytest.raises(ark.ArkError):
        ark.image_data_url(b"", "image/png")


def test_build_video_content_puts_the_prompt_first():
    content = ark.build_video_content(
        "镜头推近",
        [("first_frame", "data:image/png;base64,AAA"), ("reference_image", "https://x/1.png")],
    )
    assert content[0] == {"type": "text", "text": "镜头推近"}
    assert [item["role"] for item in content[1:]] == ["first_frame", "reference_image"]
    assert content[1]["image_url"] == {"url": "data:image/png;base64,AAA"}


def test_url_joins_without_a_double_slash(config):
    # 路径常量不带前缀（前缀归 base_url 管），拼出来正好是文档里那两条地址。
    assert ark._url(config, ark.IMAGES_PATH) == f"https://ark.cn-beijing.volces.com{IMAGES_PATH}"
    assert ark._url(config, ark.TASKS_PATH) == f"https://ark.cn-beijing.volces.com{TASKS_PATH}"


def test_missing_api_key_fails_before_the_request(config):
    config.api_key_enc = None
    with pytest.raises(ark.ArkError, match="api_key"):
        ark._headers(config)


# ---- 图片（同步）-----------------------------------------------------------


def test_generate_image_payload_and_parse(config, ark_server):
    ark_server.route(
        "POST",
        IMAGES_PATH,
        {"data": [{"url": "https://ark/1.png", "size": "2048x2048"}], "usage": {"generated_images": 1}},
    )

    result = asyncio.run(
        ark.generate_image(config, prompt="竖屏首帧", size="2K", images=["data:image/png;base64,AAA"])
    )

    body = ark_server.body()
    assert body["model"] == "doubao-seedream-5-0-pro-260128"
    assert body["prompt"] == "竖屏首帧" and body["size"] == "2K"
    assert body["watermark"] is False, "水印默认关：开了就等于给用户一条带别人 logo 的片子"
    # 一张参考图是字符串，多张才是数组——方舟两种形状不一样。
    assert body["image"] == "data:image/png;base64,AAA"
    assert ark_server.headers["Authorization"] == "Bearer sk-unit-test"
    assert (result.url, result.size) == ("https://ark/1.png", "2048x2048")
    assert result.usage == {"generated_images": 1}


def test_generate_image_with_several_references_sends_a_list(config, ark_server):
    ark_server.route("POST", IMAGES_PATH, {"data": [{"url": "https://ark/1.png"}]})

    asyncio.run(ark.generate_image(config, prompt="p", images=["a", "b"]))

    assert ark_server.body()["image"] == ["a", "b"]


@pytest.mark.parametrize("body", [{}, {"data": []}, {"data": [{"burl": "x"}]}, {"data": "不是数组"}])
def test_generate_image_without_a_url_is_a_bad_response(config, ark_server, body):
    ark_server.route("POST", IMAGES_PATH, body)
    with pytest.raises(ark.ArkError, match="data|url"):
        asyncio.run(ark.generate_image(config, prompt="p"))


def test_a_non_json_success_body_is_reported_as_such(config, ark_server):
    ark_server.route("POST", IMAGES_PATH, httpx.Response(200, text="<html>proxy said no</html>"))
    with pytest.raises(ark.ArkError, match="不是 JSON"):
        asyncio.run(ark.generate_image(config, prompt="p"))


def test_a_json_array_is_not_a_response_we_trust(config, ark_server):
    ark_server.route("POST", IMAGES_PATH, httpx.Response(200, content=b"[1,2]"))
    with pytest.raises(ark.ArkError, match="顶层不是对象"):
        asyncio.run(ark.generate_image(config, prompt="p"))


# ---- 视频（异步任务）-------------------------------------------------------


def test_submit_video_merges_the_params(config, ark_server):
    ark_server.route("POST", TASKS_PATH, {"id": "task-1"})

    submission = asyncio.run(
        ark.submit_video(
            config,
            content=ark.build_video_content(
                "动起来", [("first_frame", "data:image/png;base64,AAA")]
            ),
            params={"ratio": "9:16", "resolution": "720p", "duration": 5, "generate_audio": False},
        )
    )

    body = ark_server.body()
    # 参数由业务侧显式给，客户端不兜默认：1.0 系与 2.x 的默认画幅不是一个值。
    assert (body["ratio"], body["resolution"], body["duration"], body["generate_audio"]) == (
        "9:16",
        "720p",
        5,
        False,
    )
    assert body["content"][1]["role"] == "first_frame"
    assert submission.task_id == "task-1"
    assert submission.request["model"] == config.model_name


def test_submit_video_without_an_id(config, ark_server):
    ark_server.route("POST", TASKS_PATH, {"code": "Internal"})
    with pytest.raises(ark.ArkError, match="任务 id"):
        asyncio.run(ark.submit_video(config, content=[{"type": "text", "text": "x"}]))


def test_get_video_task_reads_the_success_shape(config, ark_server):
    ark_server.route(
        "GET",
        f"{TASKS_PATH}/task-9",
        {
            "id": "task-9",
            "status": "succeeded",
            "content": {"video_url": "https://ark/1.mp4", "last_frame_url": "https://ark/last.png"},
            "usage": {"total_tokens": 12},
        },
    )

    state = asyncio.run(ark.get_video_task(config, "task-9"))

    # status 直接取上游字面值，作业层原样写回，不再翻译一遍。
    assert (state.status, state.video_url, state.last_frame_url) == (
        "succeeded",
        "https://ark/1.mp4",
        "https://ark/last.png",
    )
    assert state.usage == {"total_tokens": 12}
    assert state.task_id == "task-9"


def test_get_video_task_carries_the_upstream_failure(config, ark_server):
    ark_server.route(
        "GET",
        f"{TASKS_PATH}/task-9",
        {"id": "task-9", "status": "failed", "error": {"code": "ContentRisk", "message": "涉政"}},
    )

    state = asyncio.run(ark.get_video_task(config, "task-9"))

    assert (state.status, state.error_code, state.error_message) == ("failed", "ContentRisk", "涉政")


def test_cancel_uses_delete_on_the_same_path(config, ark_server):
    ark_server.route("DELETE", f"{TASKS_PATH}/task-9", {"id": "task-9"})

    asyncio.run(ark.cancel_video_task(config, "task-9"))

    assert ark_server.requests[0].method == "DELETE"


# ---- 错误翻译 ---------------------------------------------------------------


def _response(status_code: int, body=None, text: str = "") -> httpx.Response:
    response = (
        httpx.Response(status_code, json=body)
        if body is not None
        else httpx.Response(status_code, text=text)
    )
    response.request = httpx.Request("POST", "https://ark.cn-beijing.volces.com/api/v3/x")
    return response


@pytest.mark.parametrize(
    "status_code,body,code,retriable,fragment",
    [
        (429, {"error": {"code": "QuotaExceeded", "message": "qps limit"}}, "QuotaExceeded", True, "限流"),
        (
            500,
            {"error": {"code": "InternalServiceError", "message": "boom"}},
            "InternalServiceError",
            True,
            "稍后自动重试",
        ),
        (
            400,
            {"error": {"code": "SensitiveContentDetected", "message": "risk_control"}},
            "SensitiveContentDetected",
            False,
            "内容安全审核",
        ),
        (401, {"error": {"code": "AuthenticationError", "message": "invalid key"}}, "AuthenticationError", False, "认证失败"),
        (
            404,
            {"error": {"code": "InvalidEndpointOrModel", "message": "model not exist"}},
            "InvalidEndpointOrModel",
            False,
            "Model ID",
        ),
        (400, {"code": "InvalidRequest", "message": "duration 超出范围"}, "InvalidRequest", False, "调用失败"),
    ],
)
def test_error_translation_decides_whether_to_retry(status_code, body, code, retriable, fragment):
    error = ark._describe_error(_response(status_code, body=body))
    assert (error.code, error.retriable) == (code, retriable), str(error)
    assert fragment in str(error)
    assert error.status_code == status_code


def test_error_translation_keeps_the_raw_text_when_the_body_is_not_json():
    error = ark._describe_error(_response(502, text="<html>502 Bad Gateway</html>"))
    assert error.retriable is True and "502 Bad Gateway" in str(error)


def test_a_non_2xx_becomes_an_error(config, ark_server):
    ark_server.route("POST", IMAGES_PATH, httpx.Response(429, json={"error": {"message": "qps"}}))
    with pytest.raises(ark.ArkError, match="限流"):
        asyncio.run(ark.generate_image(config, prompt="p"))


def test_network_failure_is_retriable(config, ark_server):
    ark_server.fail_with(httpx.ConnectError("dns down"))
    with pytest.raises(ark.ArkError) as caught:
        asyncio.run(ark.generate_image(config, prompt="p"))
    assert (caught.value.code, caught.value.retriable) == ("NetworkError", True)


# ---- 连通性探测 -------------------------------------------------------------


def test_probe_succeeds_but_says_it_did_not_check_the_model(config, ark_server):
    ark_server.route("GET", TASKS_PATH, {"total": 7, "data": []})

    ok, message = asyncio.run(ark.probe(config))

    assert ok is True
    assert "Model ID 要在第一次真实生成时才会校验" in message and "7" in message
    assert dict(ark_server.requests[0].url.params) == {"page_num": "1", "page_size": "1"}


def test_probe_never_raises(config, ark_server):
    """探测是设置页那一下点击，炸了只能留下一个 500：失败要翻成一句话。"""
    ark_server.route(
        "GET", TASKS_PATH, httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    ok, message = asyncio.run(ark.probe(config))
    assert ok is False and "认证失败" in message

    ark_server.fail_with(httpx.ConnectError("unreachable"))
    ok, message = asyncio.run(ark.probe(config))
    assert ok is False and "无法连接" in message


def test_probe_404_blames_the_url_not_the_model_id(config, ark_server):
    """探测的请求体里没有模型，这种 404 讲「核对 Model ID」是往错方向支人。

    真事：base_url 里多粘了 ``/images/generations``，任务列表被拼成
    ``…/images/generations/contents/generations/tasks``，上游回 PathNotFound。
    """
    ark_server.route(
        "GET",
        TASKS_PATH,
        httpx.Response(404, json={"error": {"code": "PathNotFound", "message": "path not found"}}),
    )

    ok, message = asyncio.run(ark.probe(config))

    assert ok is False
    assert "base_url" in message and "PathNotFound" in message
    assert "Model ID" not in message


def test_a_model_carrying_404_still_points_at_the_model_id(config, ark_server):
    """同一句 404 在真正带模型的调用上，该说的还是 Model ID——两种说法不能互相顶掉。"""
    ark_server.route(
        "POST",
        IMAGES_PATH,
        httpx.Response(404, json={"error": {"code": "InvalidEndpointOrModel"}}),
    )

    with pytest.raises(ark.ArkError, match="Model ID"):
        asyncio.run(ark.generate_image(config, prompt="p"))


# ---- 下载 ------------------------------------------------------------------


def test_download_returns_the_bytes(ark_server):
    ark_server.route("GET", "/artifact/1.mp4", httpx.Response(200, content=MP4))

    assert asyncio.run(ark.download("https://cdn/artifact/1.mp4", max_bytes=1024)) == MP4


def test_download_stops_at_the_size_cap(ark_server):
    ark_server.route("GET", "/artifact/big.mp4", httpx.Response(200, content=b"0" * 4096))
    with pytest.raises(ark.ArkError) as caught:
        asyncio.run(ark.download("https://cdn/artifact/big.mp4", max_bytes=100))
    # 超限说明这个 URL 指向的不是我们预期的产物，判死而不是重试。
    assert (caught.value.code, caught.value.retriable) == ("DownloadTooLarge", False)


@pytest.mark.parametrize(
    "exc,code,retriable",
    [
        (httpx.ReadTimeout("slow"), "DownloadTimeout", True),
        (httpx.ConnectError("reset"), "DownloadNetwork", True),
    ],
)
def test_download_failures_that_deserve_a_retry(ark_server, exc, code, retriable):
    ark_server.fail_with(exc)
    with pytest.raises(ark.ArkError) as caught:
        asyncio.run(ark.download("https://cdn/artifact/1.mp4", max_bytes=1024))
    assert (caught.value.code, caught.value.retriable) == (code, retriable)


def test_download_rejects_a_dead_link_and_an_empty_body(ark_server):
    ark_server.route("GET", "/artifact/gone.mp4", httpx.Response(403, text="expired"))
    with pytest.raises(ark.ArkError) as caught:
        asyncio.run(ark.download("https://cdn/artifact/gone.mp4", max_bytes=1024))
    assert caught.value.code == "DownloadFailed"

    ark_server.route("GET", "/artifact/empty.mp4", httpx.Response(200, content=b""))
    with pytest.raises(ark.ArkError, match="空的"):
        asyncio.run(ark.download("https://cdn/artifact/empty.mp4", max_bytes=1024))


# ---- 产物落盘 --------------------------------------------------------------


@pytest.fixture()
def media(tmp_path, monkeypatch):
    """产物目录指到临时盘，上限压到几 KB：不碰真实 uploads/，也不必造几 MB 字节。"""
    monkeypatch.setattr(
        store,
        "settings",
        SimpleNamespace(upload_media_path=tmp_path, UPLOAD_MEDIA_URL="/uploads"),
    )
    monkeypatch.setattr(store, "_IMAGE_MAX_BYTES", 1024)
    monkeypatch.setattr(store, "_VIDEO_MAX_BYTES", 2048)
    return tmp_path


@pytest.mark.parametrize(
    "raw,expected",
    [
        (PNG, ("image/png", ".png")),
        (JPEG, ("image/jpeg", ".jpg")),
        (b"GIF89a" + b"0" * 32, ("image/gif", ".gif")),
        (b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"0" * 16, ("image/webp", ".webp")),
        (b"BM" + b"0" * 32, ("image/bmp", ".bmp")),
        (MP4, ("video/mp4", ".mp4")),
        (WEBM, ("video/webm", ".webm")),
    ],
)
def test_sniff_trusts_only_the_file_header(raw, expected):
    assert store.sniff_media(raw) == expected


@pytest.mark.parametrize("raw", [NOT_MEDIA, b"", b"RIFF\x10\x00\x00\x00WAVEfmt " + b"0" * 16])
def test_sniff_rejects_anything_else(raw):
    with pytest.raises(store.MediaFileError):
        store.sniff_media(raw)


def test_store_blob_writes_under_the_project_with_a_random_name(media):
    first = store.store_blob(7, PNG)
    assert first.rel_path.startswith("videos/7/")
    assert len(first.rel_path.rsplit("/", 1)[1]) == len("9b873576ec46468f.png")
    assert first.rel_path.endswith(".png") and first.mime == "image/png"
    assert first.size == len(PNG)
    assert first.url == f"/uploads/{first.rel_path}"
    assert (media / first.rel_path).read_bytes() == PNG
    assert store.store_blob(7, PNG).rel_path != first.rel_path, "随机名：两次产物不互相覆盖"
    assert store.store_blob(7, MP4).rel_path.endswith(".mp4")


def test_store_blob_enforces_the_size_cap(media):
    with pytest.raises(store.MediaFileError, match="上限"):
        store.store_blob(7, PNG + b"0" * 2048)


@pytest.mark.parametrize(
    "rel_path,ok",
    [
        ("videos/7/9b873576ec46468f.png", True),
        ("videos/99999999999999999999/9b873576ec46468f.mp4", True),
        ("", False),
        ("slides/7/9b873576ec46468f.png", False),
        ("videos/7/../../etc/passwd", False),
        ("videos//9b873576ec46468f.png", False),
        ("videos/7/9b873576ec46468f.png/extra", False),
        ("videos/7/../7/9b873576ec46468f.png", False),
        ("videos/notanumber/9b873576ec46468f.png", False),
        ("videos/7/shell.php", False),
        ("videos/7/9b873576ec46468f.svg", False),
        ("videos/7/zzzzzzzzzzzzzzzz.png", False),
    ],
)
def test_rel_path_must_be_the_shape_this_module_writes(rel_path, ok):
    assert store.is_safe_rel_path(rel_path) is ok


def test_resolve_url_reads_back_and_rejects_junk(media):
    stored = store.store_blob(3, PNG)

    assert store.resolve_url(stored.rel_path) == f"/uploads/{stored.rel_path}"
    assert store.resolve_url("videos/3/nope.txt") == ""
    assert store.read_blob(stored.rel_path) == PNG
    with pytest.raises(store.MediaFileError):
        store.read_blob("videos/../secret")
    with pytest.raises(store.MediaFileError):
        store.read_blob("videos/3/9b873576ec46468f.png")


def test_delete_removes_the_file_and_keeps_unknown_neighbours(media):
    stored = store.store_blob(9, PNG)
    stray = media / "videos" / "9" / "README.txt"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("不是产物")

    store.delete_blob("videos/9/nope.txt")  # 非法形状直接跳过，不碰磁盘
    store.delete_blob(stored.rel_path)
    assert not (media / stored.rel_path).exists()

    keep = store.store_blob(9, MP4)
    store.delete_project_dir(9)

    assert not (media / keep.rel_path).exists()
    assert stray.exists(), "不认识的文件留着，不硬删别人的东西"
    assert (media / "videos" / "9").is_dir(), "目录没清干净就留着"

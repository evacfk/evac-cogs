"""RedGifs resolver against injected fake network functions. Proves the request
flow and failure handling; it does NOT prove RedGifs' live API still matches."""
import pytest

from redditfeed import redgifs
from redditfeed.redgifs import RedgifsError, RedgifsResolver, RedgifsTooLarge

PAGE = "https://www.redgifs.com/watch/FantasticRoundPuma"


def test_gif_id_parsing():
    assert redgifs.gif_id_from_url(PAGE) == "fantasticroundpuma"
    assert redgifs.gif_id_from_url("https://redgifs.com/ifr/AbcDef?x=1") == "abcdef"
    assert redgifs.gif_id_from_url("https://www.redgifs.com/users/someone") is None
    assert redgifs.gif_id_from_url("not a url") is None


def test_pick_video_prefers_hd_then_sd_never_images():
    assert redgifs.pick_video_url({"gif": {"urls": {"hd": "https://c/h.mp4", "sd": "https://c/s.mp4"}}}) == "https://c/h.mp4"
    assert redgifs.pick_video_url({"gif": {"urls": {"sd": "https://c/s.mp4", "poster": "https://c/p.jpg"}}}) == "https://c/s.mp4"
    assert redgifs.pick_video_url({"gif": {"urls": {"poster": "https://c/p.jpg"}}}) is None
    assert redgifs.pick_video_url({}) is None and redgifs.pick_video_url(None) is None


def test_upload_limit_leaves_margin_and_has_a_ceiling():
    assert redgifs.upload_limit(10 * 1024 * 1024) == 10 * 1024 * 1024 - redgifs.UPLOAD_MARGIN_BYTES
    assert redgifs.upload_limit(500 * 1024 * 1024) == redgifs.HARD_MAX_BYTES - redgifs.UPLOAD_MARGIN_BYTES
    assert redgifs.upload_limit(None) > 0


class Net:
    def __init__(self, gif=None, token="tok", fail_gif=0, data=b"video"):
        self.json_calls, self.byte_calls = [], []
        self.gif, self.token, self.fail_gif, self.data = gif or {"gif": {"urls": {"hd": "https://c/h.mp4"}}}, token, fail_gif, data

    async def get_json(self, url, headers):
        self.json_calls.append((url, headers))
        if url.endswith("/auth/temporary"):
            return {"token": self.token}
        if self.fail_gif:
            self.fail_gif -= 1
            raise RedgifsError("HTTP 401")
        return self.gif

    async def get_bytes(self, url, headers, max_bytes):
        self.byte_calls.append((url, headers, max_bytes))
        return self.data


def resolver(net, clock=lambda: 1000.0):
    return RedgifsResolver(get_json=net.get_json, get_bytes=net.get_bytes, clock=clock)


async def test_fetch_gets_a_token_looks_up_the_clip_and_downloads_with_a_referer():
    net = Net()
    name, data = await resolver(net).fetch(PAGE, 5_000_000)
    assert (name, data) == ("fantasticroundpuma.mp4", b"video")
    assert net.json_calls[1][0].endswith("/gifs/fantasticroundpuma")
    assert net.json_calls[1][1]["Authorization"] == "Bearer tok"
    url, headers, limit = net.byte_calls[0]
    assert url == "https://c/h.mp4" and headers["Referer"] == redgifs.REFERER and limit == 5_000_000


async def test_token_is_reused_then_refreshed_when_old():
    net, now = Net(), [1000.0]
    r = resolver(net, clock=lambda: now[0])
    await r.fetch(PAGE, 10)
    await r.fetch(PAGE, 10)
    assert sum(1 for u, _ in net.json_calls if u.endswith("/auth/temporary")) == 1
    now[0] += redgifs.TOKEN_TTL_SECONDS + 1
    await r.fetch(PAGE, 10)
    assert sum(1 for u, _ in net.json_calls if u.endswith("/auth/temporary")) == 2


async def test_a_rejected_token_is_retried_once_with_a_fresh_one():
    net = Net(fail_gif=1)
    await resolver(net).fetch(PAGE, 10)
    assert sum(1 for u, _ in net.json_calls if u.endswith("/auth/temporary")) == 2


async def test_persistent_failures_raise_redgifserror():
    with pytest.raises(RedgifsError):
        await resolver(Net(fail_gif=5)).fetch(PAGE, 10)
    with pytest.raises(RedgifsError):
        await resolver(Net(gif={"gif": {"urls": {}}})).fetch(PAGE, 10)
    with pytest.raises(RedgifsError):
        await resolver(Net(token="")).fetch(PAGE, 10)
    with pytest.raises(RedgifsError):
        await resolver(Net()).fetch("https://www.redgifs.com/users/x", 10)
    with pytest.raises(RedgifsError):
        await resolver(Net(data=b"")).fetch(PAGE, 10)


async def test_oversized_and_no_room_are_too_large():
    with pytest.raises(RedgifsTooLarge):
        await resolver(Net(data=b"x" * 11)).fetch(PAGE, 10)
    with pytest.raises(RedgifsTooLarge):
        await resolver(Net()).fetch(PAGE, 0)


async def test_unexpected_exceptions_are_wrapped():
    class Boom(Net):
        async def get_json(self, url, headers):
            raise TimeoutError("slow")

    with pytest.raises(RedgifsError):
        await resolver(Boom()).fetch(PAGE, 10)

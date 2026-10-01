"""
Tests for WeChat Channel Adapter

No real WeChat Official Account credentials are available yet (needs
Tencent business registration), so every WeChat API call here is
mocked. Covers: token lifecycle, webhook signature verification,
message-type conversion (text/image/voice/video/location), send paths
(text/media/rate-limit/inactive-user/errors), media upload, user info,
template messages, QR codes, and the AES-256-CBC message crypto.
"""

import base64
import hashlib
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.base import (
    ChannelStatus,
    MediaAttachment,
    MessageType,
    ChannelRateLimitError,
)
from integrations.channels.extensions.wechat_adapter import (
    WeChatAdapter,
    WeChatConfig,
    WeChatMessageCrypto,
    TemplateMessage,
    create_wechat_adapter,
)


def _wechat_xml(
    msg_type="text",
    content=None,
    openid="oUser123",
    to_user="gh_official",
    msg_id="1000000001",
    extra="",
    create_time=None,
):
    create_time = create_time or int(time.time())
    body = f"""<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{openid}]]></FromUserName>
<CreateTime>{create_time}</CreateTime>
<MsgType><![CDATA[{msg_type}]]></MsgType>
<MsgId>{msg_id}</MsgId>
{extra}
</xml>"""
    return body


class _Resp:
    """Minimal async context-manager response double."""

    def __init__(self, json_data=None, content_type="application/json", read_data=b""):
        self._json_data = json_data or {}
        self.content_type = content_type
        self._read_data = read_data

    async def json(self):
        return self._json_data

    async def read(self):
        return self._read_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def wechat_config():
    return WeChatConfig(
        app_id="wx1234567890",
        app_secret="secret123",
        token="verify-token",
    )


@pytest.fixture
def adapter(wechat_config, mock_session):
    a = WeChatAdapter(wechat_config)
    a._session = mock_session
    a._access_token = "cached-token"
    a._token_expires_at = int(time.time()) + 7200
    return a


class TestConnectDisconnect:
    @pytest.mark.asyncio
    async def test_connect_missing_credentials_fails_fast(self):
        cfg = WeChatConfig(app_id="", app_secret="")
        a = WeChatAdapter(cfg)
        assert await a.connect() is False
        assert a._session is None

    @pytest.mark.asyncio
    async def test_connect_success(self, wechat_config):
        a = WeChatAdapter(wechat_config)
        fake_session = MagicMock()
        fake_session.get = MagicMock(
            return_value=_Resp({"access_token": "tok", "expires_in": 7200})
        )
        with patch("aiohttp.ClientSession", return_value=fake_session):
            ok = await a.connect()
        assert ok is True
        assert a.status == ChannelStatus.CONNECTED
        assert a._access_token == "tok"

    @pytest.mark.asyncio
    async def test_connect_token_failure_reports_error_status(self, wechat_config):
        a = WeChatAdapter(wechat_config)
        fake_session = MagicMock()
        fake_session.get = MagicMock(return_value=_Resp({"errcode": 40001, "errmsg": "bad secret"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            ok = await a.connect()
        assert ok is False

    @pytest.mark.asyncio
    async def test_connect_encryption_requires_aes_key(self, wechat_config):
        wechat_config.enable_encryption = True
        wechat_config.encoding_aes_key = None
        a = WeChatAdapter(wechat_config)
        fake_session = MagicMock()
        fake_session.get = MagicMock(
            return_value=_Resp({"access_token": "tok", "expires_in": 7200})
        )
        with patch("aiohttp.ClientSession", return_value=fake_session):
            ok = await a.connect()
        assert ok is False

    @pytest.mark.asyncio
    async def test_disconnect_closes_session_and_clears_state(self, adapter, mock_session):
        await adapter.disconnect()
        mock_session.close.assert_awaited_once()
        assert adapter._session is None
        assert adapter._access_token is None
        assert adapter.status == ChannelStatus.DISCONNECTED


class TestTokenLifecycle:
    @pytest.mark.asyncio
    async def test_ensure_token_skips_refresh_when_valid(self, adapter, mock_session):
        mock_session.get = MagicMock(side_effect=AssertionError("should not refresh"))
        assert await adapter._ensure_token() is True

    @pytest.mark.asyncio
    async def test_ensure_token_refreshes_when_expired(self, adapter, mock_session):
        adapter._token_expires_at = int(time.time()) - 10
        mock_session.get = MagicMock(
            return_value=_Resp({"access_token": "fresh-tok", "expires_in": 7200})
        )
        assert await adapter._ensure_token() is True
        assert adapter._access_token == "fresh-tok"

    @pytest.mark.asyncio
    async def test_refresh_access_token_sets_expiry_with_buffer(self, adapter, mock_session):
        mock_session.get = MagicMock(
            return_value=_Resp({"access_token": "tok2", "expires_in": 7200})
        )
        before = int(time.time())
        ok = await adapter._refresh_access_token()
        assert ok is True
        # 7200 - 300s buffer
        assert adapter._token_expires_at >= before + 6899

    @pytest.mark.asyncio
    async def test_refresh_access_token_no_session_returns_false(self, wechat_config):
        a = WeChatAdapter(wechat_config)
        assert await a._refresh_access_token() is False


class TestWebhookVerification:
    def test_verify_webhook_valid_signature(self, adapter):
        token = adapter.wechat_config.token
        timestamp, nonce = "1700000000", "abc123"
        sign_str = "".join(sorted([token, timestamp, nonce]))
        signature = hashlib.sha1(sign_str.encode()).hexdigest()
        assert adapter.verify_webhook(signature, timestamp, nonce) == nonce

    def test_verify_webhook_invalid_signature_rejected(self, adapter):
        result = adapter.verify_webhook("bogus", "1700000000", "abc123")
        assert result == ""


class TestWebhookHandling:
    @pytest.mark.asyncio
    async def test_handle_webhook_text_dispatches_message(self, adapter):
        received = []
        adapter.on_message(lambda m: received.append(m))
        body = _wechat_xml("text", extra="<Content><![CDATA[hello there]]></Content>")
        result = await adapter.handle_webhook(body, "sig", "1700000000", "nonce")
        assert result == "success"
        assert len(received) == 1
        assert received[0].text == "hello there"
        assert received[0].sender_id == "oUser123"
        assert received[0].chat_id == "oUser123"
        assert received[0].is_group is False

    @pytest.mark.asyncio
    async def test_handle_webhook_bad_xml_returns_none(self, adapter):
        result = await adapter.handle_webhook("<not-xml", "sig", "ts", "nonce")
        assert result is None

    @pytest.mark.asyncio
    async def test_handle_webhook_encrypted_rejects_bad_signature(self, adapter):
        adapter.wechat_config.enable_encryption = True
        adapter._crypto = MagicMock()
        adapter._crypto.verify_signature = MagicMock(return_value=False)
        body = "<xml><Encrypt><![CDATA[ciphertext]]></Encrypt></xml>"
        result = await adapter.handle_webhook(
            body, "sig", "ts", "nonce", msg_signature="msig"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_handle_webhook_encrypted_decrypts_then_dispatches(self, adapter):
        adapter.wechat_config.enable_encryption = True
        adapter._crypto = MagicMock()
        adapter._crypto.verify_signature = MagicMock(return_value=True)
        inner = _wechat_xml("text", extra="<Content><![CDATA[secret msg]]></Content>")
        adapter._crypto.decrypt = MagicMock(return_value=inner)
        received = []
        adapter.on_message(lambda m: received.append(m))
        outer = "<xml><Encrypt><![CDATA[ciphertext]]></Encrypt></xml>"
        result = await adapter.handle_webhook(
            outer, "sig", "ts", "nonce", msg_signature="msig"
        )
        assert result == "success"
        assert received[0].text == "secret msg"

    @pytest.mark.asyncio
    async def test_handle_webhook_event_subscribe_calls_registered_handler(self, adapter):
        handled = []
        adapter.register_event_handler("subscribe", lambda root: handled.append(root))
        body = _wechat_xml(
            "event",
            extra="<Event><![CDATA[subscribe]]></Event>",
        )
        result = await adapter.handle_webhook(body, "sig", "ts", "nonce")
        assert result == "success"
        assert len(handled) == 1


class TestMessageConversion:
    def test_convert_text_message(self, adapter):
        root_xml = _wechat_xml("text", extra="<Content><![CDATA[hi]]></Content>")
        from xml.etree import ElementTree
        root = ElementTree.fromstring(root_xml)
        msg = adapter._convert_message(root)
        assert msg.text == "hi"
        assert msg.channel == "wechat"
        assert not msg.has_media

    def test_convert_image_message(self, adapter):
        root_xml = _wechat_xml(
            "image",
            extra="<PicUrl><![CDATA[http://x/pic.jpg]]></PicUrl><MediaId><![CDATA[media1]]></MediaId>",
        )
        from xml.etree import ElementTree
        root = ElementTree.fromstring(root_xml)
        msg = adapter._convert_message(root, MessageType.IMAGE)
        assert msg.has_media
        assert msg.media[0].url == "http://x/pic.jpg"
        assert msg.media[0].file_id == "media1"

    def test_convert_voice_message_uses_recognition_as_text(self, adapter):
        root_xml = _wechat_xml(
            "voice",
            extra="<MediaId><![CDATA[v1]]></MediaId><Recognition><![CDATA[recognized text]]></Recognition>",
        )
        from xml.etree import ElementTree
        root = ElementTree.fromstring(root_xml)
        msg = adapter._convert_message(root, MessageType.VOICE)
        assert msg.text == "recognized text"
        assert msg.media[0].file_id == "v1"

    def test_convert_video_message(self, adapter):
        root_xml = _wechat_xml(
            "video",
            extra="<MediaId><![CDATA[vid1]]></MediaId><ThumbMediaId><![CDATA[thumb1]]></ThumbMediaId>",
        )
        from xml.etree import ElementTree
        root = ElementTree.fromstring(root_xml)
        msg = adapter._convert_message(root, MessageType.VIDEO)
        assert msg.media[0].file_id == "vid1"

    def test_convert_location_message(self, adapter):
        root_xml = _wechat_xml(
            "location",
            extra=(
                "<Location_X>30.1</Location_X><Location_Y>120.2</Location_Y>"
                "<Scale>15</Scale><Label><![CDATA[Somewhere]]></Label>"
            ),
        )
        from xml.etree import ElementTree
        root = ElementTree.fromstring(root_xml)
        msg = adapter._convert_message(root)
        assert "30.1" in msg.text and "120.2" in msg.text and "Somewhere" in msg.text


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_text_success(self, adapter, mock_session):
        mock_session.post = MagicMock(return_value=_Resp({"errcode": 0}))
        result = await adapter.send_message("openid1", "hello")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_text_no_token_fails(self, wechat_config, mock_session):
        a = WeChatAdapter(wechat_config)
        a._session = mock_session
        mock_session.get = MagicMock(return_value=_Resp({"errcode": 40001, "errmsg": "invalid"}))
        result = await a.send_message("openid1", "hello")
        assert result.success is False
        assert "access token" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_inactive_user_48h_error(self, adapter, mock_session):
        mock_session.post = MagicMock(
            return_value=_Resp({"errcode": 45015, "errmsg": "response out of time limit"})
        )
        result = await adapter.send_message("openid1", "hello")
        assert result.success is False
        assert "48 hours" in result.error

    @pytest.mark.asyncio
    async def test_send_rate_limited_raises(self, adapter, mock_session):
        mock_session.post = MagicMock(
            return_value=_Resp({"errcode": 45047, "errmsg": "freq limit"})
        )
        with pytest.raises(ChannelRateLimitError):
            await adapter.send_message("openid1", "hello")

    @pytest.mark.asyncio
    async def test_send_unknown_error_returns_failure_not_raise(self, adapter, mock_session):
        mock_session.post = MagicMock(
            return_value=_Resp({"errcode": 99999, "errmsg": "weird failure"})
        )
        result = await adapter.send_message("openid1", "hello")
        assert result.success is False
        assert result.error == "weird failure"

    @pytest.mark.asyncio
    async def test_send_with_media_routes_to_media_path(self, adapter, mock_session):
        mock_session.post = MagicMock(return_value=_Resp({"errcode": 0}))
        media = [MediaAttachment(type=MessageType.IMAGE, file_id="existing-media")]
        result = await adapter.send_message("openid1", "caption", media=media)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_edit_message_falls_back_to_send(self, adapter, mock_session):
        mock_session.post = MagicMock(return_value=_Resp({"errcode": 0}))
        result = await adapter.edit_message("openid1", "msgid1", "new text")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_delete_message_unsupported_returns_false(self, adapter):
        assert await adapter.delete_message("openid1", "msgid1") is False

    @pytest.mark.asyncio
    async def test_send_typing_is_noop(self, adapter):
        assert await adapter.send_typing("openid1") is None


class TestMediaUpload:
    @pytest.mark.asyncio
    async def test_send_media_without_id_uploads_first(self, adapter, mock_session):
        mock_session.post = MagicMock(
            side_effect=[
                _Resp({"media_id": "uploaded-id"}),  # upload
                _Resp({"errcode": 0}),  # send
            ]
        )
        media = MediaAttachment(type=MessageType.IMAGE, url="http://x/pic.jpg")
        mock_session.get = MagicMock(return_value=_Resp(read_data=b"binarydata"))
        result = await adapter._send_media_message("openid1", media)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_media_upload_fails_no_media_id(self, adapter, mock_session):
        media = MediaAttachment(type=MessageType.IMAGE, url=None, file_path=None, file_id=None)
        result = await adapter._send_media_message("openid1", media)
        assert result.success is False
        assert "media" in result.error.lower()

    @pytest.mark.asyncio
    async def test_upload_media_from_url(self, adapter, mock_session):
        mock_session.get = MagicMock(return_value=_Resp(read_data=b"filebytes"))
        mock_session.post = MagicMock(return_value=_Resp({"media_id": "new-id"}))
        media = MediaAttachment(type=MessageType.IMAGE, url="http://x/pic.jpg")
        media_id = await adapter._upload_media(media)
        assert media_id == "new-id"


class TestUserInfo:
    @pytest.mark.asyncio
    async def test_get_user_info_success_and_cached(self, adapter, mock_session):
        mock_session.get = MagicMock(
            return_value=_Resp({
                "openid": "oUser123",
                "nickname": "Alice",
                "subscribe": 1,
            })
        )
        user = await adapter.get_user_info("oUser123")
        assert user.nickname == "Alice"
        assert user.subscribe is True

        # second call should hit cache, not the network again
        mock_session.get = MagicMock(side_effect=AssertionError("should use cache"))
        cached = await adapter.get_user_info("oUser123")
        assert cached.nickname == "Alice"

    @pytest.mark.asyncio
    async def test_get_user_info_error_returns_none(self, adapter, mock_session):
        mock_session.get = MagicMock(return_value=_Resp({"errcode": 40003, "errmsg": "bad openid"}))
        user = await adapter.get_user_info("bad-openid")
        assert user is None

    @pytest.mark.asyncio
    async def test_get_chat_info_wraps_user_info(self, adapter, mock_session):
        mock_session.get = MagicMock(
            return_value=_Resp({"openid": "oUser123", "nickname": "Bob", "subscribe": 1})
        )
        info = await adapter.get_chat_info("oUser123")
        assert info["nickname"] == "Bob"
        assert info["subscribed"] is True


class TestTemplateMenuQrMedia:
    def test_template_message_to_dict(self):
        tm = TemplateMessage(template_id="tmpl1", touser="oUser123")
        tm.add_field("status", "Shipped", color="#00FF00")
        d = tm.to_dict()
        assert d["template_id"] == "tmpl1"
        assert d["data"]["status"]["value"] == "Shipped"

    @pytest.mark.asyncio
    async def test_send_template_message_success(self, adapter, mock_session):
        mock_session.post = MagicMock(return_value=_Resp({"errcode": 0, "msgid": 42}))
        tm = TemplateMessage(template_id="tmpl1", touser="oUser123")
        result = await adapter.send_template_message(tm)
        assert result.success is True
        assert result.message_id == "42"

    @pytest.mark.asyncio
    async def test_create_menu_success(self, adapter, mock_session):
        mock_session.post = MagicMock(return_value=_Resp({"errcode": 0}))
        assert await adapter.create_menu({"button": []}) is True

    @pytest.mark.asyncio
    async def test_create_qr_code_permanent(self, adapter, mock_session):
        mock_session.post = MagicMock(return_value=_Resp({"ticket": "abc-ticket"}))
        url = await adapter.create_qr_code("my-scene", permanent=True)
        assert url == "https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket=abc-ticket"

    @pytest.mark.asyncio
    async def test_create_qr_code_no_ticket_returns_none(self, adapter, mock_session):
        mock_session.post = MagicMock(return_value=_Resp({"errcode": 1, "errmsg": "fail"}))
        url = await adapter.create_qr_code("my-scene")
        assert url is None

    @pytest.mark.asyncio
    async def test_get_media_content_success(self, adapter, mock_session):
        mock_session.get = MagicMock(
            return_value=_Resp(content_type="image/jpeg", read_data=b"imgbytes")
        )
        data = await adapter.get_media_content("media1")
        assert data == b"imgbytes"

    @pytest.mark.asyncio
    async def test_get_media_content_error_response_returns_none(self, adapter, mock_session):
        mock_session.get = MagicMock(
            return_value=_Resp({"errcode": 40007, "errmsg": "invalid media_id"})
        )
        data = await adapter.get_media_content("bad-media")
        assert data is None

    @pytest.mark.asyncio
    async def test_mini_program_qr_disabled_returns_none(self, adapter):
        adapter.wechat_config.enable_mini_program = False
        assert await adapter.get_mini_program_qr_code("/pages/home") is None


class TestMessageCrypto:
    def _crypto(self):
        # 43-char base64-ish key like WeChat issues (32 bytes once decoded w/ '=' padding)
        aes_key = base64.b64encode(os.urandom(32)).decode().rstrip("=")[:43]
        return WeChatMessageCrypto(app_id="wx123", encoding_aes_key=aes_key, token="tok")

    def test_encrypt_decrypt_roundtrip(self):
        crypto = self._crypto()
        original = "hello from wechat"
        encrypted = crypto.encrypt(original)
        decrypted = crypto.decrypt(encrypted)
        assert decrypted == original

    def test_verify_signature_matches_wechat_algorithm(self):
        crypto = self._crypto()
        timestamp, nonce, encrypt = "1700000000", "nonce1", "encdata"
        parts = sorted([crypto.token, timestamp, nonce, encrypt])
        expected = hashlib.sha1("".join(parts).encode()).hexdigest()
        assert crypto.verify_signature(expected, timestamp, nonce, encrypt) is True
        assert crypto.verify_signature("wrong", timestamp, nonce, encrypt) is False


class TestFactory:
    def test_create_wechat_adapter_requires_app_id(self):
        with pytest.raises(ValueError, match="app ID"):
            create_wechat_adapter(app_id=None, app_secret="s")

    def test_create_wechat_adapter_requires_app_secret(self):
        with pytest.raises(ValueError, match="app secret"):
            create_wechat_adapter(app_id="wx1", app_secret=None)

    def test_create_wechat_adapter_success(self):
        adapter = create_wechat_adapter(app_id="wx1", app_secret="s1", token="t1")
        assert adapter.wechat_config.app_id == "wx1"
        assert adapter.name == "wechat"

    def test_create_wechat_adapter_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("WECHAT_APP_ID", "wx-env")
        monkeypatch.setenv("WECHAT_APP_SECRET", "secret-env")
        adapter = create_wechat_adapter()
        assert adapter.wechat_config.app_id == "wx-env"

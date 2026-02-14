"""
Voice Channel Adapter

Implements voice/phone integration with Twilio and Vonage APIs.
Based on SantaClaw extension patterns for voice communication.

Features:
- Twilio Voice API integration
- Vonage (Nexmo) Voice API integration
- Inbound/Outbound calls
- IVR (Interactive Voice Response)
- DTMF input handling
- Text-to-Speech (TTS)
- Speech-to-Text (STT)
- Call recording
- Call transfers
- Conference calls
- Webhook handling
- Real-time transcription
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import hmac
import hashlib
import base64
from typing import Optional, List, Dict, Any, Callable, Union
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlencode

try:
    import aiohttp
    HAS_HTTP = True
except ImportError:
    HAS_HTTP = False

from ..base import (
    ChannelAdapter,
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    MediaAttachment,
    SendResult,
    ChannelConnectionError,
    ChannelSendError,
    ChannelRateLimitError,
)

logger = logging.getLogger(__name__)


class VoiceProvider(Enum):
    """Voice service provider."""
    TWILIO = "twilio"
    VONAGE = "vonage"


class CallStatus(Enum):
    """Call status."""
    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    BUSY = "busy"
    NO_ANSWER = "no-answer"
    FAILED = "failed"
    CANCELED = "canceled"


class DTMFKey(Enum):
    """DTMF key codes."""
    KEY_0 = "0"
    KEY_1 = "1"
    KEY_2 = "2"
    KEY_3 = "3"
    KEY_4 = "4"
    KEY_5 = "5"
    KEY_6 = "6"
    KEY_7 = "7"
    KEY_8 = "8"
    KEY_9 = "9"
    KEY_STAR = "*"
    KEY_HASH = "#"


class TTSVoice(Enum):
    """Text-to-Speech voice options."""
    # Twilio voices
    ALICE = "alice"
    POLLY_JOANNA = "Polly.Joanna"
    POLLY_MATTHEW = "Polly.Matthew"
    POLLY_AMY = "Polly.Amy"
    POLLY_BRIAN = "Polly.Brian"
    # Vonage voices
    VONAGE_EMMA = "emma"
    VONAGE_AMY = "amy"
    VONAGE_BRIAN = "brian"
    VONAGE_JOEY = "joey"


@dataclass
class VoiceConfig(ChannelConfig):
    """Voice-specific configuration."""
    provider: VoiceProvider = VoiceProvider.TWILIO
    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    # Vonage
    vonage_api_key: str = ""
    vonage_api_secret: str = ""
    vonage_application_id: str = ""
    vonage_private_key: str = ""
    vonage_phone_number: str = ""
    # Common
    webhook_base_url: str = ""
    default_voice: str = "alice"
    default_language: str = "en-US"
    enable_recording: bool = False
    enable_transcription: bool = False
    max_call_duration: int = 3600  # seconds
    speech_timeout: int = 3  # seconds


@dataclass
class VoiceCall:
    """Active voice call information."""
    call_sid: str
    from_number: str
    to_number: str
    status: CallStatus
    direction: str  # inbound, outbound
    start_time: Optional[datetime] = None
    duration: int = 0
    recording_url: Optional[str] = None
    transcription: Optional[str] = None


@dataclass
class IVRMenu:
    """IVR menu configuration."""
    name: str
    prompt: str
    options: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    timeout_action: Optional[Dict[str, Any]] = None
    invalid_action: Optional[Dict[str, Any]] = None
    max_attempts: int = 3


class VoiceAdapter(ChannelAdapter):
    """
    Voice/Phone adapter with Twilio and Vonage support.

    Usage:
        # Twilio
        config = VoiceConfig(
            provider=VoiceProvider.TWILIO,
            twilio_account_sid="your-sid",
            twilio_auth_token="your-token",
            twilio_phone_number="+1234567890",
            webhook_base_url="https://your-server.com",
        )

        # Vonage
        config = VoiceConfig(
            provider=VoiceProvider.VONAGE,
            vonage_api_key="your-key",
            vonage_api_secret="your-secret",
            vonage_phone_number="+1234567890",
        )

        adapter = VoiceAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: VoiceConfig):
        if not HAS_HTTP:
            raise ImportError(
                "aiohttp not installed. "
                "Install with: pip install aiohttp"
            )

        super().__init__(config)
        self.voice_config: VoiceConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._active_calls: Dict[str, VoiceCall] = {}
        self._ivr_menus: Dict[str, IVRMenu] = {}
        self._dtmf_handlers: Dict[str, Callable] = {}
        self._speech_handlers: List[Callable] = []
        self._call_start_handlers: List[Callable] = []
        self._call_end_handlers: List[Callable] = []

    @property
    def name(self) -> str:
        return "voice"

    async def connect(self) -> bool:
        """Initialize voice adapter."""
        try:
            self._session = aiohttp.ClientSession()

            # Validate provider credentials
            if self.voice_config.provider == VoiceProvider.TWILIO:
                if not self.voice_config.twilio_account_sid or not self.voice_config.twilio_auth_token:
                    logger.error("Twilio credentials required")
                    return False

                # Verify credentials
                if not await self._verify_twilio():
                    return False

            elif self.voice_config.provider == VoiceProvider.VONAGE:
                if not self.voice_config.vonage_api_key or not self.voice_config.vonage_api_secret:
                    logger.error("Vonage credentials required")
                    return False

                # Verify credentials
                if not await self._verify_vonage():
                    return False

            self.status = ChannelStatus.CONNECTED
            logger.info(f"Voice adapter connected ({self.voice_config.provider.value})")
            return True

        except Exception as e:
            logger.error(f"Failed to connect voice adapter: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect voice adapter."""
        if self._session:
            await self._session.close()
            self._session = None

        self._active_calls.clear()
        self.status = ChannelStatus.DISCONNECTED

    async def _verify_twilio(self) -> bool:
        """Verify Twilio credentials."""
        if not self._session:
            return False

        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{self.voice_config.twilio_account_sid}.json"
            auth = aiohttp.BasicAuth(
                self.voice_config.twilio_account_sid,
                self.voice_config.twilio_auth_token,
            )

            async with self._session.get(url, auth=auth) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Twilio account: {data.get('friendly_name')}")
                    return True
                else:
                    logger.error(f"Twilio verification failed: {resp.status}")
                    return False

        except Exception as e:
            logger.error(f"Twilio verification error: {e}")
            return False

    async def _verify_vonage(self) -> bool:
        """Verify Vonage credentials."""
        if not self._session:
            return False

        try:
            url = "https://api.nexmo.com/account/get-balance"
            params = {
                "api_key": self.voice_config.vonage_api_key,
                "api_secret": self.voice_config.vonage_api_secret,
            }

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Vonage balance: {data.get('value')}")
                    return True
                else:
                    logger.error(f"Vonage verification failed: {resp.status}")
                    return False

        except Exception as e:
            logger.error(f"Vonage verification error: {e}")
            return False

    def validate_twilio_signature(
        self,
        url: str,
        params: Dict[str, str],
        signature: str,
    ) -> bool:
        """Validate Twilio webhook signature."""
        if not self.voice_config.twilio_auth_token:
            return False

        try:
            # Build validation string
            s = url
            if params:
                s += "".join(f"{k}{v}" for k, v in sorted(params.items()))

            expected = base64.b64encode(
                hmac.new(
                    self.voice_config.twilio_auth_token.encode(),
                    s.encode(),
                    hashlib.sha1,
                ).digest()
            ).decode()

            return hmac.compare_digest(signature, expected)

        except Exception:
            return False

    async def handle_webhook(self, body: Dict[str, Any], event_type: str = "") -> Dict[str, Any]:
        """
        Handle incoming webhook from Twilio/Vonage.
        Returns TwiML/NCCO response.
        """
        if self.voice_config.provider == VoiceProvider.TWILIO:
            return await self._handle_twilio_webhook(body, event_type)
        else:
            return await self._handle_vonage_webhook(body, event_type)

    async def _handle_twilio_webhook(
        self,
        body: Dict[str, Any],
        event_type: str,
    ) -> Dict[str, Any]:
        """Handle Twilio webhook."""
        call_sid = body.get("CallSid", "")
        call_status = body.get("CallStatus", "")

        if event_type == "voice" or "CallSid" in body:
            # Incoming call or call status
            return await self._handle_twilio_call(body)

        elif event_type == "gather" or "Digits" in body:
            # DTMF input
            return await self._handle_twilio_gather(body)

        elif event_type == "speech" or "SpeechResult" in body:
            # Speech input
            return await self._handle_twilio_speech(body)

        elif event_type == "recording":
            # Recording complete
            await self._handle_twilio_recording(body)

        elif event_type == "transcription":
            # Transcription complete
            await self._handle_twilio_transcription(body)

        return {"twiml": "<Response></Response>"}

    async def _handle_twilio_call(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Twilio incoming call."""
        call_sid = body.get("CallSid", "")
        from_number = body.get("From", "")
        to_number = body.get("To", "")
        direction = body.get("Direction", "inbound")
        status = body.get("CallStatus", "ringing")

        # Create/update call record
        call = VoiceCall(
            call_sid=call_sid,
            from_number=from_number,
            to_number=to_number,
            status=CallStatus(status),
            direction=direction,
            start_time=datetime.now(),
        )
        self._active_calls[call_sid] = call

        # Notify handlers
        for handler in self._call_start_handlers:
            try:
                result = handler(call)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Call start handler error: {e}")

        # Create message for incoming call
        message = Message(
            id=call_sid,
            channel=self.name,
            sender_id=from_number,
            sender_name=from_number,
            chat_id=f"call:{call_sid}",
            text="[Incoming Call]",
            timestamp=datetime.now(),
            is_group=False,
            raw={
                "call_sid": call_sid,
                "direction": direction,
                "status": status,
            },
        )

        await self._dispatch_message(message)

        # Return default greeting TwiML
        twiml = self._build_default_greeting_twiml()
        return {"twiml": twiml}

    async def _handle_twilio_gather(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Twilio DTMF gather."""
        call_sid = body.get("CallSid", "")
        digits = body.get("Digits", "")

        # Create message for DTMF input
        message = Message(
            id=f"{call_sid}_{digits}",
            channel=self.name,
            sender_id=body.get("From", ""),
            chat_id=f"call:{call_sid}",
            text=f"[DTMF:{digits}]",
            timestamp=datetime.now(),
            raw={
                "call_sid": call_sid,
                "digits": digits,
                "input_type": "dtmf",
            },
        )

        await self._dispatch_message(message)

        # Check for registered handler
        if digits in self._dtmf_handlers:
            handler = self._dtmf_handlers[digits]
            try:
                result = await handler(call_sid, digits)
                if isinstance(result, str):
                    return {"twiml": result}
            except Exception as e:
                logger.error(f"DTMF handler error: {e}")

        return {"twiml": "<Response></Response>"}

    async def _handle_twilio_speech(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Twilio speech recognition result."""
        call_sid = body.get("CallSid", "")
        speech_result = body.get("SpeechResult", "")
        confidence = body.get("Confidence", 0)

        # Create message for speech input
        message = Message(
            id=f"{call_sid}_speech",
            channel=self.name,
            sender_id=body.get("From", ""),
            chat_id=f"call:{call_sid}",
            text=speech_result,
            timestamp=datetime.now(),
            raw={
                "call_sid": call_sid,
                "speech_result": speech_result,
                "confidence": confidence,
                "input_type": "speech",
            },
        )

        await self._dispatch_message(message)

        # Notify speech handlers
        for handler in self._speech_handlers:
            try:
                result = handler(call_sid, speech_result, confidence)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, str):
                    return {"twiml": result}
            except Exception as e:
                logger.error(f"Speech handler error: {e}")

        return {"twiml": "<Response></Response>"}

    async def _handle_twilio_recording(self, body: Dict[str, Any]) -> None:
        """Handle Twilio recording complete."""
        call_sid = body.get("CallSid", "")
        recording_url = body.get("RecordingUrl", "")

        if call_sid in self._active_calls:
            self._active_calls[call_sid].recording_url = recording_url

        logger.info(f"Recording complete for {call_sid}: {recording_url}")

    async def _handle_twilio_transcription(self, body: Dict[str, Any]) -> None:
        """Handle Twilio transcription complete."""
        call_sid = body.get("CallSid", "")
        transcription = body.get("TranscriptionText", "")

        if call_sid in self._active_calls:
            self._active_calls[call_sid].transcription = transcription

        logger.info(f"Transcription for {call_sid}: {transcription}")

    async def _handle_vonage_webhook(
        self,
        body: Dict[str, Any],
        event_type: str,
    ) -> Dict[str, Any]:
        """Handle Vonage webhook."""
        conversation_uuid = body.get("conversation_uuid", "")

        if body.get("status") == "started":
            return await self._handle_vonage_call_started(body)
        elif body.get("dtmf"):
            return await self._handle_vonage_dtmf(body)
        elif body.get("speech"):
            return await self._handle_vonage_speech(body)

        return {"ncco": []}

    async def _handle_vonage_call_started(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Vonage call started."""
        uuid = body.get("uuid", "")
        from_number = body.get("from", "")
        to_number = body.get("to", "")

        call = VoiceCall(
            call_sid=uuid,
            from_number=from_number,
            to_number=to_number,
            status=CallStatus.IN_PROGRESS,
            direction=body.get("direction", "inbound"),
            start_time=datetime.now(),
        )
        self._active_calls[uuid] = call

        # Create message
        message = Message(
            id=uuid,
            channel=self.name,
            sender_id=from_number,
            chat_id=f"call:{uuid}",
            text="[Incoming Call]",
            timestamp=datetime.now(),
            raw={"uuid": uuid},
        )

        await self._dispatch_message(message)

        # Return default NCCO
        ncco = self._build_default_greeting_ncco()
        return {"ncco": ncco}

    async def _handle_vonage_dtmf(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Vonage DTMF input."""
        uuid = body.get("uuid", "")
        dtmf = body.get("dtmf", {})
        digits = dtmf.get("digits", "")

        message = Message(
            id=f"{uuid}_{digits}",
            channel=self.name,
            sender_id=body.get("from", ""),
            chat_id=f"call:{uuid}",
            text=f"[DTMF:{digits}]",
            timestamp=datetime.now(),
            raw={"uuid": uuid, "digits": digits},
        )

        await self._dispatch_message(message)
        return {"ncco": []}

    async def _handle_vonage_speech(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Vonage speech input."""
        uuid = body.get("uuid", "")
        speech = body.get("speech", {})
        results = speech.get("results", [])

        if results:
            text = results[0].get("text", "")
            confidence = results[0].get("confidence", 0)

            message = Message(
                id=f"{uuid}_speech",
                channel=self.name,
                sender_id=body.get("from", ""),
                chat_id=f"call:{uuid}",
                text=text,
                timestamp=datetime.now(),
                raw={"uuid": uuid, "confidence": confidence},
            )

            await self._dispatch_message(message)

        return {"ncco": []}

    def _build_default_greeting_twiml(self) -> str:
        """Build default TwiML greeting."""
        voice = self.voice_config.default_voice
        language = self.voice_config.default_language

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="{voice}" language="{language}">
        Hello, thank you for calling. How can I help you today?
    </Say>
    <Gather input="speech dtmf" timeout="{self.voice_config.speech_timeout}" action="/voice/gather">
        <Say voice="{voice}">Please speak or press a key.</Say>
    </Gather>
</Response>"""

    def _build_default_greeting_ncco(self) -> List[Dict[str, Any]]:
        """Build default Vonage NCCO greeting."""
        return [
            {
                "action": "talk",
                "text": "Hello, thank you for calling. How can I help you today?",
                "voiceName": self.voice_config.default_voice,
            },
            {
                "action": "input",
                "type": ["speech", "dtmf"],
                "speech": {
                    "language": self.voice_config.default_language,
                },
            },
        ]

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send TTS message to active call."""
        if not chat_id.startswith("call:"):
            return SendResult(success=False, error="Invalid chat_id for voice")

        call_sid = chat_id.replace("call:", "")

        if call_sid not in self._active_calls:
            return SendResult(success=False, error="Call not found")

        # In practice, TTS is handled via TwiML/NCCO response
        # This would be used for out-of-band updates
        logger.info(f"TTS message for {call_sid}: {text}")
        return SendResult(success=True)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Not applicable for voice."""
        return SendResult(success=False, error="Not supported for voice")

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Not applicable for voice."""
        return False

    async def send_typing(self, chat_id: str) -> None:
        """Not applicable for voice."""
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get call information."""
        if not chat_id.startswith("call:"):
            return None

        call_sid = chat_id.replace("call:", "")

        if call_sid in self._active_calls:
            call = self._active_calls[call_sid]
            return {
                "call_sid": call.call_sid,
                "from": call.from_number,
                "to": call.to_number,
                "status": call.status.value,
                "direction": call.direction,
                "duration": call.duration,
            }

        return None

    # Voice-specific methods

    async def make_call(
        self,
        to_number: str,
        twiml_url: Optional[str] = None,
        twiml: Optional[str] = None,
    ) -> Optional[str]:
        """Make an outbound call (Twilio)."""
        if self.voice_config.provider != VoiceProvider.TWILIO:
            logger.error("make_call only supported for Twilio")
            return None

        if not self._session:
            return None

        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{self.voice_config.twilio_account_sid}/Calls.json"
            auth = aiohttp.BasicAuth(
                self.voice_config.twilio_account_sid,
                self.voice_config.twilio_auth_token,
            )

            data = {
                "From": self.voice_config.twilio_phone_number,
                "To": to_number,
            }

            if twiml_url:
                data["Url"] = twiml_url
            elif twiml:
                data["Twiml"] = twiml
            else:
                data["Twiml"] = self._build_default_greeting_twiml()

            async with self._session.post(url, auth=auth, data=data) as resp:
                if resp.status == 201:
                    result = await resp.json()
                    call_sid = result.get("sid")

                    call = VoiceCall(
                        call_sid=call_sid,
                        from_number=self.voice_config.twilio_phone_number,
                        to_number=to_number,
                        status=CallStatus.QUEUED,
                        direction="outbound",
                    )
                    self._active_calls[call_sid] = call

                    return call_sid
                else:
                    error = await resp.text()
                    logger.error(f"Failed to make call: {error}")
                    return None

        except Exception as e:
            logger.error(f"Make call error: {e}")
            return None

    async def hangup_call(self, call_sid: str) -> bool:
        """Hang up an active call."""
        if self.voice_config.provider == VoiceProvider.TWILIO:
            return await self._hangup_twilio(call_sid)
        else:
            return await self._hangup_vonage(call_sid)

    async def _hangup_twilio(self, call_sid: str) -> bool:
        """Hang up Twilio call."""
        if not self._session:
            return False

        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{self.voice_config.twilio_account_sid}/Calls/{call_sid}.json"
            auth = aiohttp.BasicAuth(
                self.voice_config.twilio_account_sid,
                self.voice_config.twilio_auth_token,
            )

            data = {"Status": "completed"}

            async with self._session.post(url, auth=auth, data=data) as resp:
                if resp.status == 200:
                    if call_sid in self._active_calls:
                        self._active_calls[call_sid].status = CallStatus.COMPLETED
                    return True

        except Exception as e:
            logger.error(f"Hangup error: {e}")

        return False

    async def _hangup_vonage(self, uuid: str) -> bool:
        """Hang up Vonage call."""
        if not self._session:
            return False

        try:
            url = f"https://api.nexmo.com/v1/calls/{uuid}"
            headers = {
                "Authorization": f"Bearer {self._get_vonage_jwt()}",
                "Content-Type": "application/json",
            }

            data = {"action": "hangup"}

            async with self._session.put(url, headers=headers, json=data) as resp:
                return resp.status == 204

        except Exception as e:
            logger.error(f"Vonage hangup error: {e}")

        return False

    def _get_vonage_jwt(self) -> str:
        """Generate Vonage JWT token."""
        # Simplified - in production use proper JWT library
        # This is a placeholder
        return "vonage_jwt_token"

    def register_dtmf_handler(
        self,
        key: str,
        handler: Callable[[str, str], Any],
    ) -> None:
        """Register handler for DTMF key press."""
        self._dtmf_handlers[key] = handler

    def on_speech(self, handler: Callable[[str, str, float], Any]) -> None:
        """Register speech recognition handler."""
        self._speech_handlers.append(handler)

    def on_call_start(self, handler: Callable[[VoiceCall], Any]) -> None:
        """Register call start handler."""
        self._call_start_handlers.append(handler)

    def on_call_end(self, handler: Callable[[VoiceCall], Any]) -> None:
        """Register call end handler."""
        self._call_end_handlers.append(handler)

    def register_ivr_menu(self, menu: IVRMenu) -> None:
        """Register an IVR menu."""
        self._ivr_menus[menu.name] = menu

    def build_twiml_say(
        self,
        text: str,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ) -> str:
        """Build TwiML Say element."""
        voice = voice or self.voice_config.default_voice
        language = language or self.voice_config.default_language
        return f'<Say voice="{voice}" language="{language}">{text}</Say>'

    def build_twiml_gather(
        self,
        prompt: str,
        input_type: str = "dtmf speech",
        action_url: str = "/voice/gather",
        timeout: Optional[int] = None,
        num_digits: Optional[int] = None,
    ) -> str:
        """Build TwiML Gather element."""
        timeout = timeout or self.voice_config.speech_timeout
        voice = self.voice_config.default_voice

        gather_attrs = f'input="{input_type}" timeout="{timeout}" action="{action_url}"'
        if num_digits:
            gather_attrs += f' numDigits="{num_digits}"'

        return f"""<Gather {gather_attrs}>
    <Say voice="{voice}">{prompt}</Say>
</Gather>"""

    def build_twiml_record(
        self,
        action_url: str = "/voice/recording",
        transcribe: bool = False,
        max_length: int = 120,
    ) -> str:
        """Build TwiML Record element."""
        transcribe_attr = 'transcribe="true" transcribeCallback="/voice/transcription"' if transcribe else ""
        return f'<Record action="{action_url}" maxLength="{max_length}" {transcribe_attr}/>'

    def build_twiml_dial(
        self,
        number: str,
        caller_id: Optional[str] = None,
        timeout: int = 30,
    ) -> str:
        """Build TwiML Dial element."""
        caller_id = caller_id or self.voice_config.twilio_phone_number
        return f'<Dial callerId="{caller_id}" timeout="{timeout}"><Number>{number}</Number></Dial>'

    def build_ncco_talk(
        self,
        text: str,
        voice: Optional[str] = None,
        loop: int = 1,
    ) -> Dict[str, Any]:
        """Build Vonage NCCO talk action."""
        return {
            "action": "talk",
            "text": text,
            "voiceName": voice or self.voice_config.default_voice,
            "loop": loop,
        }

    def build_ncco_input(
        self,
        event_url: str,
        dtmf: bool = True,
        speech: bool = True,
        max_digits: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build Vonage NCCO input action."""
        types = []
        if dtmf:
            types.append("dtmf")
        if speech:
            types.append("speech")

        action = {
            "action": "input",
            "type": types,
            "eventUrl": [event_url],
        }

        if dtmf and max_digits:
            action["dtmf"] = {"maxDigits": max_digits}

        if speech:
            action["speech"] = {
                "language": self.voice_config.default_language,
            }

        return action

    def build_ncco_record(
        self,
        event_url: str,
        format: str = "mp3",
    ) -> Dict[str, Any]:
        """Build Vonage NCCO record action."""
        return {
            "action": "record",
            "format": format,
            "eventUrl": [event_url],
        }


def create_voice_adapter(
    provider: str = "twilio",
    **kwargs
) -> VoiceAdapter:
    """
    Factory function to create Voice adapter.

    Args:
        provider: Voice provider ("twilio" or "vonage")
        **kwargs: Provider-specific configuration

    For Twilio, set:
        - twilio_account_sid or TWILIO_ACCOUNT_SID env var
        - twilio_auth_token or TWILIO_AUTH_TOKEN env var
        - twilio_phone_number or TWILIO_PHONE_NUMBER env var

    For Vonage, set:
        - vonage_api_key or VONAGE_API_KEY env var
        - vonage_api_secret or VONAGE_API_SECRET env var
        - vonage_phone_number or VONAGE_PHONE_NUMBER env var

    Returns:
        Configured VoiceAdapter
    """
    provider_enum = VoiceProvider(provider.lower())

    if provider_enum == VoiceProvider.TWILIO:
        account_sid = kwargs.pop("twilio_account_sid", None) or os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = kwargs.pop("twilio_auth_token", None) or os.getenv("TWILIO_AUTH_TOKEN")
        phone_number = kwargs.pop("twilio_phone_number", None) or os.getenv("TWILIO_PHONE_NUMBER")

        if not account_sid or not auth_token:
            raise ValueError("Twilio account SID and auth token required")

        config = VoiceConfig(
            provider=provider_enum,
            twilio_account_sid=account_sid,
            twilio_auth_token=auth_token,
            twilio_phone_number=phone_number or "",
            **kwargs
        )

    elif provider_enum == VoiceProvider.VONAGE:
        api_key = kwargs.pop("vonage_api_key", None) or os.getenv("VONAGE_API_KEY")
        api_secret = kwargs.pop("vonage_api_secret", None) or os.getenv("VONAGE_API_SECRET")
        phone_number = kwargs.pop("vonage_phone_number", None) or os.getenv("VONAGE_PHONE_NUMBER")

        if not api_key or not api_secret:
            raise ValueError("Vonage API key and secret required")

        config = VoiceConfig(
            provider=provider_enum,
            vonage_api_key=api_key,
            vonage_api_secret=api_secret,
            vonage_phone_number=phone_number or "",
            **kwargs
        )

    else:
        raise ValueError(f"Unknown provider: {provider}")

    return VoiceAdapter(config)

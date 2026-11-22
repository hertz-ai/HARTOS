"""
Email Channel Adapter

Implements email messaging via IMAP/SMTP.
Based on SantaClaw extension patterns for email.

Features:
- IMAP for receiving emails
- SMTP for sending emails
- Email threading support (In-Reply-To, References headers)
- HTML and plain text support
- Attachment handling
- TLS/SSL support
- Docker-compatible configuration (container networking)
- Connection pooling
- Idle push notifications (IMAP IDLE)
"""

from __future__ import annotations

import asyncio
import logging
import os
import email
import email.utils
import email.mime.text
import email.mime.multipart
import email.mime.base
import email.mime.image
import email.mime.audio
from email.header import decode_header, make_header
from email.utils import parseaddr, formataddr
import imaplib
import smtplib
import ssl
from typing import Optional, List, Dict, Any, Callable, Tuple
from datetime import datetime
from dataclasses import dataclass, field
import re
import hashlib
import mimetypes
import base64
from pathlib import Path

try:
    import aioimaplib
    HAS_AIOIMAP = True
except ImportError:
    HAS_AIOIMAP = False

try:
    import aiosmtplib
    HAS_AIOSMTP = True
except ImportError:
    HAS_AIOSMTP = False

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

#: SMTP replies to AUTH that refuse the credentials themselves (RFC 4954):
#: 535 "authentication credentials invalid" and 534 "authentication
#: mechanism is too weak" (Gmail's "Application-specific password
#: required").  smtplib raises SMTPAuthenticationError for EVERY reply to
#: AUTH but 235 and 503, so 454 (temporary failure, too many attempts), 421
#: (closing) and 530/538 reach it too: none of those is a password a new one
#: would fix (review of d072f8ea6, rv_d072: a 454 with the right password
#: filed a "rejected" card and used up an entry).
_SMTP_REFUSED_CODES = (534, 535)

#: RFC 5530 response codes of a NO to LOGIN that refuse the credentials
#: themselves; a NO with no response code is RFC 3501's "user name or
#: password rejected" (Outlook: "LOGIN failed.").  Any other code is not a
#: refusal a new password can fix: LIMIT (too many connections),
#: CONTACTADMIN, PRIVACYREQUIRED, UNAVAILABLE, SERVERBUG, INUSE, ... and
#: ALERT (RFC 3501: words the person must see, no cause a client can read;
#: Gmail sends "[ALERT] Your account is not enabled for IMAP use" and
#: "[ALERT] Please log in via your web browser").  Those are not refused,
#: and their words reach the reply (_server_words).
_IMAP_REFUSED_CODES = ('AUTHENTICATIONFAILED', 'AUTHORIZATIONFAILED', 'EXPIRED')

#: The response code a reply's text opens with: "[LIMIT] Too many ...".
_RESPONSE_CODE = re.compile(r'\s*\[([^\]\s]+)')

#: How imaplib words an error of its own, not a reply the server gave LOGIN:
#: "LOGIN command error: BAD [...]" for a BAD, "command: LOGIN => ..." and
#: "command LOGIN illegal in state ..." for a broken exchange.
_IMAPLIB_OWN_WORDS = re.compile(r'\s*(?:\w+ command error:|command[ :])')

#: Longest piece of a server's words a reply or a log line carries.
_SERVER_WORDS_CAP = 240


def _smtp_reply_errors() -> tuple:
    """The SMTP exception classes that carry a server's reply (code, text):
    smtplib's, and aiosmtplib's when it is installed."""
    aio = getattr(aiosmtplib, 'SMTPResponseException', None) if HAS_AIOSMTP else None
    return (smtplib.SMTPResponseException,) + ((aio,) if aio else ())


def _smtp_reply(error) -> Tuple[Optional[int], str]:
    """The (code, text) of an SMTP reply ``error`` carries: smtplib names
    them smtp_code / smtp_error, aiosmtplib code / message."""
    code = getattr(error, 'smtp_code', None)
    text = getattr(error, 'smtp_error', None)
    if code is None:
        code, text = getattr(error, 'code', None), getattr(error, 'message', '')
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    return code, ' '.join(str(text or '').split())


def _imap_text(error) -> str:
    """The text an imaplib error carries: a NO to LOGIN is raised with the
    server's own bytes, imaplib's own errors with words of its own."""
    arg = error.args[0] if error.args else ''
    if isinstance(arg, bytes):
        arg = arg.decode('utf-8', 'replace')
    return ' '.join(str(arg).split())


def _imap_no_refuses(text) -> bool:
    """Whether a NO to LOGIN whose text is ``text`` refuses the credentials:
    it carries no response code, or one of _IMAP_REFUSED_CODES."""
    code = _RESPONSE_CODE.match(text or '')
    return code is None or code.group(1).upper() in _IMAP_REFUSED_CODES


def _login_refused(error) -> bool:
    """Whether ``error``, raised while logging in, is the server refusing
    the address or the password: the one outcome a new password can fix,
    and so the one that asks the owner for another.

    SMTP: a reply to AUTH of _SMTP_REFUSED_CODES (535, 534), from smtplib
    or aiosmtplib, by one rule.  IMAP: the error imaplib raises when LOGIN
    is answered NO with no response code or a credential code
    (_imap_no_refuses); not IMAP4.abort (the connection dropped or the
    server said BYE), not imaplib's own errors (a BAD, a broken exchange:
    _IMAPLIB_OWN_WORDS), and not a NO with any other code.  A server that
    cannot be resolved, refuses the connection or times out raises none of
    these (socket.gaierror, ConnectionRefusedError, TimeoutError), so a
    right password on a node whose network is down is never read as
    refused."""
    if isinstance(error, _smtp_reply_errors()):
        return _smtp_reply(error)[0] in _SMTP_REFUSED_CODES
    if (not isinstance(error, imaplib.IMAP4.error)
            or isinstance(error, imaplib.IMAP4.abort)):
        return False
    text = _imap_text(error)
    return not _IMAPLIB_OWN_WORDS.match(text) and _imap_no_refuses(text)


def _login_answered(error) -> bool:
    """Whether ``error``, raised while logging in, is a server's own answer
    (refused or not): an SMTP reply of any code (smtplib's or aiosmtplib's
    SMTPResponseException: 535, 534, 454, 421, ...) or an IMAP error that is
    not IMAP4.abort (a NO with or without a response code, a BAD, a greeting
    that is not OK).  Not a name that does not resolve, a refused or dropped
    connection, a timeout, a BYE in place of the login's answer or a broken
    TLS handshake (OSError, SMTPServerDisconnected, IMAP4.abort): no answer
    to the login came back for those.  Read from the error's type, as
    _login_refused is, never from its words."""
    return (isinstance(error, _smtp_reply_errors())
            or (isinstance(error, imaplib.IMAP4.error)
                and not isinstance(error, imaplib.IMAP4.abort)))


def _server_words(error) -> str:
    """What a failed connect was told, for the owner and the log: the
    server's own reply (an SMTP reply's code and text, an IMAP NO's text) or
    the network's error, never a setting's value.  Any other error is named
    by its type alone: its message might quote what was being sent."""
    if isinstance(error, _smtp_reply_errors()):
        code, text = _smtp_reply(error)
        words = f"{code} {text}".strip()
    elif (isinstance(error, imaplib.IMAP4.error)
            and not isinstance(error, imaplib.IMAP4.abort)
            and not _IMAPLIB_OWN_WORDS.match(_imap_text(error))):
        words = _imap_text(error)
    elif isinstance(error, (imaplib.IMAP4.error, OSError, smtplib.SMTPException)):
        words = f"{type(error).__name__}: {' '.join(str(error).split())}"
    else:
        words = type(error).__name__
    return words[:_SERVER_WORDS_CAP]


def _aioimap_login_error(response):
    """The error a LOGIN aioimaplib answered with ``response`` stands for, as
    imaplib raises it (a NO with the server's own words, anything else in
    imaplib's own), or None when the login was accepted.  aioimaplib returns
    the server's answer instead of raising, so a refused login looked
    connected; this reads it by the same rule (_login_refused)."""
    result = str(getattr(response, 'result', 'OK') or 'OK').upper()
    if result == 'OK':
        return None
    # The tagged reply's text is the last line, as imaplib reads dat[-1].
    last = (getattr(response, 'lines', None) or [b''])[-1]
    text = last if isinstance(last, bytes) else str(last).encode()
    if result == 'NO':
        return imaplib.IMAP4.error(text)
    return imaplib.IMAP4.error(f"LOGIN command error: {result} {text!r}")


#: The key a message handler sets in ``Message.raw`` (True) when it took an
#: inbound email.  A mailbox that leaves its mail unread (mark_as_read
#: False) marks only those read: the rest stay unread for its owner.
HANDLED = 'handled'

def _message_id(header_value) -> str:
    """The id a mail goes by, from its Message-ID header value: with no
    whitespace in it.  An id has none; a header folded onto a second line
    (Exchange and Outlook do it) arrives with a line break and spaces in
    front of it, and with them the same mail read from its header alone and
    from the whole message gave two ids, so it was downloaded again on every
    poll (review of ab8e0c840, S2).  One reader for both, so the two agree.
    '' for a mail with no Message-ID."""
    return ''.join(str(header_value or '').split())


#: Longest IMAP sequence-set (characters) one FETCH names: servers cap a
#: command line (typically 8 KB), and a big inbox's set is split under it.
_SEQUENCE_SET_LIMIT = 1000


def _sequence_sets(message_nums, limit: int = _SEQUENCE_SET_LIMIT) -> List[str]:
    """IMAP sequence-sets naming ``message_nums`` (the b'7' items of a SEARCH
    reply): consecutive numbers as ranges ('3:9,12'), split into sets of at
    most ``limit`` characters; one set for any inbox a command line holds."""
    numbers = sorted({int(n) for n in message_nums})
    sets, parts, size, i = [], [], 0, 0
    while i < len(numbers):
        j = i
        while j + 1 < len(numbers) and numbers[j + 1] == numbers[j] + 1:
            j += 1
        part = str(numbers[i]) if i == j else f'{numbers[i]}:{numbers[j]}'
        if parts and size + len(part) + 1 > limit:
            sets.append(','.join(parts))
            parts, size = [], 0
        parts.append(part)
        size += len(part) + 1
        i = j + 1
    if parts:
        sets.append(','.join(parts))
    return sets


@dataclass
class EmailConfig(ChannelConfig):
    """Email-specific configuration."""
    # IMAP settings
    imap_host: str = ""
    imap_port: int = 993
    imap_use_ssl: bool = True
    imap_username: str = ""
    imap_password: str = ""

    # SMTP settings
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_username: str = ""
    smtp_password: str = ""

    # Email settings
    email_address: str = ""  # From address
    display_name: Optional[str] = None
    reply_to: Optional[str] = None
    default_subject: str = "Message from Agent"

    # Folders
    inbox_folder: str = "INBOX"
    sent_folder: str = "Sent"
    archive_folder: Optional[str] = None

    # Behavior
    poll_interval: int = 30  # Seconds between IMAP polls
    use_idle: bool = True  # Use IMAP IDLE if available
    # True: every polled email is marked read.  False: emails are read with
    # PEEK and marked read only when a handler took one (HANDLED); a goal's
    # company mailbox leaves its owner's other mail unread.
    mark_as_read: bool = True
    archive_after_reply: bool = False
    html_emails: bool = True
    max_attachment_size: int = 25 * 1024 * 1024  # 25MB

    # Docker networking
    # When running in Docker, use container hostnames
    docker_mode: bool = False
    docker_imap_host: Optional[str] = None  # e.g., "mailserver"
    docker_smtp_host: Optional[str] = None  # e.g., "mailserver"


@dataclass
class EmailThread:
    """Email thread tracking."""
    thread_id: str
    subject: str
    participants: List[str]
    message_ids: List[str] = field(default_factory=list)
    last_message_id: Optional[str] = None
    references: List[str] = field(default_factory=list)


class EmailAdapter(ChannelAdapter):
    """
    Email messaging adapter using IMAP/SMTP.

    Supports both sync and async operations. For best performance in
    async environments, install aioimaplib and aiosmtplib.

    Docker Configuration:
        When running in Docker, set docker_mode=True and configure
        docker_imap_host/docker_smtp_host to use container hostnames
        for internal communication.

    Usage:
        config = EmailConfig(
            imap_host="imap.gmail.com",
            imap_username="bot@example.com",
            imap_password="app-password",
            smtp_host="smtp.gmail.com",
            smtp_username="bot@example.com",
            smtp_password="app-password",
            email_address="bot@example.com",
        )
        adapter = EmailAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: EmailConfig):
        super().__init__(config)
        self.email_config: EmailConfig = config
        self._imap: Optional[Any] = None
        self._smtp: Optional[Any] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._threads: Dict[str, EmailThread] = {}
        self._seen_message_ids: set = set()
        # The UIDs of processed mail that has no Message-ID (a mailbox that
        # leaves its mail unread; _unprocessed).
        self._seen_uids: set = set()
        # A mailbox that leaves its mail unread reads through imaplib, where
        # its peek-and-mark-when-handled read is written (_check_messages_sync).
        self._use_async = HAS_AIOIMAP and HAS_AIOSMTP and config.mark_as_read
        # Why the last connect() returned False, beside its bool (every
        # caller keeps reading the bool): True only when a server answered
        # the login with a refusal of the address or password
        # (_login_refused).  False after a success, and after a server that
        # could not be resolved, refused the connection or did not answer:
        # those say nothing about the password, so nobody is asked for it
        # again (FlaskChannelIntegration.probe_login).
        self.login_refused = False
        # Beside it: True when a server answered that login at all, refused
        # or not (_login_answered: an IMAP NO or BAD, an SMTP reply of any
        # code); False when none did (a name that does not resolve, a refused
        # or dropped connection, a timeout).  probe_login words the three
        # apart: refused, answered without refusing (the server's words are
        # what to act on), and not reached (review of 30911e8b6: a [LIMIT] or
        # a 454 read "could not be reached").
        self.login_answered = False
        # What that connect was told, in the server's (or the network's) own
        # words, which server first ("IMAP: [ALERT] Your account is not
        # enabled for IMAP use."; both, "IMAP: ...; SMTP: ...", when IMAP's
        # answer left the password to SMTP's): never a setting's value
        # (_server_words).  '' after a success.  probe_login puts it in its
        # ``why``, so the owner learns what to do (enable IMAP, try later).
        self.connect_error = ''

    @property
    def name(self) -> str:
        return "email"

    def _get_imap_host(self) -> str:
        """Get IMAP host, considering Docker mode."""
        if self.email_config.docker_mode and self.email_config.docker_imap_host:
            return self.email_config.docker_imap_host
        return self.email_config.imap_host

    def _get_smtp_host(self) -> str:
        """Get SMTP host, considering Docker mode."""
        if self.email_config.docker_mode and self.email_config.docker_smtp_host:
            return self.email_config.docker_smtp_host
        return self.email_config.smtp_host

    async def connect(self) -> bool:
        """Initialize IMAP and SMTP connections."""
        self.login_refused, self.login_answered, self.connect_error = False, False, ''
        if not self.email_config.imap_host or not self.email_config.smtp_host:
            logger.error("IMAP and SMTP hosts required")
            return False

        if not self.email_config.email_address:
            logger.error("Email address required")
            return False

        try:
            # Connect to IMAP
            imap_connected = await self._connect_imap()
            if not imap_connected:
                logger.error("Failed to connect to IMAP server")
                if self.login_answered and not self.login_refused:
                    # IMAP answered the login without refusing the password
                    # (a NO with [ALERT] or [LIMIT], a BAD), so whether the
                    # password is right is SMTP's answer to the same one.
                    # Gmail's IMAP says "[ALERT] Application-specific
                    # password required" where its SMTP refuses with 534:
                    # without this the owner was never asked for an App
                    # Password and the same password was tried every turn
                    # (review of 30911e8b6).  One SMTP login per connect,
                    # never a retry; connect stays False (IMAP failed).
                    await self._test_smtp()
                return False

            # Test SMTP connection
            smtp_ok = await self._test_smtp()
            if not smtp_ok:
                logger.error("Failed to connect to SMTP server")
                return False

            self.status = ChannelStatus.CONNECTED
            logger.info(f"Email adapter connected as: {self.email_config.email_address}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect email: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def _connect_imap(self) -> bool:
        """Connect to IMAP server."""
        try:
            host = self._get_imap_host()
            port = self.email_config.imap_port

            if self._use_async:
                # Async IMAP
                if self.email_config.imap_use_ssl:
                    self._imap = aioimaplib.IMAP4_SSL(host, port)
                else:
                    self._imap = aioimaplib.IMAP4(host, port)

                await self._imap.wait_hello_from_server()
                refused = _aioimap_login_error(await self._imap.login(
                    self.email_config.imap_username,
                    self.email_config.imap_password
                ))
                if refused is not None:
                    # Answered by the server, not raised: the same rule.
                    self.login_refused = _login_refused(refused)
                    raise refused
            else:
                # Sync IMAP (run in executor)
                loop = asyncio.get_event_loop()

                def connect_sync():
                    if self.email_config.imap_use_ssl:
                        context = ssl.create_default_context()
                        imap = imaplib.IMAP4_SSL(host, port, ssl_context=context)
                    else:
                        imap = imaplib.IMAP4(host, port)

                    try:
                        imap.login(
                            self.email_config.imap_username,
                            self.email_config.imap_password
                        )
                    except imaplib.IMAP4.error as e:
                        # Raised by LOGIN itself: the server answered it.
                        self.login_refused = _login_refused(e)
                        raise
                    return imap

                self._imap = await loop.run_in_executor(None, connect_sync)

            return True

        except Exception as e:
            self.login_answered = _login_answered(e)
            # The server's (or the network's) words, never a setting's value.
            self.connect_error = f"IMAP: {_server_words(e)}"
            logger.error("IMAP connection error: %s", self.connect_error)
            return False

    async def _test_smtp(self) -> bool:
        """Test SMTP connection."""
        try:
            host = self._get_smtp_host()
            port = self.email_config.smtp_port

            if self._use_async:
                # Async SMTP
                smtp = aiosmtplib.SMTP(
                    hostname=host,
                    port=port,
                    use_tls=not self.email_config.smtp_use_tls,  # use_tls means implicit TLS
                    start_tls=self.email_config.smtp_use_tls,
                )
                await smtp.connect()
                await smtp.login(
                    self.email_config.smtp_username,
                    self.email_config.smtp_password
                )
                await smtp.quit()
            else:
                # Sync SMTP
                loop = asyncio.get_event_loop()

                def test_sync():
                    if self.email_config.smtp_use_tls:
                        smtp = smtplib.SMTP(host, port)
                        smtp.starttls()
                    else:
                        context = ssl.create_default_context()
                        smtp = smtplib.SMTP_SSL(host, port, context=context)

                    smtp.login(
                        self.email_config.smtp_username,
                        self.email_config.smtp_password
                    )
                    smtp.quit()

                await loop.run_in_executor(None, test_sync)

            return True

        except Exception as e:
            self.login_refused = _login_refused(e)
            # An IMAP answer before this one (connect) stays answered.
            self.login_answered = self.login_answered or _login_answered(e)
            said = f"SMTP: {_server_words(e)}"
            # After an IMAP answer that left the password to SMTP (connect),
            # both servers' words: what IMAP said is still the owner's to read.
            self.connect_error = '; '.join(filter(None, [self.connect_error, said]))
            logger.error("SMTP connection error: %s", said)
            return False

    async def disconnect(self) -> None:
        """Disconnect from email servers."""
        # Cancel polling task
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._idle_task:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass

        # Close IMAP
        if self._imap:
            try:
                if self._use_async:
                    await self._imap.logout()
                else:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self._imap.logout)
            except Exception:
                pass
            self._imap = None

        self.status = ChannelStatus.DISCONNECTED

    async def start(self) -> None:
        """Start the email adapter and begin polling/IDLE."""
        await super().start()

        if self.status == ChannelStatus.CONNECTED:
            # Start polling for new messages
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Poll for new messages."""
        while self._running and self.status == ChannelStatus.CONNECTED:
            try:
                await self._check_new_messages()
                await asyncio.sleep(self.email_config.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in poll loop: {e}")
                await asyncio.sleep(self.email_config.poll_interval)

    async def _check_new_messages(self) -> None:
        """Check for new messages in inbox."""
        if not self._imap:
            return

        try:
            if self._use_async:
                await self._check_messages_async()
            else:
                await self._check_messages_sync()
        except Exception as e:
            logger.error(f"Error checking messages: {e}")
            # Try to reconnect
            await self._connect_imap()

    async def _check_messages_async(self) -> None:
        """Check messages using async IMAP."""
        try:
            # Select inbox
            await self._imap.select(self.email_config.inbox_folder)

            # Search for unseen messages
            status, data = await self._imap.search('UNSEEN')
            if status != 'OK':
                return

            message_nums = data[0].split()

            for num in message_nums:
                # Fetch message
                status, msg_data = await self._imap.fetch(num, '(RFC822)')
                if status != 'OK':
                    continue

                # Parse and process
                raw_email = msg_data[1]
                await self._process_email(raw_email, num)

        except Exception as e:
            logger.error(f"Async message check error: {e}")
            raise

    def _unprocessed(self, message_nums) -> List[Tuple[bytes, Optional[bytes], str]]:
        """For a mailbox that leaves mail unread: of the UNSEEN message
        numbers, the ones not processed yet, as (number, UID, Message-ID).

        Unhandled mail stays UNSEEN, so every poll meets the owner's whole
        unread inbox again.  One FETCH of the UID and Message-ID headers per
        sequence set (_sequence_sets: one for a whole inbox, not one per
        mail) says which are new; only those are fetched whole.  A mail with
        no Message-ID is known by its UID (the server's stable number for
        it), so it is not fetched whole on every poll either (review of
        88b6821f2, S2)."""
        fresh = []
        for message_set in _sequence_sets(message_nums):
            status, head = self._imap.fetch(
                message_set, '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            if status != 'OK' or not head:
                # Unknown: every mail is fetched whole, as before.
                return [(num, None, '') for num in message_nums]
            for at, item in enumerate(head):
                if not isinstance(item, tuple):
                    continue
                prefix = bytes(item[0])
                number = re.match(rb'\s*(\d+)', prefix)
                # Where the server puts the UID varies: before the header
                # literal, or in the closing ' UID 11)' after it.
                uid = re.search(rb'UID\s+(\d+)', prefix)
                closing = head[at + 1] if at + 1 < len(head) else None
                if uid is None and isinstance(closing, bytes):
                    uid = re.search(rb'UID\s+(\d+)', closing)
                uid = uid.group(1) if uid else None
                msg_id = _message_id(email.message_from_bytes(
                    bytes(item[1])).get('Message-ID'))
                if number is None:
                    continue
                if msg_id and msg_id in self._seen_message_ids:
                    continue
                if not msg_id and uid is not None and uid in self._seen_uids:
                    continue
                fresh.append((number.group(1), uid, msg_id))
        return fresh

    async def _check_messages_sync(self) -> None:
        """Check messages using sync IMAP."""
        loop = asyncio.get_event_loop()

        peek = not self.email_config.mark_as_read
        if peek and not self._message_handlers:
            # A mailbox that leaves its mail unread is read only for a
            # handler to take it (the outreach reply hook).  With none
            # attached yet, polling would record each Message-ID as seen and
            # a prospect's reply already waiting here would never be matched
            # once the hook arrives (review of 88b6821f2, S1).
            return

        def check_sync():
            # Select inbox
            self._imap.select(self.email_config.inbox_folder)

            # Search for unseen messages
            status, data = self._imap.search(None, 'UNSEEN')
            if status != 'OK':
                return []

            message_nums = data[0].split()
            if peek:
                wanted = self._unprocessed(message_nums)
            else:
                wanted = [(num, None, '') for num in message_nums]
            messages = []

            for num, uid, msg_id in wanted:
                # Fetch message.  RFC822 sets \Seen on the server; BODY.PEEK[]
                # leaves the message unread.
                status, msg_data = self._imap.fetch(
                    num, '(BODY.PEEK[])' if peek else '(RFC822)')
                if status != 'OK':
                    continue

                raw_email = msg_data[0][1]
                messages.append((raw_email, num, uid, msg_id))

            return messages

        messages = await loop.run_in_executor(None, check_sync)

        for raw_email, num, uid, msg_id in messages:
            await self._process_email(raw_email, num)
            if uid is not None and not msg_id:
                self._seen_uids.add(uid)

    async def _process_email(self, raw_email: bytes, msg_num: bytes) -> None:
        """Process a raw email message."""
        try:
            # Parse email
            msg = email.message_from_bytes(raw_email)

            # Get message ID
            message_id = _message_id(msg.get('Message-ID'))

            # Skip if already seen.  A mail with no Message-ID has no id to
            # remember: '' recorded as seen dropped every later such mail
            # (a mailbox that leaves its mail unread tracks them by UID).
            if message_id:
                if message_id in self._seen_message_ids:
                    return
                self._seen_message_ids.add(message_id)

            # Convert to unified message
            unified_msg = self._convert_message(msg)

            # Mark as read if configured
            if self.email_config.mark_as_read:
                await self._mark_as_read(msg_num)

            # Dispatch to handlers
            await self._dispatch_message(unified_msg)

            # A mailbox that leaves its mail unread marks only what a handler
            # took (HANDLED); the rest stays unread for its owner.
            if not self.email_config.mark_as_read and unified_msg.raw.get(HANDLED):
                await self._mark_as_read(msg_num)

        except Exception as e:
            logger.error(f"Error processing email: {e}")

    def _convert_message(self, msg: email.message.Message) -> Message:
        """Convert email to unified Message format."""
        # Get sender
        from_header = msg.get('From', '')
        sender_name, sender_email = parseaddr(from_header)

        # Decode sender name if needed
        if sender_name:
            try:
                sender_name = str(make_header(decode_header(sender_name)))
            except Exception:
                pass

        # Get subject
        subject = msg.get('Subject', '')
        try:
            subject = str(make_header(decode_header(subject)))
        except Exception:
            pass

        # Get message ID and threading info
        message_id = msg.get('Message-ID', f"<{hashlib.md5(str(msg).encode()).hexdigest()}@local>")
        in_reply_to = msg.get('In-Reply-To')
        references = msg.get('References', '').split()

        # Generate thread ID from subject or references
        thread_id = self._get_thread_id(subject, references, in_reply_to)

        # Track thread
        if thread_id not in self._threads:
            self._threads[thread_id] = EmailThread(
                thread_id=thread_id,
                subject=subject,
                participants=[sender_email],
            )

        thread = self._threads[thread_id]
        thread.message_ids.append(message_id)
        thread.last_message_id = message_id
        if references:
            thread.references = references

        # Get body and attachments
        text_body, html_body, attachments = self._extract_body_and_attachments(msg)

        # Prefer text over HTML
        text = text_body or self._html_to_text(html_body) if html_body else ""

        # Convert attachments to MediaAttachment
        media = []
        for att_filename, att_data, att_type in attachments:
            media_type = MessageType.DOCUMENT
            if att_type and att_type.startswith('image/'):
                media_type = MessageType.IMAGE
            elif att_type and att_type.startswith('video/'):
                media_type = MessageType.VIDEO
            elif att_type and att_type.startswith('audio/'):
                media_type = MessageType.AUDIO

            media.append(MediaAttachment(
                type=media_type,
                file_name=att_filename,
                mime_type=att_type,
                file_size=len(att_data) if att_data else None,
            ))

        # Parse date
        date_str = msg.get('Date')
        timestamp = datetime.now()
        if date_str:
            try:
                parsed = email.utils.parsedate_to_datetime(date_str)
                timestamp = parsed.replace(tzinfo=None)
            except Exception:
                pass

        return Message(
            id=message_id,
            channel=self.name,
            sender_id=sender_email,
            sender_name=sender_name or sender_email,
            chat_id=thread_id,
            text=text,
            media=media,
            reply_to_id=in_reply_to,
            timestamp=timestamp,
            is_group=len(thread.participants) > 2,
            raw={
                'subject': subject,
                'from': from_header,
                'to': msg.get('To'),
                'cc': msg.get('Cc'),
                'message_id': message_id,
                'in_reply_to': in_reply_to,
                'references': references,
                'html_body': html_body,
                'attachments': [(f, t) for f, _, t in attachments],
            },
        )

    def _get_thread_id(
        self,
        subject: str,
        references: List[str],
        in_reply_to: Optional[str],
    ) -> str:
        """Generate thread ID from email headers."""
        # Use first reference or in-reply-to as base
        if references:
            return hashlib.md5(references[0].encode()).hexdigest()[:16]
        if in_reply_to:
            return hashlib.md5(in_reply_to.encode()).hexdigest()[:16]

        # Use normalized subject
        normalized = re.sub(r'^(re:|fwd?:)\s*', '', subject.lower(), flags=re.IGNORECASE)
        return hashlib.md5(normalized.encode()).hexdigest()[:16]

    def _extract_body_and_attachments(
        self,
        msg: email.message.Message,
    ) -> Tuple[str, str, List[Tuple[str, bytes, str]]]:
        """Extract text body, HTML body, and attachments from email."""
        text_body = ""
        html_body = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get('Content-Disposition', ''))

                # Check if it's an attachment
                if 'attachment' in content_disposition:
                    filename = part.get_filename() or 'attachment'
                    try:
                        filename = str(make_header(decode_header(filename)))
                    except Exception:
                        pass

                    data = part.get_payload(decode=True)
                    attachments.append((filename, data, content_type))
                elif content_type == 'text/plain':
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or 'utf-8'
                        text_body = payload.decode(charset, errors='replace')
                elif content_type == 'text/html':
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or 'utf-8'
                        html_body = payload.decode(charset, errors='replace')
        else:
            content_type = msg.get_content_type()
            payload = msg.get_payload(decode=True)

            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                if content_type == 'text/plain':
                    text_body = payload.decode(charset, errors='replace')
                elif content_type == 'text/html':
                    html_body = payload.decode(charset, errors='replace')

        return text_body, html_body, attachments

    def _html_to_text(self, html: str) -> str:
        """Convert HTML to plain text."""
        # Simple HTML to text conversion
        text = re.sub(r'<br\s*/?>', '\n', html)
        text = re.sub(r'<p[^>]*>', '\n', text)
        text = re.sub(r'</p>', '', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    async def _mark_as_read(self, msg_num: bytes) -> None:
        """Mark a message as read."""
        try:
            if self._use_async:
                await self._imap.store(msg_num, '+FLAGS', '\\Seen')
            else:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._imap.store(msg_num, '+FLAGS', '\\Seen')
                )
        except Exception as e:
            logger.warning(f"Failed to mark message as read: {e}")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send an email message."""
        try:
            # Get thread info
            thread = self._threads.get(chat_id)

            # Determine recipient
            if thread and thread.participants:
                to_address = thread.participants[0]
            else:
                # chat_id might be the email address
                to_address = chat_id if '@' in chat_id else None

            if not to_address:
                return SendResult(success=False, error="No recipient address")

            # Build email
            if media or self.email_config.html_emails:
                msg = email.mime.multipart.MIMEMultipart('mixed')
            else:
                msg = email.mime.text.MIMEText(text)

            # Headers
            from_addr = formataddr((
                self.email_config.display_name or '',
                self.email_config.email_address
            ))
            msg['From'] = from_addr
            msg['To'] = to_address

            # Subject
            if thread:
                subject = thread.subject
                if not subject.lower().startswith('re:'):
                    subject = f"Re: {subject}"
            else:
                subject = self.email_config.default_subject

            msg['Subject'] = subject

            # Threading headers
            msg['Message-ID'] = email.utils.make_msgid()

            if reply_to:
                msg['In-Reply-To'] = reply_to

            if thread:
                if thread.references:
                    refs = ' '.join(thread.references)
                    if thread.last_message_id and thread.last_message_id not in refs:
                        refs += f" {thread.last_message_id}"
                    msg['References'] = refs
                elif thread.last_message_id:
                    msg['References'] = thread.last_message_id

            # Reply-To header
            if self.email_config.reply_to:
                msg['Reply-To'] = self.email_config.reply_to

            # Add body
            if isinstance(msg, email.mime.multipart.MIMEMultipart):
                # Add text/html parts
                alt = email.mime.multipart.MIMEMultipart('alternative')

                # Plain text
                text_part = email.mime.text.MIMEText(text, 'plain', 'utf-8')
                alt.attach(text_part)

                # HTML if enabled
                if self.email_config.html_emails:
                    html_text = self._text_to_html(text)
                    html_part = email.mime.text.MIMEText(html_text, 'html', 'utf-8')
                    alt.attach(html_part)

                msg.attach(alt)

                # Add attachments
                if media:
                    for m in media:
                        attachment = await self._create_attachment(m)
                        if attachment:
                            msg.attach(attachment)

            # Send
            return await self._send_smtp(to_address, msg)

        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return SendResult(success=False, error=str(e))

    def _text_to_html(self, text: str) -> str:
        """Convert plain text to HTML."""
        # Escape HTML entities
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')

        # Convert newlines to <br>
        text = text.replace('\n', '<br>\n')

        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
</head>
<body>
<div style="font-family: Arial, sans-serif; font-size: 14px;">
{text}
</div>
</body>
</html>"""

    async def _create_attachment(
        self,
        media: MediaAttachment,
    ) -> Optional[email.mime.base.MIMEBase]:
        """Create email attachment from MediaAttachment."""
        try:
            # Get file data
            if media.file_path:
                with open(media.file_path, 'rb') as f:
                    data = f.read()
                filename = os.path.basename(media.file_path)
            elif media.url:
                # Would need to fetch URL - skip for now
                logger.warning("URL attachments not implemented")
                return None
            else:
                return None

            # Check size
            if len(data) > self.email_config.max_attachment_size:
                logger.warning(f"Attachment too large: {len(data)} bytes")
                return None

            # Determine MIME type
            mime_type = media.mime_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            maintype, subtype = mime_type.split('/', 1)

            # Create attachment
            if maintype == 'text':
                part = email.mime.text.MIMEText(data.decode('utf-8', errors='replace'), _subtype=subtype)
            elif maintype == 'image':
                part = email.mime.image.MIMEImage(data, _subtype=subtype)
            elif maintype == 'audio':
                part = email.mime.audio.MIMEAudio(data, _subtype=subtype)
            else:
                part = email.mime.base.MIMEBase(maintype, subtype)
                part.set_payload(data)
                email.encoders.encode_base64(part)

            # Set filename
            part.add_header(
                'Content-Disposition',
                'attachment',
                filename=media.file_name or filename
            )

            return part

        except Exception as e:
            logger.error(f"Error creating attachment: {e}")
            return None

    async def _send_smtp(
        self,
        to_address: str,
        msg: email.message.Message,
    ) -> SendResult:
        """Send email via SMTP.

        ONE OF TWO SENDERS, AND DELIBERATELY NOT SHARED (audited 2026-09-22).
        The other is `channels/email_campaign._run_sends`, which is BULK: it holds
        one connection open for a chunk of addresses, paces between them, keeps a
        dated log so a daily cap survives a restart, halts on consecutive failures
        to protect sender reputation, and takes a PID lock because two concurrent
        runs double-send to the same list. None of that belongs on a single
        conversational reply, and a shared helper that opens one connection per
        message would throw away the batching the bulk path needs. Both call
        smtplib; that is not the same as being the same sender.

        The async/sync pair below is ONE sender with two transports (aiosmtplib
        when the loop can carry it, smtplib in an executor otherwise), not
        duplication. `_test_smtp` above has the same pair for the same reason and
        sends NOTHING -- it connects, logs in and quits.
        """
        try:
            host = self._get_smtp_host()
            port = self.email_config.smtp_port

            if self._use_async:
                # Async SMTP
                smtp = aiosmtplib.SMTP(
                    hostname=host,
                    port=port,
                    use_tls=not self.email_config.smtp_use_tls,
                    start_tls=self.email_config.smtp_use_tls,
                )
                await smtp.connect()
                await smtp.login(
                    self.email_config.smtp_username,
                    self.email_config.smtp_password
                )
                await smtp.send_message(msg)
                await smtp.quit()
            else:
                # Sync SMTP
                loop = asyncio.get_event_loop()

                def send_sync():
                    if self.email_config.smtp_use_tls:
                        smtp = smtplib.SMTP(host, port)
                        smtp.starttls()
                    else:
                        context = ssl.create_default_context()
                        smtp = smtplib.SMTP_SSL(host, port, context=context)

                    smtp.login(
                        self.email_config.smtp_username,
                        self.email_config.smtp_password
                    )
                    smtp.send_message(msg)
                    smtp.quit()

                await loop.run_in_executor(None, send_sync)

            return SendResult(
                success=True,
                message_id=msg['Message-ID'],
            )

        except Exception as e:
            logger.error(f"SMTP send error: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Email doesn't support message editing."""
        logger.warning("Email doesn't support message editing, sending new message")
        return await self.send_message(chat_id, text, reply_to=message_id)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """
        Delete an email message.
        Note: This marks the message as deleted but doesn't expunge.
        """
        logger.warning("Email deletion not fully implemented")
        return False

    async def send_typing(self, chat_id: str) -> None:
        """Email doesn't have typing indicators."""
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get thread information."""
        thread = self._threads.get(chat_id)
        if thread:
            return {
                'thread_id': thread.thread_id,
                'subject': thread.subject,
                'participants': thread.participants,
                'message_count': len(thread.message_ids),
            }
        return None

    # Email-specific methods

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[str]] = None,
        html_body: Optional[str] = None,
    ) -> SendResult:
        """Send an email with full control over headers."""
        try:
            # Create message
            if attachments or html_body:
                msg = email.mime.multipart.MIMEMultipart('mixed')
            else:
                msg = email.mime.text.MIMEText(body)

            # Headers
            from_addr = formataddr((
                self.email_config.display_name or '',
                self.email_config.email_address
            ))
            msg['From'] = from_addr
            msg['To'] = to
            msg['Subject'] = subject
            msg['Message-ID'] = email.utils.make_msgid()

            if cc:
                msg['Cc'] = ', '.join(cc)

            if self.email_config.reply_to:
                msg['Reply-To'] = self.email_config.reply_to

            # Body
            if isinstance(msg, email.mime.multipart.MIMEMultipart):
                alt = email.mime.multipart.MIMEMultipart('alternative')

                text_part = email.mime.text.MIMEText(body, 'plain', 'utf-8')
                alt.attach(text_part)

                if html_body:
                    html_part = email.mime.text.MIMEText(html_body, 'html', 'utf-8')
                    alt.attach(html_part)

                msg.attach(alt)

                # Attachments
                if attachments:
                    for filepath in attachments:
                        media = MediaAttachment(
                            type=MessageType.DOCUMENT,
                            file_path=filepath,
                            file_name=os.path.basename(filepath),
                        )
                        part = await self._create_attachment(media)
                        if part:
                            msg.attach(part)

            # Determine all recipients
            all_recipients = [to]
            if cc:
                all_recipients.extend(cc)
            if bcc:
                all_recipients.extend(bcc)

            # Send
            return await self._send_smtp(to, msg)

        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return SendResult(success=False, error=str(e))

    def get_thread(self, thread_id: str) -> Optional[EmailThread]:
        """Get thread information by ID."""
        return self._threads.get(thread_id)

    def list_threads(self) -> List[EmailThread]:
        """List all tracked threads."""
        return list(self._threads.values())


#: Mail providers by the end of their MX host: (IMAP server, SMTP server),
#: both reached on EmailConfig's ports (IMAP 993 over SSL, SMTP 587 with
#: STARTTLS).  provider_servers reads it.  Only providers whose servers a
#: password logs in to, and whose servers the MX alone names:
#:   * not Microsoft 365 (MX *.protection.outlook.com): Basic authentication
#:     (a password) is disabled for IMAP in every Exchange Online tenant
#:     (learn.microsoft.com, "Deprecation of Basic authentication in
#:     Exchange Online"), so a password card for it could never connect;
#:   * not Zoho by MX: a custom domain at Zoho logs in at imappro.zoho.com
#:     on a paid plan and at imap.zoho.com on the free one (zoho.com/mail/
#:     help/imap-access.html), and the MX (mx.zoho.com) does not say which;
#:     a wrong guess would read as a refused password.  zoho.in and zoho.eu
#:     name their servers in each account's settings only.
#: A provider's servers are written once (_GMAIL_SERVERS: Gmail and Google
#: Workspace, by MX or by address).
_GMAIL_SERVERS = ('imap.gmail.com', 'smtp.gmail.com')
_PROVIDER_SERVERS = (
    ('google.com', _GMAIL_SERVERS),
    ('googlemail.com', _GMAIL_SERVERS),
    ('ionos.com', ('imap.ionos.com', 'smtp.ionos.com')),
    ('ionos.co.uk', ('imap.ionos.co.uk', 'smtp.ionos.co.uk')),
    ('ionos.de', ('imap.ionos.de', 'smtp.ionos.de')),
    ('secureserver.net', ('imap.secureserver.net', 'smtpout.secureserver.net')),
    ('hostinger.com', ('imap.hostinger.com', 'smtp.hostinger.com')),
)

#: Mail domains that name their provider's servers by themselves, read
#: before any DNS lookup: the address alone is enough, on a node with no
#: MX resolver too (mailing_list.mx_lookup needs dnspython or dig).  A Zoho
#: address of Zoho's own domains is a personal or free account
#: (imap.zoho.com, zoho.com/mail/help/imap-access.html).
_PROVIDER_DOMAINS = {
    'gmail.com': _GMAIL_SERVERS,
    'googlemail.com': _GMAIL_SERVERS,
    'zoho.com': ('imap.zoho.com', 'smtp.zoho.com'),
    'zohomail.com': ('imap.zoho.com', 'smtp.zoho.com'),
}

#: Which password a provider's servers want, when it is not the account's
#: sign-in password: the one wording, on the password card
#: (marketing_tools._connect_or_ask) and in a refused login's reply
#: (channels.agent_tools._account_refusal), both through password_hint.
_PASSWORD_HINTS = {
    _GMAIL_SERVERS[0]: (
        "For a Gmail or Google Workspace address, enter an App Password "
        "(Google Account, Security, 2-Step Verification, App passwords), "
        "not the password you sign in with: Google refuses that one here."),
}


def provider_servers(address) -> Tuple[Optional[Dict[str, str]], str]:
    """``(servers, why)``: the IMAP and SMTP servers of the provider that
    hosts ``address``'s mail, as the email channel's settings
    ({'imap_host', 'smtp_host'}), and '' -- or None, and why they are not
    known, in words for the owner: the domain's mail host is not one of the
    providers whose servers are known, or no mail server of the domain was
    found and why (mailing_list.mx_lookup: it has no MX record, the lookup
    failed, or no lookup can run on this node).  Read from the address's
    domain first (_PROVIDER_DOMAINS: no network), then from the domain's MX
    (_PROVIDER_SERVERS), so a mailbox whose servers are known needs only its
    password from the owner (marketing_tools._connect_or_ask), and one whose
    servers are not says why the setup form is needed
    (outreach_crm_tools._connect_goal_mailbox).  Servers that are not known
    are logged once, with the address's local part masked (review of
    d072f8ea6: a failed lookup was silent)."""
    domain = str(address or '').strip().lower().rpartition('@')[2]
    if not domain:
        return None, 'the address names no domain'
    pair = _PROVIDER_DOMAINS.get(domain)
    if pair is None:
        from ..mailing_list import mx_lookup
        mx, cause = mx_lookup(domain)
        mx = (mx or '').lower().rstrip('.')
        pair = next((servers for suffix, servers in _PROVIDER_SERVERS
                     if mx and (mx == suffix or mx.endswith('.' + suffix))),
                    None)
        if pair is None:
            why = (f"the mail host of {domain} ({mx}) is not one of the "
                   f"providers whose servers HARTOS knows" if mx else
                   f"no mail server of {domain} was found ({cause})")
            logger.warning("Mail servers of ***@%s are not known: %s",
                           domain, why)
            return None, why
    return {'imap_host': pair[0], 'smtp_host': pair[1]}, ''


def password_hint(servers) -> str:
    """What to tell the owner about which password to enter for a mailbox on
    ``servers`` (a provider_servers result, or a mailbox's settings: its
    'imap_host' is read); '' when there is nothing to say."""
    return _PASSWORD_HINTS.get(str((servers or {}).get('imap_host') or ''), '')


def create_email_adapter(
    imap_host: str = None,
    smtp_host: str = None,
    email_address: str = None,
    password: str = None,
    **kwargs
) -> EmailAdapter:
    """
    Factory function to create Email adapter.

    For simple setup, provide same credentials for IMAP and SMTP.

    Args:
        imap_host: IMAP server host (or set EMAIL_IMAP_HOST env var)
        smtp_host: SMTP server host (or set EMAIL_SMTP_HOST env var)
        email_address: Email address (or set EMAIL_ADDRESS env var)
        password: Email password (or set EMAIL_PASSWORD env var)
        **kwargs: Additional config options

    Returns:
        Configured EmailAdapter
    """
    imap_host = imap_host or os.getenv("EMAIL_IMAP_HOST")
    smtp_host = smtp_host or os.getenv("EMAIL_SMTP_HOST")
    email_address = email_address or os.getenv("EMAIL_ADDRESS")
    password = password or os.getenv("EMAIL_PASSWORD")

    if not imap_host or not smtp_host:
        raise ValueError("IMAP and SMTP hosts required")
    if not email_address:
        raise ValueError("Email address required")

    # Use same credentials for IMAP and SMTP if not specified separately
    imap_username = kwargs.pop('imap_username', None) or email_address
    imap_password = kwargs.pop('imap_password', None) or password
    smtp_username = kwargs.pop('smtp_username', None) or email_address
    smtp_password = kwargs.pop('smtp_password', None) or password

    config = EmailConfig(
        imap_host=imap_host,
        smtp_host=smtp_host,
        email_address=email_address,
        imap_username=imap_username,
        imap_password=imap_password,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        **kwargs
    )
    return EmailAdapter(config)


# The credentials this module reads from the environment.  A value the
# owner stored in the vault is delivered there for these names
# (hartos.ai_key_vault.reads_from_env); tests/unit/
# test_env_secrets_declared.py fails on a secret read not declared.
ENV_SECRETS = (
    'EMAIL_PASSWORD',
)

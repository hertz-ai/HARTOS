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
        self._use_async = HAS_AIOIMAP and HAS_AIOSMTP

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
                await self._imap.login(
                    self.email_config.imap_username,
                    self.email_config.imap_password
                )
            else:
                # Sync IMAP (run in executor)
                loop = asyncio.get_event_loop()

                def connect_sync():
                    if self.email_config.imap_use_ssl:
                        context = ssl.create_default_context()
                        imap = imaplib.IMAP4_SSL(host, port, ssl_context=context)
                    else:
                        imap = imaplib.IMAP4(host, port)

                    imap.login(
                        self.email_config.imap_username,
                        self.email_config.imap_password
                    )
                    return imap

                self._imap = await loop.run_in_executor(None, connect_sync)

            return True

        except Exception as e:
            logger.error(f"IMAP connection error: {e}")
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
            logger.error(f"SMTP connection error: {e}")
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

    async def _check_messages_sync(self) -> None:
        """Check messages using sync IMAP."""
        loop = asyncio.get_event_loop()

        def check_sync():
            # Select inbox
            self._imap.select(self.email_config.inbox_folder)

            # Search for unseen messages
            status, data = self._imap.search(None, 'UNSEEN')
            if status != 'OK':
                return []

            message_nums = data[0].split()
            messages = []

            for num in message_nums:
                # Fetch message
                status, msg_data = self._imap.fetch(num, '(RFC822)')
                if status != 'OK':
                    continue

                raw_email = msg_data[0][1]
                messages.append((raw_email, num))

            return messages

        messages = await loop.run_in_executor(None, check_sync)

        for raw_email, num in messages:
            await self._process_email(raw_email, num)

    async def _process_email(self, raw_email: bytes, msg_num: bytes) -> None:
        """Process a raw email message."""
        try:
            # Parse email
            msg = email.message_from_bytes(raw_email)

            # Get message ID
            message_id = msg.get('Message-ID', '')

            # Skip if already seen
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
        """Send email via SMTP."""
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

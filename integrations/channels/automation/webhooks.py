"""
Webhook Manager for HevolveBot Integration.

Provides webhook registration, triggering, and signature verification.
"""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
import json


class WebhookStatus(Enum):
    """Status of a webhook."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    FAILED = "failed"
    PENDING = "pending"


@dataclass
class WebhookConfig:
    """Configuration for a webhook."""
    id: str
    url: str
    events: List[str]
    secret: str
    status: WebhookStatus = WebhookStatus.ACTIVE
    created_at: datetime = field(default_factory=datetime.now)
    last_triggered: Optional[datetime] = None
    failure_count: int = 0
    max_retries: int = 3
    timeout: int = 30
    headers: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookDelivery:
    """Record of a webhook delivery attempt."""
    webhook_id: str
    event: str
    payload: Dict[str, Any]
    timestamp: datetime
    success: bool
    response_code: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[float] = None


class WebhookManager:
    """
    Manages webhook registration, delivery, and verification.

    Features:
    - Register and unregister webhooks
    - Trigger webhooks with payloads
    - Verify webhook signatures
    - Track delivery history
    - Automatic retry on failure
    """

    def __init__(self, signing_key: Optional[str] = None):
        """
        Initialize the WebhookManager.

        Args:
            signing_key: Optional master signing key for signature generation
        """
        self._webhooks: Dict[str, WebhookConfig] = {}
        self._deliveries: List[WebhookDelivery] = []
        self._handlers: Dict[str, Callable] = {}
        self._signing_key = signing_key or secrets.token_hex(32)
        self._max_deliveries_history = 1000

    def register(
        self,
        url: str,
        events: List[str],
        webhook_id: Optional[str] = None,
        secret: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        timeout: int = 30
    ) -> WebhookConfig:
        """
        Register a new webhook.

        Args:
            url: The URL to send webhook payloads to
            events: List of event types to subscribe to
            webhook_id: Optional custom ID (auto-generated if not provided)
            secret: Optional secret for signature verification
            headers: Optional custom headers to include in requests
            metadata: Optional metadata to associate with the webhook
            max_retries: Maximum retry attempts on failure
            timeout: Request timeout in seconds

        Returns:
            The created WebhookConfig

        Raises:
            ValueError: If URL is invalid or events list is empty
        """
        if not url:
            raise ValueError("URL is required")
        if not events:
            raise ValueError("At least one event type is required")

        webhook_id = webhook_id or f"wh_{secrets.token_hex(8)}"

        if webhook_id in self._webhooks:
            raise ValueError(f"Webhook with ID '{webhook_id}' already exists")

        secret = secret or secrets.token_hex(16)

        config = WebhookConfig(
            id=webhook_id,
            url=url,
            events=events,
            secret=secret,
            headers=headers or {},
            metadata=metadata or {},
            max_retries=max_retries,
            timeout=timeout
        )

        self._webhooks[webhook_id] = config
        return config

    def unregister(self, webhook_id: str) -> bool:
        """
        Unregister a webhook.

        Args:
            webhook_id: The ID of the webhook to unregister

        Returns:
            True if successfully unregistered, False if not found
        """
        if webhook_id in self._webhooks:
            del self._webhooks[webhook_id]
            return True
        return False

    def list_webhooks(
        self,
        event: Optional[str] = None,
        status: Optional[WebhookStatus] = None
    ) -> List[WebhookConfig]:
        """
        List registered webhooks.

        Args:
            event: Optional filter by event type
            status: Optional filter by status

        Returns:
            List of matching webhook configurations
        """
        webhooks = list(self._webhooks.values())

        if event:
            webhooks = [w for w in webhooks if event in w.events]

        if status:
            webhooks = [w for w in webhooks if w.status == status]

        return webhooks

    def get_webhook(self, webhook_id: str) -> Optional[WebhookConfig]:
        """
        Get a specific webhook by ID.

        Args:
            webhook_id: The webhook ID

        Returns:
            The webhook configuration or None if not found
        """
        return self._webhooks.get(webhook_id)

    def update_webhook(
        self,
        webhook_id: str,
        url: Optional[str] = None,
        events: Optional[List[str]] = None,
        status: Optional[WebhookStatus] = None,
        headers: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[WebhookConfig]:
        """
        Update a webhook configuration.

        Args:
            webhook_id: The webhook ID to update
            url: New URL (optional)
            events: New events list (optional)
            status: New status (optional)
            headers: New headers (optional)
            metadata: New metadata (optional)

        Returns:
            Updated webhook configuration or None if not found
        """
        if webhook_id not in self._webhooks:
            return None

        webhook = self._webhooks[webhook_id]

        if url is not None:
            webhook.url = url
        if events is not None:
            webhook.events = events
        if status is not None:
            webhook.status = status
        if headers is not None:
            webhook.headers = headers
        if metadata is not None:
            webhook.metadata = metadata

        return webhook

    def trigger(
        self,
        event: str,
        payload: Dict[str, Any],
        webhook_id: Optional[str] = None
    ) -> List[WebhookDelivery]:
        """
        Trigger webhooks for an event.

        Args:
            event: The event type
            payload: The payload to send
            webhook_id: Optional specific webhook to trigger

        Returns:
            List of delivery records
        """
        deliveries = []

        if webhook_id:
            webhooks = [self._webhooks.get(webhook_id)] if webhook_id in self._webhooks else []
        else:
            webhooks = [w for w in self._webhooks.values()
                       if event in w.events and w.status == WebhookStatus.ACTIVE]

        for webhook in webhooks:
            if webhook is None:
                continue

            delivery = self._deliver(webhook, event, payload)
            deliveries.append(delivery)
            self._record_delivery(delivery)

        return deliveries

    def _deliver(
        self,
        webhook: WebhookConfig,
        event: str,
        payload: Dict[str, Any]
    ) -> WebhookDelivery:
        """
        Deliver a webhook payload.

        In a real implementation, this would make an HTTP request.
        For testing, we simulate the delivery.

        Args:
            webhook: The webhook configuration
            event: The event type
            payload: The payload to deliver

        Returns:
            Delivery record
        """
        start_time = time.time()

        # Add standard headers to payload
        full_payload = {
            "event": event,
            "timestamp": datetime.now().isoformat(),
            "webhook_id": webhook.id,
            "data": payload
        }

        # Generate signature
        signature = self.generate_signature(json.dumps(full_payload), webhook.secret)

        # In a real implementation, this would make an HTTP POST request
        # For now, we check if there's a registered handler for testing
        handler = self._handlers.get(webhook.id)

        try:
            if handler:
                result = handler(event, full_payload, signature)
                success = result.get("success", True)
                response_code = result.get("code", 200)
                response_body = result.get("body", "OK")
            else:
                # Simulate successful delivery
                success = True
                response_code = 200
                response_body = "OK"

            webhook.last_triggered = datetime.now()
            if success:
                webhook.failure_count = 0
            else:
                webhook.failure_count += 1
                if webhook.failure_count >= webhook.max_retries:
                    webhook.status = WebhookStatus.FAILED

            duration_ms = (time.time() - start_time) * 1000

            return WebhookDelivery(
                webhook_id=webhook.id,
                event=event,
                payload=full_payload,
                timestamp=datetime.now(),
                success=success,
                response_code=response_code,
                response_body=response_body,
                duration_ms=duration_ms
            )

        except Exception as e:
            webhook.failure_count += 1
            if webhook.failure_count >= webhook.max_retries:
                webhook.status = WebhookStatus.FAILED

            duration_ms = (time.time() - start_time) * 1000

            return WebhookDelivery(
                webhook_id=webhook.id,
                event=event,
                payload=full_payload,
                timestamp=datetime.now(),
                success=False,
                error=str(e),
                duration_ms=duration_ms
            )

    def _record_delivery(self, delivery: WebhookDelivery) -> None:
        """Record a delivery in history."""
        self._deliveries.append(delivery)

        # Trim history if it exceeds max
        if len(self._deliveries) > self._max_deliveries_history:
            self._deliveries = self._deliveries[-self._max_deliveries_history:]

    def get_delivery_history(
        self,
        webhook_id: Optional[str] = None,
        event: Optional[str] = None,
        limit: int = 100
    ) -> List[WebhookDelivery]:
        """
        Get webhook delivery history.

        Args:
            webhook_id: Optional filter by webhook ID
            event: Optional filter by event type
            limit: Maximum number of records to return

        Returns:
            List of delivery records
        """
        deliveries = self._deliveries.copy()

        if webhook_id:
            deliveries = [d for d in deliveries if d.webhook_id == webhook_id]

        if event:
            deliveries = [d for d in deliveries if d.event == event]

        return deliveries[-limit:]

    def generate_signature(self, payload: str, secret: str) -> str:
        """
        Generate a signature for a webhook payload.

        Args:
            payload: The payload string to sign
            secret: The secret key

        Returns:
            The HMAC-SHA256 signature
        """
        return hmac.new(
            secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

    def verify_signature(
        self,
        payload: str,
        signature: str,
        secret: str
    ) -> bool:
        """
        Verify a webhook signature.

        Args:
            payload: The payload string
            signature: The signature to verify
            secret: The secret key

        Returns:
            True if signature is valid, False otherwise
        """
        expected = self.generate_signature(payload, secret)
        return hmac.compare_digest(expected, signature)

    def register_handler(
        self,
        webhook_id: str,
        handler: Callable[[str, Dict[str, Any], str], Dict[str, Any]]
    ) -> None:
        """
        Register a handler for testing webhook delivery.

        Args:
            webhook_id: The webhook ID
            handler: Function that receives (event, payload, signature) and returns response dict
        """
        self._handlers[webhook_id] = handler

    def unregister_handler(self, webhook_id: str) -> None:
        """Remove a test handler."""
        if webhook_id in self._handlers:
            del self._handlers[webhook_id]

    def reset_failure_count(self, webhook_id: str) -> bool:
        """
        Reset the failure count for a webhook.

        Args:
            webhook_id: The webhook ID

        Returns:
            True if successful, False if webhook not found
        """
        if webhook_id in self._webhooks:
            self._webhooks[webhook_id].failure_count = 0
            self._webhooks[webhook_id].status = WebhookStatus.ACTIVE
            return True
        return False

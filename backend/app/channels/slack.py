"""Slack notifications for pending approvals.

Posts to a Slack incoming webhook (https://api.slack.com/messaging/webhooks) with a Block Kit
message, so the approvals channel shows the amount, the order and the reason, and links back
to the queue. Without LOOPDESK_SLACK_WEBHOOK_URL the message is only recorded in the trace
(dry run), so the demo never calls out to the internet.
"""

from __future__ import annotations

import httpx

from ..config import Settings
from ..models import Approval, Tenant


def approval_message(approval: Approval, tenant: Tenant) -> dict:
    args = approval.arguments or {}
    amount = float(args.get("amount") or 0)
    return {
        "text": f"[{tenant.name}] Refund approval needed: {approval.tool_name} ({approval.id})",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Refund approval needed"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Tenant*\n{tenant.name}"},
                {"type": "mrkdwn", "text": f"*Order*\n{args.get('order_id', '-')}"},
                {"type": "mrkdwn", "text": f"*Amount*\n${amount:,.2f} AUD"},
                {"type": "mrkdwn", "text": f"*Approval*\n#{approval.id}"},
            ]},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Reason*\n{approval.reason}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": "Approve or reject in LoopDesk → Approvals. "
                                           "The agent's evidence pack is attached to the request."},
            ]},
        ],
    }


def notify_approval(settings: Settings, approval: Approval, tenant: Tenant,
                    transport: httpx.BaseTransport | None = None) -> dict:
    message = approval_message(approval, tenant)
    if not settings.slack_webhook_url:
        return {"sent": False, "mode": "dry_run", "content": message["text"]}
    try:
        with httpx.Client(timeout=5.0, transport=transport) as client:
            response = client.post(settings.slack_webhook_url, json=message)
        return {"sent": response.status_code == 200, "mode": "live", "status": response.status_code}
    except (httpx.HTTPError, ValueError) as exc:
        return {"sent": False, "mode": "live", "error": str(exc)}

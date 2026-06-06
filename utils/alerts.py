"""
AlertManager — handles notification dispatch and alert escalation.

Supported channels:
  - Email (Flask-Mail)
  - WhatsApp (Twilio)
  - SMS (Twilio)

Add TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM,
and TWILIO_SMS_FROM to your .env to enable Twilio channels.
"""

from datetime import datetime
from flask_mail import Message


class AlertManager:
    def __init__(self, app, mail):
        self.app = app
        self.mail = mail

    # ------------------------------------------------------------------
    # Public: send a single alert to a single user via all enabled channels
    # ------------------------------------------------------------------
    def send_alert(self, alert, user, item=None):
        """
        Send an alert to a user.
        Respects AlertSetting flags (send_email, send_whatsapp, send_sms).
        Falls back to email-only when no setting is found.
        """
        from models import AlertSetting, InventoryItem

        if item is None and alert.item_id:
            item = InventoryItem.query.get(alert.item_id)

        setting = None
        if item:
            setting = AlertSetting.query.filter_by(
                company_id=item.company_id,
                alert_type=alert.alert_type
            ).first()

        send_email = setting.send_email if setting else True
        send_whatsapp = setting.send_whatsapp if setting else False
        send_sms = setting.send_sms if setting else False

        if send_email and user.email:
            self._email(alert, user, item)
        if send_whatsapp and user.phone:
            self._whatsapp(alert, user, item)
        if send_sms and user.phone:
            self._sms(alert, user, item)

    # ------------------------------------------------------------------
    # Public: escalate alerts that are past their escalation threshold
    # ------------------------------------------------------------------
    def check_and_escalate(self):
        """
        Called by the scheduler hourly.
        Escalates any open alert that has been unresolved beyond
        the configured escalation_days threshold.
        """
        from models import db, Alert, AlertSetting, User

        settings = {s.alert_type: s for s in AlertSetting.query.all()}
        open_alerts = Alert.query.filter_by(resolved_at=None, is_escalated=False).all()

        for alert in open_alerts:
            setting = settings.get(alert.alert_type)
            if not setting or not setting.escalation_days:
                continue
            age_days = (datetime.utcnow() - alert.triggered_at).days
            if age_days < setting.escalation_days:
                continue

            alert.is_escalated = True
            alert.escalation_level = min(alert.escalation_level + 1, 2)

            if alert.item and alert.item.company_id:
                managers = User.query.filter(
                    User.company_id == alert.item.company_id,
                    User.role.in_(['admin', 'manager']),
                    User.is_active == True
                ).all()
                for mgr in managers:
                    self._escalation_email(alert, mgr, age_days)

        db.session.commit()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    def _email(self, alert, user, item):
        try:
            item_name = item.name if item else 'Unknown asset'
            asset_code = item.asset_code if item else 'N/A'
            location = item.location if item else 'N/A'

            subject = (
                f"[Inventory Alert] "
                f"{alert.alert_type.replace('_', ' ').title()} — {item_name}"
            )
            body = (
                f"Dear {user.username},\n\n"
                f"This is an automated alert from your House Inventory System.\n\n"
                f"Alert type : {alert.alert_type.replace('_', ' ').title()}\n"
                f"Message    : {alert.message}\n"
                f"Asset      : {item_name} (Code: {asset_code})\n"
                f"Location   : {location}\n"
                f"Triggered  : {alert.triggered_at.strftime('%d %b %Y %H:%M')}\n\n"
                f"Please log in to review and resolve this alert.\n\n"
                f"— Inventory System"
            )
            msg = Message(subject=subject, recipients=[user.email], body=body)
            self.mail.send(msg)
        except Exception as e:
            print(f"[AlertManager] Email error: {e}")

    def _whatsapp(self, alert, user, item):
        try:
            from twilio.rest import Client
            sid = self.app.config.get('TWILIO_ACCOUNT_SID')
            token = self.app.config.get('TWILIO_AUTH_TOKEN')
            from_number = self.app.config.get('TWILIO_WHATSAPP_FROM')

            if not all([sid, token, from_number]):
                print("[AlertManager] WhatsApp: Twilio credentials not set in .env")
                return

            item_name = item.name if item else 'Unknown asset'
            asset_code = item.asset_code if item else 'N/A'

            body = (
                f"*Inventory Alert*\n"
                f"Type: {alert.alert_type.replace('_', ' ').title()}\n"
                f"{alert.message}\n"
                f"Asset: {item_name} ({asset_code})\n"
                f"Location: {item.location if item else 'N/A'}"
            )
            Client(sid, token).messages.create(
                from_=from_number,
                to=f"whatsapp:{user.phone}",
                body=body
            )
        except ImportError:
            print("[AlertManager] Twilio not installed: pip install twilio")
        except Exception as e:
            print(f"[AlertManager] WhatsApp error: {e}")

    def _sms(self, alert, user, item):
        try:
            from twilio.rest import Client
            sid = self.app.config.get('TWILIO_ACCOUNT_SID')
            token = self.app.config.get('TWILIO_AUTH_TOKEN')
            from_number = self.app.config.get('TWILIO_SMS_FROM')

            if not all([sid, token, from_number]):
                print("[AlertManager] SMS: Twilio credentials not set in .env")
                return

            item_name = item.name if item else 'Unknown asset'
            body = f"Inventory Alert: {alert.message} | Asset: {item_name}"
            Client(sid, token).messages.create(
                from_=from_number,
                to=user.phone,
                body=body
            )
        except ImportError:
            print("[AlertManager] Twilio not installed: pip install twilio")
        except Exception as e:
            print(f"[AlertManager] SMS error: {e}")

    def _escalation_email(self, alert, manager, age_days):
        try:
            item_name = alert.item.name if alert.item else 'Unknown asset'
            subject = (
                f"[ESCALATED] {alert.alert_type.replace('_', ' ').title()} "
                f"alert unresolved for {age_days} day(s)"
            )
            body = (
                f"Dear {manager.username},\n\n"
                f"The following alert has not been resolved in {age_days} days "
                f"and requires your immediate attention:\n\n"
                f"Alert type : {alert.alert_type.replace('_', ' ').title()}\n"
                f"Message    : {alert.message}\n"
                f"Asset      : {item_name}\n"
                f"Triggered  : {alert.triggered_at.strftime('%d %b %Y %H:%M')}\n\n"
                f"Please log in and take action immediately.\n\n"
                f"— Inventory System (Automated Escalation)"
            )
            msg = Message(subject=subject, recipients=[manager.email], body=body)
            self.mail.send(msg)
        except Exception as e:
            print(f"[AlertManager] Escalation email error: {e}")

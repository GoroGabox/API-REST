"""Backend de email que envía vía la API HTTP de Resend (puerto 443).

Evita los bloqueos de puertos SMTP salientes (25/465/587) comunes en PaaS como
Railway (plan trial). Se activa con:

    EMAIL_BACKEND=autotestAPI.email_backends.ResendEmailBackend
    RESEND_API_KEY=re_...

Es un drop-in del sistema de email de Django: `send_mail`, `EmailMessage`,
`mail_admins`, etc. siguen funcionando igual.
"""
import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

RESEND_API_URL = 'https://api.resend.com/emails'


class ResendEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        api_key = getattr(settings, 'RESEND_API_KEY', '')
        if not api_key:
            if not self.fail_silently:
                raise ValueError('RESEND_API_KEY no está configurada.')
            return 0

        headers = {'Authorization': f'Bearer {api_key}'}
        timeout = getattr(settings, 'EMAIL_TIMEOUT', 10) or 10
        sent = 0

        for message in email_messages:
            payload = {
                'from': message.from_email,
                'to': list(message.to),
                'subject': message.subject,
                'text': message.body,
            }
            if message.cc:
                payload['cc'] = list(message.cc)
            if message.bcc:
                payload['bcc'] = list(message.bcc)
            if message.reply_to:
                payload['reply_to'] = list(message.reply_to)
            # Adjunta la parte HTML si el mensaje la trae (EmailMultiAlternatives).
            for content, mimetype in getattr(message, 'alternatives', None) or []:
                if mimetype == 'text/html':
                    payload['html'] = content

            try:
                resp = requests.post(RESEND_API_URL, headers=headers, json=payload, timeout=timeout)
                resp.raise_for_status()
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise

        return sent

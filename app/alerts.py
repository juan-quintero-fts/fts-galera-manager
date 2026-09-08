from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .audit import log, update_node_alert_state
from .core import settings


def node_is_online(node):
    """Un nodo está disponible sólo si SSH y MariaDB están activos."""
    return bool(node.get('ssh')) and node.get('mariadb') == 'active'


def node_status_detail(node):
    return (
        f"SSH={'activo' if node.get('ssh') else 'inaccesible'}, "
        f"MariaDB={node.get('mariadb', 'N/A')}, "
        f"Cluster={node.get('cluster', 'N/A')}, "
        f"Ready={node.get('ready', 'N/A')}, "
        f"Estado local={node.get('local_state', 'N/A')}"
    )


def send_node_email(node, online):
    state = 'RECUPERADO' if online else 'CAÍDO'
    message = EmailMessage()
    message['Subject'] = f'[{settings.app_name}] Nodo {state}: {node["host"]}'
    message['From'] = settings.alert_email_from
    message['To'] = ', '.join(settings.alert_email_to)
    message.set_content(
        f"El nodo {node['host']} está {state.lower()}.\n\n"
        f"{node_status_detail(node)}\n\n"
        "Esta alerta es informativa: la aplicación no ejecutó ninguna acción correctiva."
    )

    if settings.smtp_ssl:
        client = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
    with client:
        if settings.smtp_starttls and not settings.smtp_ssl:
            client.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)


def notify_node_transitions(nodes):
    """Envía un único correo por cambio de estado; el primer sondeo sólo crea la línea base."""
    if not settings.email_alerts_enabled:
        return
    for node in nodes:
        online = node_is_online(node)
        previous = update_node_alert_state(node['host'], online)
        if previous is None or previous == online:
            continue
        action = 'alert:node-recovered' if online else 'alert:node-down'
        try:
            send_node_email(node, online)
            log('monitor', node['host'], action, True, node_status_detail(node))
        except Exception as exc:
            log('monitor', node['host'], action, False, f'No fue posible enviar el correo: {exc}')

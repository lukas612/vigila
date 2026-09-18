"""The welcome/conversion email series: one branch for someone who signed
up but never activated a plan, one for someone who just started paying.
Each email is idempotent per user (see LifecycleEmailLog) so re-running the
daily cron, or a retried webhook, never double-sends.

Two ways these fire:
  - immediate steps (the "_0" keys) are called straight from the event that
    triggers them (main._ensure_profile, billing.apply_event) — see the
    call sites there.
  - day-N steps are only ever called from scripts/send_lifecycle_emails.py,
    which checks elapsed time once a day; there's no event to react to for
    "it's been 5 days and still nothing happened".

payment_failed isn't part of the idempotent series — see
LifecycleEmailLog's docstring — it's sent directly with app.email.send_email
from billing.apply_event on each fresh transition into past_due.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from . import email
from .models import CheckResultEnum, LifecycleEmailKey, LifecycleEmailLog, MonitoredId, NotificationRun, Plan, User

logger = logging.getLogger("vigila")

CUENTA_URL = "https://vigilamultas.com/cuenta.html"
PLANES_URL = "https://vigilamultas.com/index.html#planes"


def _wrap(inner_html: str, user_email: str) -> str:
    return f"""
    <div style="font-family:-apple-system,sans-serif;max-width:520px;margin:0 auto;color:#182233;">
      {inner_html}
      <p style="font-size:12px;color:#8B93A6;margin-top:28px;">
        Recibes esto porque tienes una cuenta en VigilaMultas ({user_email}). Si prefieres no recibir más correos como este, respóndenos y te damos de baja a mano.
      </p>
    </div>
    """


def _button(url: str, label: str) -> str:
    return (
        f'<p style="margin-top:22px;">'
        f'<a href="{url}" style="background:#F5A623;color:#2A1B00;padding:12px 22px;'
        f'border-radius:8px;text-decoration:none;font-weight:700;display:inline-block;">{label}</a>'
        f"</p>"
    )


def _features(items: list[str]) -> str:
    lis = "".join(f'<li style="margin-bottom:6px;">{item}</li>' for item in items)
    return f'<ul style="padding-left:20px;margin:16px 0;color:#182233;">{lis}</ul>'


def _send_once(db: Session, user: User, key: LifecycleEmailKey, subject: str, inner_html: str) -> bool:
    """Sends `key` to `user` unless it's already gone out — returns whether
    an email was actually sent (not just attempted), so callers/the cron
    can count real sends."""
    already_sent = db.query(LifecycleEmailLog).filter_by(user_id=user.id, email_key=key).first()
    if already_sent:
        return False
    ok = email.send_email(user.email, subject, _wrap(inner_html, user.email))
    if not ok:
        return False
    db.add(LifecycleEmailLog(user_id=user.id, email_key=key))
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Serie A — se ha registrado pero no tiene plan activo
# ---------------------------------------------------------------------------


def send_welcome_no_plan_0(db: Session, user: User) -> bool:
    inner = f"""
        <h2 style="color:#1B4D8C;margin-bottom:4px;">Ya tienes cuenta en VigilaMultas — falta un paso</h2>
        <p>Hola,</p>
        <p>Ya tienes acceso a tu cuenta. Pero ojo: <b>todavía no estás vigilado</b>. Tener cuenta no revisa el BOE por ti — necesitas activar un plan.</p>
        {_features([
            "Revisamos el Tablón Edictal Único todos los días",
            "Te avisamos por email en cuanto aparezca algo a tu nombre",
            "No se te pasa el plazo de 20 días para alegar",
        ])}
        <p>Desde <b>3,99€/mes</b>. Sin permanencia, cancela cuando quieras.</p>
        {_button(CUENTA_URL, "Activar mi vigilancia")}
        """
    return _send_once(
        db, user, LifecycleEmailKey.welcome_no_plan_0,
        "Ya tienes cuenta en VigilaMultas — falta un paso",
        inner,
    )


def send_welcome_no_plan_2(db: Session, user: User) -> bool:
    inner = f"""
        <h2 style="color:#1B4D8C;margin-bottom:4px;">El BOE no avisa por correo. Nosotros sí.</h2>
        <p>Mucha gente no sabe que tiene una notificación pendiente hasta que es tarde: el BOE publica edictos que <b>se dan por notificados aunque no los veas</b>, y el plazo para alegar son solo 20 días naturales.</p>
        <p>Cada semana se publican miles de notificaciones de tráfico en el Tablón Edictal Único — la mayoría de gente ni sabe que existe.</p>
        <p>Nosotros lo comprobamos por ti dos veces al día. Tú solo tienes que activarte una vez.</p>
        <p style="font-size:13px;color:#57607A;">Sin permanencia. Cancelas cuando quieras, con un clic.</p>
        {_button(PLANES_URL, "Ver planes desde 3,99€/mes")}
        """
    return _send_once(
        db, user, LifecycleEmailKey.welcome_no_plan_2,
        "El BOE no avisa por correo. Nosotros sí.",
        inner,
    )


def send_welcome_no_plan_5(db: Session, user: User) -> bool:
    inner = f"""
        <h2 style="color:#1B4D8C;margin-bottom:4px;">Una multa de tráfico cuesta más que un año de VigilaMultas</h2>
        <p>Si se te pasa el plazo de alegación, la multa se da por firme — igual da que no la vieras. Una multa media ronda los 100-500€. Un año entero de VigilaMultas cuesta menos que eso.</p>
        <p>No te vendemos un seguro contra multas: te avisamos a tiempo para que <b>tú decidas</b> si alegar, pagar con descuento por pronto pago, o lo que corresponda. Pero para eso hay que enterarse a tiempo.</p>
        {_button(CUENTA_URL, "Activarme ahora")}
        <p style="font-size:13px;color:#57607A;">¿Dudas? Responde a este correo, te contestamos nosotros, no un bot.</p>
        """
    return _send_once(
        db, user, LifecycleEmailKey.welcome_no_plan_5,
        "Una multa de tráfico cuesta más que un año de VigilaMultas",
        inner,
    )


def send_welcome_no_plan_10(db: Session, user: User) -> bool:
    inner = f"""
        <h2 style="color:#1B4D8C;margin-bottom:4px;">Vamos a dejar de recordártelo — última vez</h2>
        <p>No te vamos a insistir más después de este correo. Tu cuenta sigue abierta, pero sin plan activo no estamos revisando nada por ti.</p>
        <p>Si es cuestión de precio: el plan Individual son <b>3,99€/mes, sin permanencia</b>. Si es por confianza: cancelas cuando quieras desde tu cuenta, sin llamadas ni letra pequeña.</p>
        {_button(CUENTA_URL, "Activar mi vigilancia")}
        """
    return _send_once(
        db, user, LifecycleEmailKey.welcome_no_plan_10,
        "Vamos a dejar de recordártelo — última vez",
        inner,
    )


# ---------------------------------------------------------------------------
# Serie B — ya paga (recién suscrito)
# ---------------------------------------------------------------------------


def send_welcome_paid_0(db: Session, user: User) -> bool:
    """Sent the moment the subscription first goes active. At this exact
    point the user can't have a target yet (main._max_targets requires an
    active plan before /api/targets accepts one), so this always guides
    them to add their first one rather than confirming one already exists."""
    plan_label = "Familiar" if user.plan == Plan.familiar else "Individual"
    inner = f"""
        <h2 style="color:#1B4D8C;margin-bottom:4px;">Tu plan {plan_label} ya está activo</h2>
        <p>¡Gracias por confiar en VigilaMultas! Solo falta un paso: <b>añade el DNI, NIE o matrícula</b> que quieres vigilar desde tu cuenta.</p>
        {_features([
            "En cuanto lo añadas, lo revisamos al momento y luego dos veces al día",
            "Si aparece algo, te avisamos por email de inmediato",
            "Si no aparece nada, no hacemos ruido — silencio es buena señal",
        ])}
        {_button(CUENTA_URL, "Añadir mi primera vigilancia")}
        <p style="font-size:13px;color:#57607A;">Sin permanencia · Gestiona o cancela cuando quieras desde tu cuenta.</p>
        """
    return _send_once(
        db, user, LifecycleEmailKey.welcome_paid_0,
        f"Tu plan {plan_label} ya está activo — añade tu primer DNI o matrícula",
        inner,
    )


def send_welcome_paid_3(db: Session, user: User) -> bool:
    """Cross-sell to Familiar — only makes sense for someone still on
    Individual; the cron re-checks this every day so an eventual plan
    switch within the window still gets a chance to see it."""
    if user.plan != Plan.individual:
        return False
    inner = f"""
        <h2 style="color:#1B4D8C;margin-bottom:4px;">¿Vigilamos también a tu pareja, tu hijo o tu furgoneta?</h2>
        <p>Tu plan Individual cubre un solo DNI, NIE o matrícula. Con el plan <b>Familiar (6,99€/mes)</b> puedes vigilar hasta 5 — ideal para pareja, hijos con carné reciente, o varios vehículos de la familia o del negocio.</p>
        <p>Solo 3€ más al mes por 4 vigilancias adicionales.</p>
        {_button(CUENTA_URL, "Cambiar a plan Familiar")}
        """
    return _send_once(
        db, user, LifecycleEmailKey.welcome_paid_3,
        "¿Vigilamos también a tu pareja, tu hijo o tu furgoneta?",
        inner,
    )


def _checks_run_for_user(db: Session, user: User) -> int:
    return (
        db.query(NotificationRun)
        .join(MonitoredId, NotificationRun.monitored_id == MonitoredId.id)
        .filter(MonitoredId.user_id == user.id)
        .count()
    )


def _any_match_found_for_user(db: Session, user: User) -> bool:
    return (
        db.query(NotificationRun)
        .join(MonitoredId, NotificationRun.monitored_id == MonitoredId.id)
        .filter(MonitoredId.user_id == user.id, NotificationRun.result == CheckResultEnum.found)
        .first()
        is not None
    )


def send_welcome_paid_30(db: Session, user: User) -> bool:
    checks = _checks_run_for_user(db, user)
    outcome = (
        "te avisamos de al menos una notificación nueva."
        if _any_match_found_for_user(db, user)
        else "sin novedades — sigues limpio."
    )
    checks_line = (
        f"hemos revisado el BOE <b>{checks}</b> {'vez' if checks == 1 else 'veces'} por ti"
        if checks
        else "hemos estado revisando el BOE por ti"
    )
    inner = f"""
        <h2 style="color:#1B4D8C;margin-bottom:4px;">Llevas un mes protegido</h2>
        <p>Un mes vigilando tus identificadores: {checks_line}, {outcome}</p>
        <p>Si te está siendo útil, nos ayuda mucho que lo compartas con alguien a quien le pueda pasar lo mismo — la mayoría de gente ni sabe que existe el Tablón Edictal Único.</p>
        {_button(CUENTA_URL, "Ver mi historial")}
        """
    return _send_once(
        db, user, LifecycleEmailKey.welcome_paid_30,
        "Llevas un mes protegido — esto es lo que hemos comprobado por ti",
        inner,
    )


# ---------------------------------------------------------------------------
# Pago fallido — no forma parte de la serie idempotente (ver docstring de
# LifecycleEmailLog); se llama directamente desde billing.apply_event.
# ---------------------------------------------------------------------------


def send_payment_failed(user: User) -> bool:
    inner = f"""
        <h2 style="color:#D93025;margin-bottom:4px;">⚠️ Hemos pausado tu vigilancia — pago fallido</h2>
        <p>No hemos podido cobrar tu suscripción. Mientras no se regularice, <b>no estamos revisando el BOE por ti</b> — y si hay algo pendiente, el plazo de 20 días sigue corriendo igual.</p>
        {_button(CUENTA_URL, "Actualizar método de pago")}
        """
    return email.send_email(user.email, "Hemos pausado tu vigilancia — pago fallido", _wrap(inner, user.email))

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckRequest(BaseModel):
    value: str = Field(..., min_length=5, max_length=20, description="DNI, NIE o matrícula")
    consent: bool = Field(..., description="Consentimiento explícito para procesar el dato")


class NotificationOut(BaseModel):
    boe_ref: str
    expediente: str
    localidad: str
    fecha: str
    matricula: str
    importe: str
    precepto: str
    articulo: str
    puntos: str
    fecha_publicacion: str | None = Field(None, description="Fecha de publicación del edicto, DD/MM/AAAA")
    plazo_alegacion_fin: str | None = Field(None, description="Último día del plazo de 20 días naturales, DD/MM/AAAA")
    dias_restantes: int | None = Field(
        None, description="Días naturales restantes hasta plazo_alegacion_fin (negativo si ya venció)"
    )


class CheckResponse(BaseModel):
    found: bool
    notifications: list[NotificationOut] = []


class WaitlistRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    context: str = Field("ok", description="'ok' o 'alert' — desde qué resultado se suscribe")


class LocalityStat(BaseModel):
    localidad: str
    expedientes_count: int
    importe_total: float | None = None


class DailyStatsResponse(BaseModel):
    stat_date: str = Field(..., description="Fecha de los datos, AAAA-MM-DD")
    total_expedientes: int
    total_importe: float
    total_con_dni: int = Field(0, description="Expedientes con un DNI/NIE identificado")
    total_con_matricula: int = Field(0, description="Expedientes con una matrícula identificada")
    localidades: list[LocalityStat]


class WeeklyStatsResponse(BaseModel):
    date_from: str = Field(..., description="Día más antiguo incluido en la ventana, AAAA-MM-DD")
    date_to: str = Field(..., description="Día más reciente incluido en la ventana, AAAA-MM-DD")
    days_included: list[str] = Field(..., description="Días con crawl que entran en la ventana, AAAA-MM-DD")
    total_expedientes: int
    total_importe: float
    total_con_dni: int = Field(0, description="Expedientes con un DNI/NIE identificado")
    total_con_matricula: int = Field(0, description="Expedientes con una matrícula identificada")
    localidades: list[LocalityStat]


class SessionRequest(BaseModel):
    """The tokens Supabase Auth's JS client receives after a magic-link
    login, handed to us once so we can wrap them in HttpOnly cookies."""

    access_token: str
    refresh_token: str
    expires_in: int = 3600


class MeResponse(BaseModel):
    email: str
    plan: str | None = None
    subscription_status: str = "none"
    max_targets: int = 0


class CheckoutRequest(BaseModel):
    plan: str = Field(..., description="'individual' o 'familiar'")


class CheckoutResponse(BaseModel):
    url: str


class PortalResponse(BaseModel):
    url: str


class CreateTargetRequest(BaseModel):
    value: str = Field(..., min_length=5, max_length=20, description="DNI, NIE o matrícula a vigilar")
    label: str | None = Field(None, max_length=120, description="Apodo opcional, p.ej. 'coche de Ana'")
    consent: bool = Field(..., description="Consentimiento explícito para guardar y vigilar este identificador")


class NotificationHitOut(BaseModel):
    boe_ref: str
    expediente: str | None
    matricula: str | None
    localidad: str | None
    importe: float | None
    fecha: str | None
    precepto: str | None
    articulo: str | None
    plazo_alegacion_fin: str | None = Field(None, description="DD/MM/AAAA")
    dias_restantes: int | None


class TargetOut(BaseModel):
    id: str
    label: str | None
    value_masked: str = Field(..., description="Identificador con todo menos los últimos caracteres ocultos")
    active: bool
    created_at: str
    last_checked_at: str | None
    notifications: list[NotificationHitOut]


class AdminUserOut(BaseModel):
    id: str
    email: str
    created_at: str
    plan: str | None
    subscription_status: str
    stripe_customer_id: str | None
    targets_count: int
    notifications_count: int


class AdminDayCount(BaseModel):
    day: str = Field(..., description="AAAA-MM-DD")
    total: int
    found: int


class AdminChecksResponse(BaseModel):
    total: int
    found: int
    not_found: int
    error: int
    by_day: list[AdminDayCount]

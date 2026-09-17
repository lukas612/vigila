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
    localidades: list[LocalityStat]

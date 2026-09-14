"""Catálogo curado de Mirko (SPEC_catalogo_y_materiales §2.2) — data del seed.

8 servicios mapeados a las 4 categorías administrables (#106). `precio` es display;
en USD se aclara "tipo de cambio paralelo". `pdf` referencia el material fuente en
`PDF_SOURCES`; `categoria` referencia una categoría por nombre (el seed la upserta).
Decisiones de negocio cerradas (2026-06-20).
"""

from __future__ import annotations

# Categorías del catálogo (#106). Editables luego desde la UI (ABM).
CATEGORIES = (
    "Capacitación personalizada",
    "Curso de producción audiovisual",
    "Paquetes de producción",
    "Desarrollo de marca personal",
)

# slug del material → nombre del archivo fuente (carpeta material 20-06-2026)
PDF_SOURCES = {
    "portafolio": "Portafolio_Cursos_Mirko_Calzadilla_Actualizado.pdf",
    "produccion": "Paquetes de Producción Audiovisual.pdf",
    "marca": "Marcas Premium.pdf",
}

CURATED: list[dict[str, object]] = [
    {
        "slug": "curso-contenido-edicion",
        "nombre": "Curso de Creación de Contenido y Edición",
        "categoria": "Curso de producción audiovisual",
        "resumen": "Curso híbrido en 4 módulos: producción audiovisual (presencial), CapCut "
        "básico y avanzado, e IA aplicada al contenido (virtual por Zoom).",
        "precio": "650 Bs",
        "moneda": "BOB",
        "flujo_cierre": "pago_qr",
        "pdf": "portafolio",
    },
    {
        "slug": "capacitacion-1a1",
        "nombre": "Capacitación Personalizada 1 a 1",
        "categoria": "Capacitación personalizada",
        "resumen": "Programa a medida por rubro (producción, edición, estrategia, marketing, "
        "marca personal, IA).",
        "precio": "1.200 USD (tipo de cambio paralelo)",
        "moneda": "USD",
        "flujo_cierre": "handoff_consultivo",
        "pdf": "portafolio",
    },
    {
        "slug": "produccion-estandar",
        "nombre": "Producción Estándar",
        "categoria": "Paquetes de producción",
        "resumen": "Rodaje cámara Sony a6400, edición CapCut. 5 videos + 12 fotos, 2 días.",
        "precio": "5.500 Bs",
        "moneda": "BOB",
        "flujo_cierre": "handoff_consultivo",
        "pdf": "produccion",
    },
    {
        "slug": "produccion-premium",
        "nombre": "Producción Premium / Cine",
        "categoria": "Paquetes de producción",
        "resumen": "Rodaje cine (Sony FX3/A7 IV, gimbal, drone), edición AE/Premiere/DaVinci "
        "+ IA. 5 videos + 12 fotos, 2 días.",
        "precio": "9.500 Bs",
        "moneda": "BOB",
        "flujo_cierre": "handoff_consultivo",
        "pdf": "produccion",
    },
    {
        "slug": "edicion-capcut",
        "nombre": "Edición mensual CapCut",
        "categoria": "Paquetes de producción",
        "resumen": "Edición accesible: 5 o 10 videos/mes.",
        "precio": "1.800 / 3.000 Bs",
        "moneda": "BOB",
        "flujo_cierre": "handoff_consultivo",
        "pdf": "produccion",
    },
    {
        "slug": "edicion-ae",
        "nombre": "Edición mensual After Effects",
        "categoria": "Paquetes de producción",
        "resumen": "Edición premium (AE/Premiere/DaVinci): 5 o 10 videos/mes; +150 Bs/video con IA.",
        "precio": "3.700 / 6.000 Bs",
        "moneda": "BOB",
        "flujo_cierre": "handoff_consultivo",
        "pdf": "produccion",
    },
    {
        "slug": "servicios-individuales",
        "nombre": "Servicios individuales",
        "categoria": "Paquetes de producción",
        "resumen": "Piezas sueltas: video individual, cinematográfico con IA, ediciones IA.",
        "precio": "desde 3.800 / 2.800 / 450 Bs",
        "moneda": "BOB",
        "flujo_cierre": "handoff_consultivo",
        "pdf": "produccion",
    },
    {
        "slug": "marca-alto-impacto",
        "nombre": "Marca de Alto Impacto",
        "categoria": "Desarrollo de marca personal",
        "resumen": "Programa integral mes a mes con equipo de cine: estrategia, producción, "
        "edición, fotografía, manejo de redes y reportes. Engloba producción + edición.",
        "precio": "$1.800 USD/mes · contrato mín. 3 meses (tipo de cambio paralelo)",
        "moneda": "USD",
        "flujo_cierre": "handoff_consultivo",
        "pdf": "marca",
    },
]

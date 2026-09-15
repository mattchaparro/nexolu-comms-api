"""Importa el CSV exportado de ManyChat a los contactos de Connect.

Contexto (analisis, secciones A.8 y M): la API publica de ManyChat NO
lista suscriptores masivamente - la exportacion es por su UI (CSV). Este
script toma ese CSV y siembra `contacts` (telefono + nombre + tags) para
que el dia que el numero de Luxury se mueva a Connect, los flujos y las
difusiones arranquen conociendo a la clientela.

Idempotente y aditivo: un contacto que ya existe (mismo app/negocio/
telefono) solo GANA tags y, si no tenia nombre, el del CSV - nunca se
pisa lo que Connect ya aprendio.

Uso:
    python scripts/import_manychat_contacts.py export.csv --app spa --business luxury-nails
    # columnas no estandar:
    python scripts/import_manychat_contacts.py export.csv --app spa \
        --phone-column "Whatsapp Phone" --name-column "Full Name" --tags-column "Tags"

Lee DATABASE_URL/COMMS_MASTER_KEY del entorno/.env, igual que el servicio.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path

from sqlalchemy import inspect

from nexolu_comms_api.core.db.session import get_sessionmaker, init_models
from nexolu_comms_api.core.flows.engine import ContactRepository

# Nombres de columna que ManyChat ha usado en sus exportaciones - se prueba
# en orden y gana la primera presente. Si la cuenta exporta otra cosa, los
# flags --*-column mandan.
PHONE_CANDIDATES = ("whatsapp phone", "phone", "phone number", "telefono", "teléfono")
NAME_CANDIDATES = ("full name", "name", "first name", "nombre")
TAGS_CANDIDATES = ("tags", "etiquetas")


def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    # Movil colombiano local -> con indicativo, igual que ChannelPhone en
    # las apps PHP del ecosistema.
    if len(digits) == 10 and digits.startswith("3"):
        digits = "57" + digits
    return digits if 11 <= len(digits) <= 15 else None


def _pick_column(header: list[str], explicit: str | None, candidates: tuple[str, ...]) -> str | None:
    if explicit:
        for column in header:
            if column.strip().lower() == explicit.strip().lower():
                return column
        raise SystemExit(f"La columna '{explicit}' no existe en el CSV. Columnas: {header}")
    lowered = {column.strip().lower(): column for column in header}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _read_rows(path: str) -> list[dict[str, str]]:
    # Lectura completa y sincrona a proposito: es un script de una sola
    # corrida, no un endpoint - y el CSV de un spa son cientos de filas.
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


async def _import(args: argparse.Namespace, rows: list[dict[str, str]]) -> None:
    if not rows:
        raise SystemExit("El CSV no tiene filas.")

    header = list(rows[0].keys())
    phone_col = _pick_column(header, args.phone_column, PHONE_CANDIDATES)
    if phone_col is None:
        raise SystemExit(f"No se encontro la columna de telefono. Columnas: {header} (usa --phone-column)")
    name_col = _pick_column(header, args.name_column, NAME_CANDIDATES)
    tags_col = _pick_column(header, args.tags_column, TAGS_CANDIDATES)

    await init_models()
    imported = updated = skipped = 0

    async with get_sessionmaker()() as session:
        repo = ContactRepository(session)

        for row in rows:
            phone = _normalize_phone(row.get(phone_col) or "")
            if phone is None:
                skipped += 1
                continue

            name = (row.get(name_col) or "").strip() if name_col else ""
            tags = [t.strip() for t in re.split(r"[,;]", row.get(tags_col) or "") if t.strip()] if tags_col else []

            contact = await repo.get_or_create(args.app, args.business, phone, name=name)
            if inspect(contact).pending:
                imported += 1
            else:
                updated += 1
            await session.flush()

            if tags:
                # Aditivo: los tags de ManyChat se SUMAN a los que Connect
                # ya haya puesto, sin duplicar.
                contact.tags = list(dict.fromkeys([*contact.tags, *tags]))

        await session.commit()

    print(f"Importados: {imported} | Actualizados: {updated} | Sin telefono valido (saltados): {skipped}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", help="Ruta del CSV exportado de ManyChat")
    parser.add_argument("--app", required=True, help="app_id de Connect duena de los contactos (p.ej. spa)")
    parser.add_argument("--business", default="", help="business_id dentro de la app; vacio = nivel de app")
    parser.add_argument("--phone-column", default=None)
    parser.add_argument("--name-column", default=None)
    parser.add_argument("--tags-column", default=None)
    args = parser.parse_args()
    asyncio.run(_import(args, _read_rows(args.csv)))


if __name__ == "__main__":
    sys.exit(main())

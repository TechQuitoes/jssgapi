"""One-time backfill script: translate existing English data to Hindi.

This script reads all existing records from members, social_groups,
family_members and professional_information, translates the English text
fields to Hindi via Azure Translator (Free F0 tier) and writes the result
into the new *_hi columns added by migration g1h2i3j4k5l6.

Usage
-----
  # Dry-run: shows what WOULD be translated, writes nothing to the database
  .venv/bin/python -m scripts.backfill_hindi_translations --dry-run

  # Actually run the translations and save to database
  .venv/bin/python -m scripts.backfill_hindi_translations --apply

  # Re-translate even rows that already have Hindi text (force overwrite)
  .venv/bin/python -m scripts.backfill_hindi_translations --apply --force

Notes
-----
* The script skips any field that is NULL / blank in English.
* Without --force it also skips rows that already have a Hindi value,
  so it is safe to run more than once (idempotent).
* A small delay (0.1 s) between API calls is added to avoid rate-limit
  bursts, though the Azure Free tier has generous per-second limits.
* Progress is printed to stdout so you can monitor it in real time.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session_factory
from app.models.family import FamilyMember
from app.models.group import SocialGroup
from app.models.member import Member
from app.models.professional import ProfessionalInformation
from app.services.translate_service import _translate_via_azure

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("backfill_hindi")

# Small sleep between consecutive API calls to stay polite to the free tier
_API_DELAY = 0.08  # seconds


def _tx(text: str | None) -> str | None:
    """Translate *text* from English to Hindi; returns None on blank / error."""
    if not text or not text.strip():
        return None
    try:
        result = _translate_via_azure(text.strip(), target_language="hi", source_language="en")
        time.sleep(_API_DELAY)
        return result.translated_text or None
    except Exception as exc:
        logger.warning("Translation failed for %r: %s", text[:60], exc)
        return None


async def backfill_members(session, dry_run: bool, force: bool) -> int:
    """Translate members' name, address and occupation fields."""
    result = await session.execute(select(Member))
    members = result.scalars().all()
    updated = 0

    for m in members:
        changed = False

        def _update(current_hi: str | None, en_value: str | None, setter):
            nonlocal changed
            if not en_value or not en_value.strip():
                return
            if current_hi and not force:
                return  # already has Hindi translation
            hi = _tx(en_value)
            if hi:
                setter(hi)
                changed = True

        _update(m.first_name_hi,         m.first_name,         lambda v: setattr(m, "first_name_hi", v))
        _update(m.middle_name_hi,         m.middle_name,        lambda v: setattr(m, "middle_name_hi", v))
        _update(m.last_name_hi,           m.last_name,          lambda v: setattr(m, "last_name_hi", v))
        _update(m.address_line_hi,        m.address_line,       lambda v: setattr(m, "address_line_hi", v))
        _update(m.occupation_summary_hi,  m.occupation_summary, lambda v: setattr(m, "occupation_summary_hi", v))

        if changed:
            updated += 1
            if dry_run:
                logger.info(
                    "[DRY-RUN] Member %s %s → first_name_hi=%r",
                    m.first_name, m.last_name, m.first_name_hi,
                )
            else:
                logger.info("Translated member: %s %s", m.first_name, m.last_name)

    if not dry_run and updated:
        await session.flush()

    return updated


async def backfill_groups(session, dry_run: bool, force: bool) -> int:
    """Translate social groups' name, description and contact_name fields."""
    result = await session.execute(select(SocialGroup))
    groups = result.scalars().all()
    updated = 0

    for g in groups:
        changed = False

        def _update(current_hi: str | None, en_value: str | None, setter):
            nonlocal changed
            if not en_value or not en_value.strip():
                return
            if current_hi and not force:
                return
            hi = _tx(en_value)
            if hi:
                setter(hi)
                changed = True

        _update(g.name_hi,         g.name,         lambda v: setattr(g, "name_hi", v))
        _update(g.description_hi,  g.description,  lambda v: setattr(g, "description_hi", v))
        _update(g.contact_name_hi, g.contact_name, lambda v: setattr(g, "contact_name_hi", v))

        if changed:
            updated += 1
            if dry_run:
                logger.info("[DRY-RUN] Group %r → name_hi=%r", g.name, g.name_hi)
            else:
                logger.info("Translated group: %s", g.name)

    if not dry_run and updated:
        await session.flush()

    return updated


async def backfill_family_members(session, dry_run: bool, force: bool) -> int:
    """Translate family members' name and occupation fields."""
    result = await session.execute(select(FamilyMember))
    family_members = result.scalars().all()
    updated = 0

    for fm in family_members:
        changed = False

        def _update(current_hi: str | None, en_value: str | None, setter):
            nonlocal changed
            if not en_value or not en_value.strip():
                return
            if current_hi and not force:
                return
            hi = _tx(en_value)
            if hi:
                setter(hi)
                changed = True

        _update(fm.name_hi,       fm.name,       lambda v: setattr(fm, "name_hi", v))
        _update(fm.occupation_hi, fm.occupation, lambda v: setattr(fm, "occupation_hi", v))

        if changed:
            updated += 1
            if dry_run:
                logger.info("[DRY-RUN] FamilyMember %r → name_hi=%r", fm.name, fm.name_hi)
            else:
                logger.info("Translated family member: %s", fm.name)

    if not dry_run and updated:
        await session.flush()

    return updated


async def backfill_professional(session, dry_run: bool, force: bool) -> int:
    """Translate professional information fields."""
    result = await session.execute(select(ProfessionalInformation))
    pros = result.scalars().all()
    updated = 0

    for p in pros:
        changed = False

        def _update(current_hi: str | None, en_value: str | None, setter):
            nonlocal changed
            if not en_value or not en_value.strip():
                return
            if current_hi and not force:
                return
            hi = _tx(en_value)
            if hi:
                setter(hi)
                changed = True

        _update(p.occupation_hi,   p.occupation,   lambda v: setattr(p, "occupation_hi", v))
        _update(p.company_name_hi, p.company_name, lambda v: setattr(p, "company_name_hi", v))
        _update(p.designation_hi,  p.designation,  lambda v: setattr(p, "designation_hi", v))

        if changed:
            updated += 1
            if dry_run:
                logger.info("[DRY-RUN] Professional %r → occupation_hi=%r", p.occupation, p.occupation_hi)
            else:
                logger.info("Translated professional info: %s @ %s", p.occupation, p.company_name)

    if not dry_run and updated:
        await session.flush()

    return updated


async def _run(dry_run: bool, force: bool) -> None:
    mode = "DRY-RUN (nothing will be saved)" if dry_run else "APPLY (writing to database)"
    logger.info("=" * 60)
    logger.info("Hindi backfill starting — mode: %s", mode)
    if force:
        logger.info("--force: existing Hindi translations will be overwritten")
    logger.info("=" * 60)

    async with async_session_factory() as session:
        async with session.begin():
            m_count  = await backfill_members(session, dry_run, force)
            g_count  = await backfill_groups(session, dry_run, force)
            fm_count = await backfill_family_members(session, dry_run, force)
            p_count  = await backfill_professional(session, dry_run, force)

            if dry_run:
                logger.info("DRY-RUN complete — no changes committed.")
            else:
                logger.info("Committing all translations to database …")
                # commit happens automatically via session.begin() context manager

    logger.info("=" * 60)
    logger.info("Summary:")
    logger.info("  Members translated        : %d", m_count)
    logger.info("  Groups translated         : %d", g_count)
    logger.info("  Family members translated : %d", fm_count)
    logger.info("  Professional info records : %d", p_count)
    logger.info("  TOTAL                     : %d", m_count + g_count + fm_count + p_count)
    if dry_run:
        logger.info("Run with --apply to save these translations to the database.")
    else:
        logger.info("Done! All Hindi translations have been saved.")
    logger.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill Hindi (_hi) translation columns for existing database records."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which records would be translated without writing to the database.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Translate and save Hindi text to the database.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Re-translate rows that already have a Hindi value (default: skip existing).",
    )
    args = parser.parse_args()
    asyncio.run(_run(dry_run=args.dry_run, force=args.force))


if __name__ == "__main__":
    main()

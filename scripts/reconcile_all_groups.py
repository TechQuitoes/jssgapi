"""Reconcile and synchronize all groups and member relationships across the entire database.

This script:
1. Re-links Manish Shah to Taloda (MH3901001) and detaches him from Navsari (fixing admin phone collision).
2. Deduplicates any duplicate (member_id, group_id) rows in member_group_memberships.
3. Automatically re-links all orphaned members whose profile_data->>'social_group_name' matches an active group.
4. Backfills missing member_group_memberships (priority 1) for all members who have members.group_id set.
5. Syncs profile_data['social_group_name'] to the actual social_groups.name for all linked members.
6. Ensures unique constraint on member_group_memberships(member_id, group_id).
7. Validates all 123 groups for 100% consistency between card counts and listing counts.
"""

import asyncio
from sqlalchemy import text
from app.database import async_session_factory


async def reconcile():
    async with async_session_factory() as session:
        print("=== STEP 1: Fix Manish Shah / Taloda / Navsari ===")
        # Check current state of Manish Shah
        taloda_id = "b59cc2fe-030c-470c-8199-661d96dcfc2a"
        navsari_id = "f09edba8-1527-43cf-b760-f4900b43cc73"
        manish_user_id = "f79d8372-04de-4168-adab-202ba1551516"
        manish_member_id = "1cab61e3-edc2-4c28-97f7-5139cc3040aa"

        # Update User
        await session.execute(text("""
            UPDATE users
            SET full_name = 'Manish Shah',
                group_id = :taloda_id
            WHERE id = :user_id;
        """), {"taloda_id": taloda_id, "user_id": manish_user_id})

        # Update Member
        await session.execute(text("""
            UPDATE members
            SET first_name = 'Manish',
                last_name = 'Shah',
                group_id = :taloda_id,
                membership_number = 'MH3901001',
                membership_status = 'APPROVED',
                is_post_holder = true,
                post_holder_title = 'Group Admin',
                profile_data = jsonb_set(
                    jsonb_set(profile_data, '{social_group_name}', '"Jain Shwetambar Social Group Taloda"'),
                    '{full_name}', '"Manish Shah"'
                )
            WHERE id = :member_id;
        """), {"taloda_id": taloda_id, "member_id": manish_member_id})

        # Remove Navsari membership for Manish Shah
        await session.execute(text("""
            DELETE FROM member_group_memberships
            WHERE member_id = :member_id AND group_id = :navsari_id;
        """), {"member_id": manish_member_id, "navsari_id": navsari_id})

        # Ensure Taloda membership for Manish Shah
        await session.execute(text("""
            DELETE FROM member_group_memberships
            WHERE member_id = :member_id AND group_id = :taloda_id;
        """), {"member_id": manish_member_id, "taloda_id": taloda_id})

        await session.execute(text("""
            INSERT INTO member_group_memberships (
                id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), :member_id, :taloda_id, 1, 'MH3901001', NOW(), NOW(), NOW()
            );
        """), {"member_id": manish_member_id, "taloda_id": taloda_id})

        # Clear duplicate contact_phone from Navsari so collision doesn't persist
        await session.execute(text("""
            UPDATE social_groups
            SET contact_phone = NULL
            WHERE id = :navsari_id AND contact_phone = '9428715439';
        """), {"navsari_id": navsari_id})
        print("-> Manish Shah successfully restored to Taloda (MH3901001)")

        print("\n=== STEP 2: Deduplicate member_group_memberships ===")
        dedup_res = await session.execute(text("""
            WITH duplicates AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY member_id, group_id 
                           ORDER BY priority ASC, created_at ASC
                       ) as rn
                FROM member_group_memberships
            )
            DELETE FROM member_group_memberships
            WHERE id IN (
                SELECT id FROM duplicates WHERE rn > 1
            );
        """))
        print(f"-> Removed {dedup_res.rowcount} duplicate membership rows.")

        print("\n=== STEP 3: Ensure unique index on member_group_memberships(member_id, group_id) ===")
        await session.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_member_group_memberships_member_group
            ON member_group_memberships (member_id, group_id);
        """))
        print("-> Unique constraint/index verified.")

        print("\n=== STEP 4: Auto-link orphaned members by profile social_group_name ===")
        relink_res = await session.execute(text("""
            WITH matched_orphans AS (
                SELECT m.id as member_id, g.id as group_id
                FROM members m
                JOIN social_groups g 
                  ON lower(trim(m.profile_data->>'social_group_name')) = lower(trim(g.name))
                WHERE m.is_deleted = false
                  AND g.is_deleted = false
                  AND m.group_id IS NULL
            )
            UPDATE members m
            SET group_id = mo.group_id
            FROM matched_orphans mo
            WHERE m.id = mo.member_id;
        """))
        print(f"-> Re-linked {relink_res.rowcount} orphan members to their matching active groups.")

        print("\n=== STEP 5: Backfill missing primary member_group_memberships ===")
        backfill_res = await session.execute(text("""
            INSERT INTO member_group_memberships (
                id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at
            )
            SELECT 
                gen_random_uuid(),
                m.id,
                m.group_id,
                1,
                m.membership_number,
                COALESCE(m.joined_at, NOW()),
                NOW(),
                NOW()
            FROM members m
            WHERE m.group_id IS NOT NULL
              AND m.is_deleted = false
              AND NOT EXISTS (
                  SELECT 1 FROM member_group_memberships mgm 
                  WHERE mgm.member_id = m.id AND mgm.group_id = m.group_id
              );
        """))
        print(f"-> Backfilled {backfill_res.rowcount} missing member_group_memberships rows.")

        print("\n=== STEP 6: Synchronize profile_data['social_group_name'] with active group name ===")
        sync_prof_res = await session.execute(text("""
            UPDATE members m
            SET profile_data = jsonb_set(
                COALESCE(m.profile_data, '{}'::jsonb),
                '{social_group_name}',
                to_jsonb(g.name)
            )
            FROM social_groups g
            WHERE m.group_id = g.id
              AND g.is_deleted = false
              AND (
                  m.profile_data->>'social_group_name' IS NULL 
                  OR trim(m.profile_data->>'social_group_name') != trim(g.name)
              );
        """))
        print(f"-> Synchronized profile_data for {sync_prof_res.rowcount} members.")

        await session.commit()
        print("\nAll database changes committed successfully!")

        print("\n=== STEP 7: Comprehensive Validation Audit ===")
        audit_sql = """
        WITH card_counts AS (
            SELECT g.id, g.name, g.group_id, count(distinct m.id) as card_count
            FROM social_groups g
            LEFT JOIN member_group_memberships mgm ON mgm.group_id = g.id
            LEFT JOIN members m ON (m.id = mgm.member_id OR m.group_id = g.id)
                                AND m.is_deleted = false AND m.membership_status = 'APPROVED'
            WHERE g.is_deleted = false
            GROUP BY g.id, g.name, g.group_id
        ),
        list_counts AS (
            SELECT g.id, count(distinct m.id) as list_count
            FROM social_groups g
            LEFT JOIN members m ON (
                m.is_deleted = false 
                AND m.membership_status = 'APPROVED'
                AND (
                    (m.group_id IS NULL AND NOT EXISTS (SELECT 1 FROM member_group_memberships mgm2 WHERE mgm2.member_id = m.id) 
                     AND lower(trim(m.profile_data->>'social_group_name')) = lower(trim(g.name)))
                    OR m.group_id = g.id
                    OR EXISTS (SELECT 1 FROM member_group_memberships mgm WHERE mgm.member_id = m.id AND mgm.group_id = g.id)
                )
            )
            WHERE g.is_deleted = false
            GROUP BY g.id
        )
        SELECT cc.id, cc.name, cc.group_id, cc.card_count, lc.list_count
        FROM card_counts cc
        JOIN list_counts lc ON cc.id = lc.id
        WHERE cc.card_count != lc.list_count
        ORDER BY cc.name;
        """
        audit_res = await session.execute(text(audit_sql))
        mismatches = audit_res.mappings().all()
        print(f"Total groups with count mismatch: {len(mismatches)}")
        if mismatches:
            for m in mismatches:
                print("Mismatch:", dict(m))
        else:
            print("PERFECT: 100% of all groups have exact matching card count and member listing!")


if __name__ == "__main__":
    asyncio.run(reconcile())

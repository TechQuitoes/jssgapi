-- ============================================================================
-- JSSG Permanent Reconcile & Data Integrity Script for All Groups
-- Run on Hostinger VPS:
-- docker exec -i jssg_db psql -U jssg -d jssg_db < scripts/reconcile_all_groups.sql
-- ============================================================================

BEGIN;

-- 1. Restore Manish Shah to Taloda (MH3901) and detach from Navsari
UPDATE users
SET full_name = 'Manish Shah',
    group_id = 'b59cc2fe-030c-470c-8199-661d96dcfc2a'
WHERE id = 'f79d8372-04de-4168-adab-202ba1551516';

UPDATE members
SET first_name = 'Manish',
    last_name = 'Shah',
    group_id = 'b59cc2fe-030c-470c-8199-661d96dcfc2a',
    membership_number = 'MH3901001',
    membership_status = 'APPROVED',
    is_post_holder = true,
    post_holder_title = 'Group Admin',
    profile_data = jsonb_set(
        jsonb_set(COALESCE(profile_data, '{}'::jsonb), '{social_group_name}', '"Jain Shwetambar Social Group Taloda"'),
        '{full_name}', '"Manish Shah"'
    )
WHERE id = '1cab61e3-edc2-4c28-97f7-5139cc3040aa';

DELETE FROM member_group_memberships
WHERE member_id = '1cab61e3-edc2-4c28-97f7-5139cc3040aa'
  AND group_id = 'f09edba8-1527-43cf-b760-f4900b43cc73';

DELETE FROM member_group_memberships
WHERE member_id = '1cab61e3-edc2-4c28-97f7-5139cc3040aa'
  AND group_id = 'b59cc2fe-030c-470c-8199-661d96dcfc2a';

INSERT INTO member_group_memberships (
    id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at
) VALUES (
    gen_random_uuid(),
    '1cab61e3-edc2-4c28-97f7-5139cc3040aa',
    'b59cc2fe-030c-470c-8199-661d96dcfc2a',
    1,
    'MH3901001',
    NOW(),
    NOW(),
    NOW()
);

UPDATE social_groups
SET contact_phone = NULL
WHERE id = 'f09edba8-1527-43cf-b760-f4900b43cc73' AND contact_phone = '9428715439';

-- 2. Deduplicate member_group_memberships (e.g. Aalot duplicate rows)
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

-- 3. Ensure unique constraint on member_group_memberships(member_id, group_id)
CREATE UNIQUE INDEX IF NOT EXISTS uq_member_group_memberships_member_group
ON member_group_memberships (member_id, group_id);

-- 4. Auto-link orphaned members by profile social_group_name
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

-- 5. Backfill missing primary member_group_memberships
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

-- 6. Synchronize profile_data['social_group_name'] with active group name
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

COMMIT;

-- Validation check: should return 0 rows
SELECT g.id, g.name, g.group_id,
       count(distinct m.id) as card_count
FROM social_groups g
LEFT JOIN member_group_memberships mgm ON mgm.group_id = g.id
LEFT JOIN members m ON (m.id = mgm.member_id OR m.group_id = g.id)
                    AND m.is_deleted = false AND m.membership_status = 'APPROVED'
WHERE g.is_deleted = false
  AND g.name IN ('Jain Shwetambar Social Group Taloda', 'Jain Shwetambar Social Group Indore Lotus', 'Jain Shwetambar Social Group Navsari')
GROUP BY g.id, g.name, g.group_id;

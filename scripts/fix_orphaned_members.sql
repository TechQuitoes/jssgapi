-- =====================================================================
-- FIX ORPHANED MEMBERS & CLEAN DUPLICATE MEMBERSHIPS
-- =====================================================================

BEGIN;

-- 1. Remove duplicate memberships (e.g. Aalot Arpit Jain duplicate)
DELETE FROM member_group_memberships
WHERE id IN (
    SELECT id FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY member_id, group_id 
                   ORDER BY priority ASC, created_at ASC
               ) as rnum
        FROM member_group_memberships
    ) t
    WHERE t.rnum > 1
);

-- 2. Soft-delete dummy test member 'Test Admin'
UPDATE members SET is_deleted = true, deleted_at = NOW() WHERE id = '50067cd9-e56a-47d6-a936-eed76bbdee00';
UPDATE users SET is_deleted = true, deleted_at = NOW() WHERE id = 'bd5d298f-5188-4f08-8a5d-a23718399675';

-- 3. Re-link 16 orphaned members and group admins to their respective groups:

-- 1) Hullas Chopra -> Indore Lotus (MP0922)
UPDATE members SET group_id = '3885dc56-30b7-449e-9528-4ab72dc58b1e', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP0922002') WHERE id = '6fac6037-1fd3-4740-82fb-e38f132f73a2';
UPDATE users SET group_id = '3885dc56-30b7-449e-9528-4ab72dc58b1e' WHERE id = 'fbd36f61-24c1-47b0-b179-7c06a569d928';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '6fac6037-1fd3-4740-82fb-e38f132f73a2', '3885dc56-30b7-449e-9528-4ab72dc58b1e', 1, 'MP0922002', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 2) Vishal Jain -> Indore Sarthak (MP09040)
UPDATE members SET group_id = '57b72722-b872-4384-89e0-30319ab39e37', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP09040002') WHERE id = '1416312b-b17a-4f34-bdf3-ee8ccf0ed4c1';
UPDATE users SET group_id = '57b72722-b872-4384-89e0-30319ab39e37' WHERE id = 'ee71824a-8fa5-4812-b3d4-c3b80cd28be8';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '1416312b-b17a-4f34-bdf3-ee8ccf0ed4c1', '57b72722-b872-4384-89e0-30319ab39e37', 1, 'MP09040002', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 3) Dharmendra Sekhawat -> Indore Classic (MP09038)
UPDATE members SET group_id = 'e16ad5c2-0949-4eef-b0d3-7537da3eef70', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP09038002') WHERE id = '1a12f0af-35b5-4b24-8083-934ca49ec3ed';
UPDATE users SET group_id = 'e16ad5c2-0949-4eef-b0d3-7537da3eef70' WHERE id = '46815e89-159c-4453-9215-6b8da4d2f347';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '1a12f0af-35b5-4b24-8083-934ca49ec3ed', 'e16ad5c2-0949-4eef-b0d3-7537da3eef70', 1, 'MP09038002', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 4) Praveen Makawana -> Indore Classic (MP09038)
UPDATE members SET group_id = 'e16ad5c2-0949-4eef-b0d3-7537da3eef70', membership_number = COALESCE(membership_number, 'MP09038003') WHERE id = 'd77caeb9-d3f8-4f5a-949b-0f2a6f538417';
UPDATE users SET group_id = 'e16ad5c2-0949-4eef-b0d3-7537da3eef70' WHERE id = '5f9285d1-2969-457d-9309-dae7aa568bc6';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), 'd77caeb9-d3f8-4f5a-949b-0f2a6f538417', 'e16ad5c2-0949-4eef-b0d3-7537da3eef70', 1, 'MP09038003', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 5) Varun Tated -> Indore Millennium (MP0907)
UPDATE members SET group_id = '06aa4f91-2a56-403d-83f4-2b656bb94318', membership_number = COALESCE(membership_number, 'MP0907010') WHERE id = '010f5962-c259-42cb-82a5-a4f8519a18ca';
UPDATE users SET group_id = '06aa4f91-2a56-403d-83f4-2b656bb94318' WHERE id = '02e90626-1dde-4ae2-a27b-caf90e4b14b1';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '010f5962-c259-42cb-82a5-a4f8519a18ca', '06aa4f91-2a56-403d-83f4-2b656bb94318', 1, 'MP0907010', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 6) Nitin Jain -> Indore Millennium (MP0907)
UPDATE members SET group_id = '06aa4f91-2a56-403d-83f4-2b656bb94318', membership_number = COALESCE(membership_number, 'MP0907011') WHERE id = '4dd1567b-fd71-49e5-b7b6-48fa72ba9437';
UPDATE users SET group_id = '06aa4f91-2a56-403d-83f4-2b656bb94318' WHERE id = '42b74949-fc3c-4ac4-932b-683c8e773bc5';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '4dd1567b-fd71-49e5-b7b6-48fa72ba9437', '06aa4f91-2a56-403d-83f4-2b656bb94318', 1, 'MP0907011', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 7) Himanshu Jain -> Indore Millennium (MP0907)
UPDATE members SET group_id = '06aa4f91-2a56-403d-83f4-2b656bb94318', membership_number = COALESCE(membership_number, 'MP0907012') WHERE id = 'bfd5820a-26d1-412b-9edf-3acb45319901';
UPDATE users SET group_id = '06aa4f91-2a56-403d-83f4-2b656bb94318' WHERE id = 'e1f87d30-3368-4afe-ac87-438f1a4fe7c4';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), 'bfd5820a-26d1-412b-9edf-3acb45319901', '06aa4f91-2a56-403d-83f4-2b656bb94318', 1, 'MP0907012', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 8) Mahendra Surana -> Indore Town (MP0918)
UPDATE members SET group_id = '791df3d6-4e70-4bc3-ae55-e9e1f884ba35', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP0918002') WHERE id = '5bde8692-3875-4a8c-8863-20ea1503d338';
UPDATE users SET group_id = '791df3d6-4e70-4bc3-ae55-e9e1f884ba35' WHERE id = '27d08bd3-6d10-4cb9-9a7c-533f53a679fa';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '5bde8692-3875-4a8c-8863-20ea1503d338', '791df3d6-4e70-4bc3-ae55-e9e1f884ba35', 1, 'MP0918002', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 9) Rajesh Bafna -> Indore PM Parivar (MP0920)
UPDATE members SET group_id = 'f3cc0e11-0a2c-444e-9753-55905b73b124', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP0920002') WHERE id = '825e9690-fb18-40ea-b76f-c089f9b1c5f1';
UPDATE users SET group_id = 'f3cc0e11-0a2c-444e-9753-55905b73b124' WHERE id = '293ba0f3-aba9-490e-8e3b-05fbbd167abc';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '825e9690-fb18-40ea-b76f-c089f9b1c5f1', 'f3cc0e11-0a2c-444e-9753-55905b73b124', 1, 'MP0920002', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 10) Vikram Shreemal -> Indore Aagrah (MP0926)
UPDATE members SET group_id = '65f69457-7768-42d5-a545-5514916a9870', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP0926002') WHERE id = '8a1cef9c-32f8-4fea-89a9-6e60b8083e0b';
UPDATE users SET group_id = '65f69457-7768-42d5-a545-5514916a9870' WHERE id = '02b71590-7cbc-4488-8ad1-edb9e40afc1c';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '8a1cef9c-32f8-4fea-89a9-6e60b8083e0b', '65f69457-7768-42d5-a545-5514916a9870', 1, 'MP0926002', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 11) Mahendra Challani -> Indore Swastik (MP0925)
UPDATE members SET group_id = 'bf57ab05-286d-41e2-96f8-f88efcfbf1ff', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP0925002') WHERE id = '9e79a45a-2c82-43c9-b717-9dbbd13933ee';
UPDATE users SET group_id = 'bf57ab05-286d-41e2-96f8-f88efcfbf1ff' WHERE id = '8e6fbb42-d67a-4a12-98d4-35de18ec76e8';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '9e79a45a-2c82-43c9-b717-9dbbd13933ee', 'bf57ab05-286d-41e2-96f8-f88efcfbf1ff', 1, 'MP0925002', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 12) Nishant Kothari -> Indore Pioneer (MP09039)
UPDATE members SET group_id = 'e9aaa841-6c2e-471f-b3cd-363279461eb9', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP09039002') WHERE id = 'b55f0f0c-6b78-4c4e-a745-1b9fe03dd4d9';
UPDATE users SET group_id = 'e9aaa841-6c2e-471f-b3cd-363279461eb9' WHERE id = '0d4ea2ad-2061-4ae2-9c41-9ee556b29b1f';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), 'b55f0f0c-6b78-4c4e-a745-1b9fe03dd4d9', 'e9aaa841-6c2e-471f-b3cd-363279461eb9', 1, 'MP09039002', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 13) Basant Bapna -> Indore Prime (MP0917)
UPDATE members SET group_id = 'ad256e87-d571-43ed-8a01-3a0fc595a257', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP0917002') WHERE id = 'f569614f-5cca-407d-9df8-23f145b251dc';
UPDATE users SET group_id = 'ad256e87-d571-43ed-8a01-3a0fc595a257' WHERE id = 'd4cb75b3-c6f3-4b17-90f9-bbf47eeb363d';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), 'f569614f-5cca-407d-9df8-23f145b251dc', 'ad256e87-d571-43ed-8a01-3a0fc595a257', 1, 'MP0917002', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 14) Prakash Jain -> JSSG Professional Unity Indore (MP0901)
UPDATE members SET group_id = 'ed6c3f55-dd93-4086-a397-7d68265f2c3b', membership_number = COALESCE(membership_number, 'MP0901104') WHERE id = '898adb62-7b77-44a5-b259-7c761eda6ad8';
UPDATE users SET group_id = 'ed6c3f55-dd93-4086-a397-7d68265f2c3b' WHERE id = 'ae4910e4-446a-49e7-8bfe-eac45c3d34f3';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '898adb62-7b77-44a5-b259-7c761eda6ad8', 'ed6c3f55-dd93-4086-a397-7d68265f2c3b', 1, 'MP0901104', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 15) Vinay Vinay -> JSSG Professional Unity Indore Testing (MP0902)
UPDATE members SET group_id = 'b88c363f-eb37-476c-89e1-741a4c9a5c00', membership_number = COALESCE(membership_number, 'MP0902015') WHERE id = '4784e216-5302-480e-9f20-03e2f51727f9';
UPDATE users SET group_id = 'b88c363f-eb37-476c-89e1-741a4c9a5c00' WHERE id = 'fab819c1-6b13-4fbf-b47a-fc7000bf11f9';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), '4784e216-5302-480e-9f20-03e2f51727f9', 'b88c363f-eb37-476c-89e1-741a4c9a5c00', 1, 'MP0902015', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

-- 16) Bipul Jain -> JSSG Professional Unity Indore Testing (MP0902)
UPDATE members SET group_id = 'b88c363f-eb37-476c-89e1-741a4c9a5c00', is_post_holder = true, post_holder_title = 'Group Admin', membership_number = COALESCE(membership_number, 'MP0902016') WHERE id = 'ccc9c869-5b66-466e-b4e3-9e8bc01d8823';
UPDATE users SET group_id = 'b88c363f-eb37-476c-89e1-741a4c9a5c00' WHERE id = '38eb5098-6352-4c33-b640-05b6b19e9fa3';
INSERT INTO member_group_memberships (id, member_id, group_id, priority, membership_number, joined_at, created_at, updated_at)
VALUES (gen_random_uuid(), 'ccc9c869-5b66-466e-b4e3-9e8bc01d8823', 'b88c363f-eb37-476c-89e1-741a4c9a5c00', 1, 'MP0902016', NOW(), NOW(), NOW())
ON CONFLICT (member_id, group_id) DO UPDATE SET priority = 1, membership_number = EXCLUDED.membership_number;

COMMIT;

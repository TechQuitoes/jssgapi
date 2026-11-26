"""Repair script to re-link orphaned members and group admins back to their respective groups.

This handles members who had their groups deleted and recreated, leaving them with
members.group_id = NULL and users.group_id = NULL, while profile_data->>'social_group_name'
still contained their group name.

Also cleans up duplicate member_group_memberships entries (such as duplicate Arpit Jain in Aalot).
"""

import asyncio
import uuid
from datetime import datetime, UTC
from sqlalchemy import text, select, func
from app.database import async_session_factory
from app.models.member import Member
from app.models.user import User, Role
from app.models.group import SocialGroup
from app.models.member_group import MemberGroupMembership
from app.services.group_id_service import assign_member_id

# Exact mappings for orphaned members to their active social groups:
# (member_id, user_id, group_id, fallback_role)
ORPHAN_MAPPINGS = [
    # Hullas Chopra -> Indore Lotus (MP0922)
    ("6fac6037-1fd3-4740-82fb-e38f132f73a2", "fbd36f61-24c1-47b0-b179-7c06a569d928", "3885dc56-30b7-449e-9528-4ab72dc58b1e", "Group Admin"),
    # Vishal Jain -> Indore Sarthak (MP09040)
    ("1416312b-b17a-4f34-bdf3-ee8ccf0ed4c1", "ee71824a-8fa5-4812-b3d4-c3b80cd28be8", "57b72722-b872-4384-89e0-30319ab39e37", "Group Admin"),
    # Dharmendra Sekhawat -> Indore Classic (MP09038)
    ("1a12f0af-35b5-4b24-8083-934ca49ec3ed", "46815e89-159c-4453-9215-6b8da4d2f347", "e16ad5c2-0949-4eef-b0d3-7537da3eef70", "Group Admin"),
    # Praveen Makawana -> Indore Classic (MP09038)
    ("d77caeb9-d3f8-4f5a-949b-0f2a6f538417", "5f9285d1-2969-457d-9309-dae7aa568bc6", "e16ad5c2-0949-4eef-b0d3-7537da3eef70", None),
    # Varun Tated -> Indore Millennium (MP0907)
    ("010f5962-c259-42cb-82a5-a4f8519a18ca", "02e90626-1dde-4ae2-a27b-caf90e4b14b1", "06aa4f91-2a56-403d-83f4-2b656bb94318", None),
    # Nitin Jain -> Indore Millennium (MP0907)
    ("4dd1567b-fd71-49e5-b7b6-48fa72ba9437", "42b74949-fc3c-4ac4-932b-683c8e773bc5", "06aa4f91-2a56-403d-83f4-2b656bb94318", None),
    # Himanshu Jain -> Indore Millennium (MP0907)
    ("bfd5820a-26d1-412b-9edf-3acb45319901", "e1f87d30-3368-4afe-ac87-438f1a4fe7c4", "06aa4f91-2a56-403d-83f4-2b656bb94318", None),
    # Mahendra Surana -> Indore Town (MP0918)
    ("5bde8692-3875-4a8c-8863-20ea1503d338", "27d08bd3-6d10-4cb9-9a7c-533f53a679fa", "791df3d6-4e70-4bc3-ae55-e9e1f884ba35", "Group Admin"),
    # Rajesh Bafna -> Indore PM Parivar (MP0920)
    ("825e9690-fb18-40ea-b76f-c089f9b1c5f1", "293ba0f3-aba9-490e-8e3b-05fbbd167abc", "f3cc0e11-0a2c-444e-9753-55905b73b124", "Group Admin"),
    # Vikram Shreemal -> Indore Aagrah (MP0926)
    ("8a1cef9c-32f8-4fea-89a9-6e60b8083e0b", "02b71590-7cbc-4488-8ad1-edb9e40afc1c", "65f69457-7768-42d5-a545-5514916a9870", "Group Admin"),
    # Mahendra Challani -> Indore Swastik (MP0925)
    ("9e79a45a-2c82-43c9-b717-9dbbd13933ee", "8e6fbb42-d67a-4a12-98d4-35de18ec76e8", "bf57ab05-286d-41e2-96f8-f88efcfbf1ff", "Group Admin"),
    # Nishant Kothari -> Indore Pioneer (MP09039)
    ("b55f0f0c-6b78-4c4e-a745-1b9fe03dd4d9", "0d4ea2ad-2061-4ae2-9c41-9ee556b29b1f", "e9aaa841-6c2e-471f-b3cd-363279461eb9", "Group Admin"),
    # Basant Bapna -> Indore Prime (MP0917)
    ("f569614f-5cca-407d-9df8-23f145b251dc", "d4cb75b3-c6f3-4b17-90f9-bbf47eeb363d", "ad256e87-d571-43ed-8a01-3a0fc595a257", "Group Admin"),
    # Prakash Jain -> JSSG Professional Unity Indore (MP0901)
    ("898adb62-7b77-44a5-b259-7c761eda6ad8", "ae4910e4-446a-49e7-8bfe-eac45c3d34f3", "ed6c3f55-dd93-4086-a397-7d68265f2c3b", None),
    # Vinay Vinay -> JSSG Professional Unity Indore Testing (MP0902)
    ("4784e216-5302-480e-9f20-03e2f51727f9", "fab819c1-6b13-4fbf-b47a-fc7000bf11f9", "b88c363f-eb37-476c-89e1-741a4c9a5c00", None),
    # Bipul Jain -> JSSG Professional Unity Indore Testing (MP0902)
    ("ccc9c869-5b66-466e-b4e3-9e8bc01d8823", "38eb5098-6352-4c33-b640-05b6b19e9fa3", "b88c363f-eb37-476c-89e1-741a4c9a5c00", "Group Admin"),
]


async def run_repair():
    async with async_session_factory() as session:
        print("=== STEP 1: Cleaning duplicate rows in member_group_memberships ===")
        dup_res = await session.execute(
            text(
                """
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
                )
                RETURNING id, member_id, group_id;
                """
            )
        )
        deleted_dups = dup_res.all()
        print(f"Removed {len(deleted_dups)} duplicate membership rows.")

        print("\n=== STEP 2: Soft-deleting dummy test member 'Test Admin' ===")
        await session.execute(
            text(
                """
                UPDATE members SET is_deleted = true, deleted_at = NOW()
                WHERE id = '50067cd9-e56a-47d6-a936-eed76bbdee00';
                """
            )
        )
        await session.execute(
            text(
                """
                UPDATE users SET is_deleted = true, deleted_at = NOW()
                WHERE id = 'bd5d298f-5188-4f08-8a5d-a23718399675';
                """
            )
        )

        print("\n=== STEP 3: Linking orphaned members to active groups ===")
        for mid_str, uid_str, gid_str, post_title in ORPHAN_MAPPINGS:
            mid = uuid.UUID(mid_str)
            uid = uuid.UUID(uid_str) if uid_str else None
            gid = uuid.UUID(gid_str)

            member = await session.get(Member, mid)
            group = await session.get(SocialGroup, gid)
            user = await session.get(User, uid) if uid else None

            if not member or not group:
                print(f"Skipping {mid_str}: member or group not found.")
                continue

            # Update User
            if user:
                user.group_id = gid
                session.add(user)

            # Update Member
            member.group_id = gid
            if post_title:
                member.is_post_holder = True
                member.post_holder_title = post_title
            session.add(member)

            # Assign Member ID & Create/Update MemberGroupMembership
            await assign_member_id(session, member, group, user_id=uid)
            print(f"Linked: {member.first_name} {member.last_name} -> {group.name} [{group.group_id}] (Member ID: {member.membership_number})")

        await session.commit()
        print("\n=== REPAIR COMPLETED SUCCESSFULLY! ===")


if __name__ == "__main__":
    asyncio.run(run_repair())

import asyncio
import aiosqlite
from bot.registration import _validate_password
from bot.profile_db import get_profile_by_id_admin

async def debug_profile(profile_id):
    db_path = '/data/subscriptions.db'
    profile = await get_profile_by_id_admin(db_path, profile_id)
    if not profile:
        print(f"Profile {profile_id} not found.")
        return
    
    print(f"Profile {profile_id}:")
    print(f"  Password: '{profile.password}'")
    print(f"  Is Valid (DB): {profile.is_valid}")
    
    errors = _validate_password(profile.password)
    print(f"  Validation Errors: {errors}")
    
    dot_check = "." in profile.password
    print(f"  Dot in password check: {dot_check}")

if __name__ == "__main__":
    asyncio.run(debug_profile(405))

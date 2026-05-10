"""
DATABASE.PY MEIN YEH METHODS ADD KARO
=======================================
Existing database.py ke end mein (clear_coverpic ke baad) yeh paste karo.

Yeh MongoDB mein ek naya field 'custompics' store karta hai:
{
  "id": user_id,
  ...
  "custompics": {
    "Naruto": "file_id_abc123",
    "One Piece": "file_id_xyz789",
    "Wistoria": "file_id_def456"
  }
}
"""

    # ─────────────────────────────────────────────
    #  Custom Pics (keyword → file_id mapping)
    # ─────────────────────────────────────────────
    async def set_custompic(self, user_id: int, keyword: str, file_id: str):
        """Ek keyword ke liye custom pic save karo."""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$set': {f'custompics.{keyword}': file_id}},
            upsert=True
        )

    async def get_custompic(self, user_id: int, keyword: str) -> str | None:
        """Ek specific keyword ki pic file_id lo."""
        user = await self._get_user(user_id)
        pics = user.get('custompics', {})
        return pics.get(keyword)

    async def get_all_custompics(self, user_id: int) -> dict:
        """User ke saare custom pics dict return karo {keyword: file_id}."""
        user = await self._get_user(user_id)
        return user.get('custompics', {})

    async def del_custompic(self, user_id: int, keyword: str):
        """Ek keyword ki custom pic delete karo."""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$unset': {f'custompics.{keyword}': ''}},
            upsert=True
        )

    async def clear_all_custompics(self, user_id: int):
        """User ke saare custom pics ek baar mein clear karo."""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$set': {'custompics': {}}},
            upsert=True
        )

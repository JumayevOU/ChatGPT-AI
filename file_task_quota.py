"""Fayl-vazifa (sandbox) kvotasi uchun "bir marta yechish, muvaffaqiyatsiz
bo'lsa qaytarish" hisob-kitobi.

GPT bitta foydalanuvchi so'rovida run_python_sandbox tool'ini bir necha
marta chaqirishi mumkin (avval faylni tekshirish, keyin o'zgartirish;
yoki xatoni tuzatib qayta urinish) — foydalanuvchi buning uchun faqat
BIR MARTA to'laydi. Fayl umuman chiqmasa, urinish bekor hisoblanadi.

Hisob ball byudjetidan ALOHIDA (config.DAILY_FILE_LIMIT_FREE) — shuning
uchun kunlik fayl limiti tugagach ham oddiy suhbat ishlashda davom etadi.
"""
from database import check_and_consume_file_quota, refund_file_quota


class FileTaskQuota:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.limit_hit = False   # chaqiruvchi shu bo'yicha chiroyli xabar chiqaradi
        self.used = 0
        self.limit = 0
        self._charged = False
        self._allowed = True
        self._unlimited = False
        self._succeeded = False

    async def ensure_charged(self) -> bool:
        """Birinchi chaqiriqda sanoqdan bittasini yechadi. Keyingi
        chaqiriqlar hech narsa qilmay, avvalgi natijani qaytaradi."""
        if not self._charged:
            result = await check_and_consume_file_quota(self.user_id)
            self._charged = True
            self._allowed = result.get("allowed", True)
            self._unlimited = result.get("unlimited", False)
            self.used = result.get("used", 0)
            self.limit = result.get("limit", 0)
            self.limit_hit = not self._allowed and not result.get("banned")
        return self._allowed

    @property
    def unlimited(self) -> bool:
        return self._unlimited

    @property
    def remaining(self) -> int:
        """Bugun yana nechta fayl yaratish mumkin (cheksiz bo'lsa -1)."""
        if self._unlimited:
            return -1
        return max(0, self.limit - self.used)

    def mark_success(self) -> None:
        self._succeeded = True

    async def refund_if_unused(self) -> None:
        """Sanoq yechilgan, lekin hech qachon muvaffaqiyatli natija
        bo'lmagan va foydalanuvchi cheksiz (premium) bo'lmasa — qaytaradi."""
        if self._charged and self._allowed and not self._succeeded and not self._unlimited:
            await refund_file_quota(self.user_id)
            self.used = max(0, self.used - 1)

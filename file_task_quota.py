"""Fayl-vazifa (sandbox) kvotasi uchun "bir marta yechish, muvaffaqiyatsiz
bo'lsa qaytarish" hisob-kitobi.

GPT bitta foydalanuvchi so'rovida run_python_sandbox tool'ini bir necha
marta chaqirishi mumkin (avval faylni tekshirish, keyin o'zgartirish;
yoki xatoni tuzatib qayta urinish) — foydalanuvchi buning uchun faqat
BIR MARTA to'laydi.
"""
from database import check_and_consume_quota, refund_quota


class FileTaskQuota:
    def __init__(self, user_id: int, cost: int):
        self.user_id = user_id
        self.cost = cost
        self._charged = False
        self._allowed = True
        self._unlimited = False
        self._succeeded = False

    async def ensure_charged(self) -> bool:
        """Birinchi chaqiriqda ball yechadi. Keyingi chaqiriqlar hech narsa
        qilmay, avvalgi natijani qaytaradi. Ruxsat bo'lsa True."""
        if not self._charged:
            result = await check_and_consume_quota(self.user_id, self.cost)
            self._charged = True
            self._allowed = result.get("allowed", True)
            self._unlimited = result.get("unlimited", False)
        return self._allowed

    def mark_success(self) -> None:
        self._succeeded = True

    async def refund_if_unused(self) -> None:
        """Ball yechilgan, lekin hech qachon muvaffaqiyatli natija bo'lmagan
        va foydalanuvchi cheksiz (premium) bo'lmasa — qaytaradi."""
        if self._charged and self._allowed and not self._succeeded and not self._unlimited:
            await refund_quota(self.user_id, self.cost)

"""Kunlik sanoqlar uchun "bir marta yechish, natija chiqmasa qaytarish"
hisob-kitobi (fayl yaratish, rasm chizish, chuqur tadqiqot).

GPT bitta foydalanuvchi so'rovida bir xil tool'ni bir necha marta
chaqirishi mumkin (avval faylni tekshirish, keyin o'zgartirish; yoki
xatoni tuzatib qayta urinish) — foydalanuvchi buning uchun faqat BIR
MARTA to'laydi. Natija umuman chiqmasa, urinish bekor hisoblanadi.

Hisob ball byudjetidan ALOHIDA (core.config.PLAN_LIMITS) — shuning uchun
kunlik limit tugagach ham oddiy suhbat ishlashda davom etadi.
"""
from db.database import check_and_consume_daily, refund_daily


class DailyQuota:
    """Bitta kunlik sanoq turi uchun hisob (kind: files / images / research)."""

    def __init__(self, user_id: int, kind: str = "files"):
        self.user_id = user_id
        self.kind = kind
        self.limit_hit = False   # chaqiruvchi shu bo'yicha chiroyli xabar chiqaradi
        self.used = 0
        self.limit = 0
        self.plan = "free"       # chaqiruvchi shunga qarab upsell ko'rsatadi
        self._charged = False
        self._allowed = True
        self._unlimited = False
        self._succeeded = False

    async def ensure_charged(self) -> bool:
        """Birinchi chaqiriqda sanoqdan bittasini yechadi. Keyingi
        chaqiriqlar hech narsa qilmay, avvalgi natijani qaytaradi."""
        if not self._charged:
            result = await check_and_consume_daily(self.user_id, self.kind)
            self._charged = True
            self._allowed = result.get("allowed", True)
            self._unlimited = result.get("unlimited", False)
            self.used = result.get("used", 0)
            self.limit = result.get("limit", 0)
            self.plan = result.get("plan", "free")
            self.limit_hit = not self._allowed and not result.get("banned")
        return self._allowed

    @property
    def charged(self) -> bool:
        """Bu sanoq umuman ishlatilganmi.

        Chaqiruvchi uchun MUHIM: bitta so'rovda bir necha xil tool ishlashi
        mumkin (masalan faqat rasm chizilib, fayl yaratilmasligi). Tegilmagan
        sanoq bo'yicha limit xabari chiqarish noto'g'ri bo'lardi.
        """
        return self._charged

    @property
    def unlimited(self) -> bool:
        return self._unlimited

    @property
    def remaining(self) -> int:
        """Bugun yana nechta amal qilish mumkin (cheksiz bo'lsa -1)."""
        if self._unlimited:
            return -1
        return max(0, self.limit - self.used)

    def mark_success(self) -> None:
        self._succeeded = True

    async def refund_if_unused(self) -> None:
        """Sanoq yechilgan, lekin hech qachon muvaffaqiyatli natija
        bo'lmagan va foydalanuvchi cheksiz (premium) bo'lmasa — qaytaradi."""
        if self._charged and self._allowed and not self._succeeded and not self._unlimited:
            await refund_daily(self.user_id, self.kind)
            self.used = max(0, self.used - 1)


class FileTaskQuota(DailyQuota):
    """Eski nom — mavjud chaqiruvchilar buzilmasin."""

    def __init__(self, user_id: int):
        super().__init__(user_id, "files")

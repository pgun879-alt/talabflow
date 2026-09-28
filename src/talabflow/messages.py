"""Customer-facing message templates, in English and Arabic.

Kept in one module so the bot's voice can be reviewed and changed without touching the state
machine, and so both languages stay in step. This is a template table, not a full i18n
framework -- ``gettext`` and translation catalogues would be more machinery than two languages
of short prompts justify.

Every template that echoes customer input is plain text, and the Telegram transport sends
without ``parse_mode`` for the same reason: customer-supplied text must never be interpreted as
markup.
"""

from __future__ import annotations

from typing import Final, Literal

Language = Literal["en", "ar"]

_TEMPLATES: Final[dict[str, dict[Language, str]]] = {
    "welcome": {
        "en": (
            "Welcome to {business}.\n\n"
            "I can take a new order and tell you the status of an existing one.\n\n"
            "  /new — place a new order\n"
            "  /status <reference> — check an order\n"
            "  /cancel — abandon the order being entered\n"
            "  /help — show this again"
        ),
        "ar": (
            "مرحبًا بك في {business}.\n\n"
            "أستطيع تسجيل طلب جديد وإخبارك بحالة طلب قائم.\n\n"
            "  /new — تسجيل طلب جديد\n"
            "  /status <الرقم المرجعي> — الاستعلام عن طلب\n"
            "  /cancel — إلغاء الطلب الجاري إدخاله\n"
            "  /help — إظهار هذه القائمة"
        ),
    },
    "ask_service": {
        "en": "What do you need? Reply with the number:\n{options}",
        "ar": "ما الخدمة التي تحتاجها؟ أرسل الرقم:\n{options}",
    },
    "invalid_service": {
        "en": "Please reply with one of the numbers listed:\n{options}",
        "ar": "الرجاء إرسال أحد الأرقام المذكورة:\n{options}",
    },
    "ask_details": {
        "en": "Describe what you need for {service}, in a sentence or two.",
        "ar": "اشرح لي ما تحتاجه بخصوص {service} في سطر أو سطرين.",
    },
    "details_too_short": {
        "en": "Could you give a little more detail? At least {minimum} characters.",
        "ar": "هل يمكنك إعطاء تفاصيل أكثر؟ {minimum} حرفًا على الأقل.",
    },
    "ask_phone": {
        "en": "What phone number should we call you on?",
        "ar": "ما رقم الهاتف الذي نتصل بك عليه؟",
    },
    "invalid_phone": {
        "en": "That does not look like a phone number. Please send digits only, 8 to 15 of them.",
        "ar": "هذا لا يبدو رقم هاتف. أرسل أرقامًا فقط، من ٨ إلى ١٥ رقمًا.",
    },
    "ask_address": {
        "en": "What is the address?",
        "ar": "ما هو العنوان؟",
    },
    "confirm": {
        "en": (
            "Please check this before I save it:\n\n"
            "  Service: {service}\n"
            "  Details: {details}\n"
            "  Phone:   {phone}\n"
            "  Address: {address}\n\n"
            "Reply YES to confirm, or /cancel to start over."
        ),
        "ar": (
            "راجع البيانات قبل الحفظ:\n\n"
            "  الخدمة: {service}\n"
            "  التفاصيل: {details}\n"
            "  الهاتف: {phone}\n"
            "  العنوان: {address}\n\n"
            "أرسل «نعم» للتأكيد، أو /cancel للبدء من جديد."
        ),
    },
    "confirm_unclear": {
        "en": "Reply YES to confirm, or /cancel to start over.",
        "ar": "أرسل «نعم» للتأكيد، أو /cancel للبدء من جديد.",
    },
    "order_created": {
        "en": (
            "Your order is registered.\n\n"
            "  Reference: {reference}\n"
            "  Status:    {status}\n\n"
            "Keep the reference — check progress any time with /status {reference}."
        ),
        "ar": (
            "تم تسجيل طلبك.\n\n"
            "  الرقم المرجعي: {reference}\n"
            "  الحالة: {status}\n\n"
            "احفظ الرقم المرجعي — يمكنك متابعة الطلب في أي وقت بإرسال /status {reference}"
        ),
    },
    "cancelled_draft": {
        "en": "The order you were entering has been discarded. Send /new to start again.",
        "ar": "تم إلغاء الطلب الذي كنت تُدخله. أرسل /new للبدء من جديد.",
    },
    "nothing_to_cancel": {
        "en": "There is no order in progress. Send /new to place one.",
        "ar": "لا يوجد طلب جارٍ. أرسل /new لتسجيل طلب.",
    },
    "status_result": {
        "en": (
            "Order {reference}\n  Service: {service}\n  Status:  {status}\n  Placed:  {created}"
        ),
        "ar": (
            "الطلب {reference}\n  الخدمة: {service}\n  الحالة: {status}\n  تاريخ التسجيل: {created}"
        ),
    },
    "status_usage": {
        "en": "Send the reference too, like this: /status TF-20260928-A1B2",
        "ar": "أرسل الرقم المرجعي أيضًا، مثل: /status TF-20260928-A1B2",
    },
    "status_not_found": {
        "en": "I could not find an order with that reference under your account.",
        "ar": "لم أجد طلبًا بهذا الرقم المرجعي في حسابك.",
    },
    "status_changed": {
        "en": "Update on order {reference}: the status is now {status}.{note}",
        "ar": "تحديث بخصوص الطلب {reference}: الحالة الآن {status}.{note}",
    },
    "unknown_command": {
        "en": "I did not understand that. Send /help to see what I can do.",
        "ar": "لم أفهم ذلك. أرسل /help لمعرفة ما أستطيع فعله.",
    },
    "too_long": {
        "en": "That message is too long. Please keep it under {limit} characters.",
        "ar": "الرسالة طويلة جدًا. الرجاء إبقاؤها أقل من {limit} حرفًا.",
    },
    "rate_limited": {
        "en": "You are sending messages very quickly. Please wait a moment and try again.",
        "ar": "أنت ترسل الرسائل بسرعة كبيرة. انتظر قليلًا ثم أعد المحاولة.",
    },
    "blocked": {
        "en": "This account cannot place orders. Please contact us by phone.",
        "ar": "لا يمكن لهذا الحساب تسجيل طلبات. الرجاء التواصل معنا هاتفيًا.",
    },
}

#: Human-readable status names shown to customers, so they never see a raw enum value.
STATUS_LABELS: Final[dict[str, dict[Language, str]]] = {
    "new": {"en": "received", "ar": "تم الاستلام"},
    "confirmed": {"en": "confirmed", "ar": "مؤكَّد"},
    "in_progress": {"en": "in progress", "ar": "قيد التنفيذ"},
    "ready": {"en": "ready", "ar": "جاهز"},
    "completed": {"en": "completed", "ar": "مكتمل"},
    "cancelled": {"en": "cancelled", "ar": "ملغى"},
}


def render(key: str, language: Language = "en", **values: object) -> str:
    """Render the template ``key`` in ``language``.

    Raises:
        KeyError: if the key does not exist -- a missing template is a bug, and failing loudly
            in a test beats shipping an empty message to a customer.
    """
    variants = _TEMPLATES[key]
    template = variants.get(language) or variants["en"]
    return template.format(**values)


def status_label(status: str, language: Language = "en") -> str:
    """Customer-facing name for a status value, falling back to the raw value."""
    return STATUS_LABELS.get(status, {}).get(language, status)


def format_service_options(services: tuple[str, ...]) -> str:
    """Render the numbered service menu."""
    return "\n".join(f"  {index}. {name}" for index, name in enumerate(services, start=1))

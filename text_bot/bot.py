import os
from pathlib import Path
from dotenv import load_dotenv
import re
import io
import time
import uuid
import json
import base64
import hmac
import hashlib
import asyncio
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from user_agent import generate_user_agent
from PIL import Image  # إضافة لقراءة أبعاد الصور

import discord
from discord import app_commands
from discord.ext import commands

# ============================================================
# الإعدادات العامة — معزولة داخل مجلد text_bot
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "656783724662226963"))
TEMP_API_URL = os.getenv("TEMP_API_URL", "").rstrip("/")
BASE_QWEN_URL = os.getenv("BASE_QWEN_URL", "https://chat.qwen.ai/api/v2").rstrip("/")
BOT_VERSION = os.getenv("BOT_VERSION", "2.5.0")
GDRIVE_API_KEY = os.getenv("GDRIVE_API_KEY", "").strip()

MAX_IMAGES_PER_REQUEST = int(os.getenv("MAX_IMAGES_PER_REQUEST", "10"))
MAX_IMAGE_SIZE_MB = int(os.getenv("MAX_IMAGE_SIZE_MB", "8"))

# عدد المحاولات لكل صورة عند الفشل
MAX_RETRY_PER_IMAGE = int(os.getenv("MAX_RETRY_PER_IMAGE", "3"))

# المعالجة المتوازية — عدد الصفحات التي تُعالج في نفس اللحظة
EXTRACTION_CONCURRENCY = max(1, int(os.getenv("EXTRACTION_CONCURRENCY", "2")))

# التقسيم الذكي للصور الشريطية الطويلة (فصول المانهوا المدموجة عموديًا)
# لا تقسيم استباقي أبدًا: الصورة تُجرّب كاملة أولًا في الوضعين،
# وعند الفشل بسبب الطول فقط تُقسّم إلى أجزاء متساوية بحد أقصى TILE_MAX_PARTS
# مع تداخل TILE_OVERLAP حتى لا تنقطع فقاعة حوار عند الحدود.
TILE_MAX_EDGE = int(os.getenv("TILE_MAX_EDGE", "6000"))         # عتبة اعتبار الصورة شريطًا طويلًا
TILE_HEIGHT = int(os.getenv("TILE_HEIGHT", "3200"))            # الارتفاع المستهدف للجزء الواحد
TILE_MAX_PARTS = max(2, int(os.getenv("TILE_MAX_PARTS", "3")))  # أقصى عدد أجزاء للصورة كاملة
TILE_OVERLAP = int(os.getenv("TILE_OVERLAP", "320"))
TILE_MAX_RETRIES = int(os.getenv("TILE_MAX_RETRIES", "2"))
# الجزء الذي ما زال طويلًا وفشل وحده يُعاد تقسيمه وحده مرة واحدة (ضمان نادر للصفحات العملاقة)
TILE_SUB_SPLIT_DEPTH = max(0, int(os.getenv("TILE_SUB_SPLIT_DEPTH", "1")))

# المراجعة الآلية بعد الترجمة — أي مقاطع أجنبية متبقية (إنجليزي/كوري/صيني...)
# في النص المُترجم تُكتشف آليًا وتُترجم وتُلصق مكانها، وحروف التصنيف (A, S, SS, C) تبقى كما هي.
# نص الاستخراج نفسه يبقى دائمًا باللغة الأصلية كما في الصورة — لا تُمس إطلاقًا.
FOREIGN_REVIEW_ENABLED = os.getenv("FOREIGN_REVIEW_ENABLED", "1") == "1"

# الترجمة المجزأة — الفصول الطويلة تُترجم على مجموعات حتى لا ينقطع الناتج
TRANSLATION_CHUNK_LINES = int(os.getenv("TRANSLATION_CHUNK_LINES", "60"))
TRANSLATION_CHUNK_CHARS = int(os.getenv("TRANSLATION_CHUNK_CHARS", "9000"))

from utils.storage import (
    admin_adjust_user,
    admin_adjust_server,
    close_mongo,
    consume_point,
    consume_server_point,
    get_user_profile,
    list_user_profiles,
    load_accounts_data,
    save_accounts_data,
    server_has_points,
    update_user_settings,
)
from utils.emojis import THEMES, emoji_manager, emojize, themed_embed

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

# ============================================================
# طبقة مركزية لاستبدال placeholders داخل رسائل discord.py
# ============================================================
def _emojize_discord_value(value):
    if isinstance(value, discord.Embed):
        return discord.Embed.from_dict(emojize(value.to_dict()))
    if isinstance(value, list):
        return [_emojize_discord_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_emojize_discord_value(item) for item in value)
    return emojize(value)


def _format_send_kwargs(args, kwargs):
    args = tuple(_emojize_discord_value(arg) for arg in args)
    kwargs = {key: _emojize_discord_value(value) for key, value in kwargs.items()}
    return args, kwargs

_original_response_send_message = discord.InteractionResponse.send_message
async def _patched_response_send_message(self, *args, **kwargs):
    args, kwargs = _format_send_kwargs(args, kwargs)
    return await _original_response_send_message(self, *args, **kwargs)
discord.InteractionResponse.send_message = _patched_response_send_message

_original_followup_send = discord.Webhook.send
async def _patched_followup_send(self, *args, **kwargs):
    args, kwargs = _format_send_kwargs(args, kwargs)
    return await _original_followup_send(self, *args, **kwargs)
discord.Webhook.send = _patched_followup_send

_original_messageable_send = discord.abc.Messageable.send
async def _patched_messageable_send(self, *args, **kwargs):
    args, kwargs = _format_send_kwargs(args, kwargs)
    return await _original_messageable_send(self, *args, **kwargs)
discord.abc.Messageable.send = _patched_messageable_send

_original_response_edit_message = discord.InteractionResponse.edit_message
async def _patched_response_edit_message(self, *args, **kwargs):
    args, kwargs = _format_send_kwargs(args, kwargs)
    return await _original_response_edit_message(self, *args, **kwargs)
discord.InteractionResponse.edit_message = _patched_response_edit_message

_original_message_edit = discord.Message.edit
async def _patched_message_edit(self, *args, **kwargs):
    args, kwargs = _format_send_kwargs(args, kwargs)
    return await _original_message_edit(self, *args, **kwargs)
discord.Message.edit = _patched_message_edit


# ============================================================
# عرض موحد للرسائل — Emojis + Markdown منسق
# ============================================================
RULE = "\n\n---\n\n"
V2_SUPPORTED = all(hasattr(discord.ui, name) for name in ("LayoutView", "Container", "TextDisplay", "Separator", "File", "ActionRow"))
V2_SEPARATOR_SUPPORTED = V2_SUPPORTED
SUPPORT_SERVER_URL = os.getenv("SUPPORT_SERVER_URL", "").strip()

def line():
    return RULE

def fmt_bool(value):
    return "مفعّل" if value else "معطّل"


# ============================================================
# تعقيم رسائل الأخطاء — يخفي أي أثر للأدوات الداخلية عن المستخدمين
# ============================================================
_ERROR_REDACTIONS = [
    (re.compile(r"https?://\S+", re.I), "رابط خارجي"),
    (re.compile(r"\bqwen[\w.-]*", re.I), "محرك المعالجة"),
    (re.compile(r"\bocr\b", re.I), "المعالجة"),
    (re.compile(r"\btokens?\b", re.I), "بيانات الاتصال"),
    (re.compile(r"\bBearer\s+\S+", re.I), "بيانات الاتصال"),
    (re.compile(r"\bAI\b"), "النظام"),
    (re.compile(r"الذكاء\s*الاصطناعي"), "محرك المعالجة"),
    (re.compile(r"ذكاء\s*اصطناعي"), "محرك المعالجة"),
]


def sanitize_error(value):
    """ينظّف نصوص الأخطاء المعروضة للمستخدمين من أي تفاصيل تقنية داخلية."""
    text = str(value or "")
    for pattern, repl in _ERROR_REDACTIONS:
        text = pattern.sub(repl, text)
    return text.strip() or "خطأ غير معروف"


def status_embed(title, description, *, error=False):
    return themed_embed(title=title, description=description, color_name="red" if error else "gold")


def output_separator(settings, idx, image_name):
    """فاصل الصفحات داخل الملف — ثابت دائمًا على اسم الصورة الأصلية."""
    safe_name = Path(image_name or f"صورة {idx}").name
    return f"\n\n{line()}\n## {safe_name}\n{line()}\n\n"


def format_duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    if minutes:
        return f"{minutes}د {seconds}ث"
    return f"{seconds}ث"

def _split_visual_rules(text):
    return [part.strip() for part in emojize(text or "").split(RULE) if part.strip()]

def components_v2_panel(title=None, description=None, *, sections=None, error=False):
    """Build a Components V2 panel only for messages that really need visual sections."""
    if not V2_SEPARATOR_SUPPORTED:
        return None
    container = discord.ui.Container(accent_colour=discord.Colour.red() if error else discord.Colour(THEMES["gold"]["color"]))
    if title:
        container.add_item(discord.ui.TextDisplay(emojize(title)))
    parts = [emojize(part).strip() for part in sections] if sections is not None else _split_visual_rules(description)
    for idx, part in enumerate([p for p in parts if p]):
        if idx > 0:
            container.add_item(discord.ui.Separator(visible=True))
        container.add_item(discord.ui.TextDisplay(part))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view

async def send_panel(destination, title=None, description=None, *, sections=None, content=None, file=None, view=None, ephemeral=False, error=False):
    panel_view = components_v2_panel(title, description, sections=sections, error=error)
    kwargs = {"content": emojize(content) if content else None}
    if file is not None:
        kwargs["file"] = file
    if ephemeral:
        kwargs["ephemeral"] = True
    if panel_view and view is None and file is None:
        kwargs["view"] = panel_view
        return await destination.send(**kwargs)
    embed = themed_embed(title=title, description=description or ("\n\n".join(sections or [])), color_name="red" if error else "gold")
    kwargs.update({"embed": embed, "view": view})
    return await destination.send(**kwargs)


# ============================================================
# لوحة النتيجة النهائية — Components V2 مع الملف داخل الرسالة
# ============================================================
def result_panel_view(*, user_id, total_images, empty_images, duration_text, format_label,
                      points_label, points_after, main_name, translation=None, warning=None):
    """
    تبني لوحة نتيجة الاستخراج النهائية بنظام Components V2:
    - عبارة (تم استخراج الفصل) مرة واحدة فقط
    - المنشن داخل الرسالة نفسها
    - تقرير الجودة على ثلاثة أسطر
    - الملفات مرفوعة داخل الرسالة عبر مكوّن File
    - فواصل مرئية وتنسيق ماركداون ديسكورد فاخر
    """
    if not V2_SUPPORTED:
        return None
    container = discord.ui.Container(accent_colour=discord.Colour(THEMES["gold"]["color"]))

    # العنوان — مرة واحدة فقط
    container.add_item(discord.ui.TextDisplay(emojize("## {emoji:circlecheck} تم استخراج الفصل")))
    container.add_item(discord.ui.Separator(visible=True))

    # المنشن داخل الرسالة
    container.add_item(discord.ui.TextDisplay(emojize(
        f"**{emoji_manager.placeholder('user')} المستخدم:** <@{user_id}>"
    )))
    container.add_item(discord.ui.Separator(visible=True))

    # تقرير الجودة على ثلاثة أسطر
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('chartpie')} تقرير الجودة\n"
        f"> {emoji_manager.placeholder('photo')} **الصور الفارغة:** `{empty_images}` من إجمالي `{total_images}` صورة\n"
        f"> {emoji_manager.placeholder('clock')} **مدة المعالجة:** `{duration_text}`\n"
        f"> {emoji_manager.placeholder('list')} **صيغة الناتج:** `{format_label}`"
    )))
    container.add_item(discord.ui.Separator(visible=True))

    # الرصيد
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('star')} النقاط\n"
        f"> تم خصم `1` نقطة من **{points_label}** — المتبقي: `{points_after}`"
    )))

    # تحذير إن وجد (فشل الترجمة أو صفحات تعذّرت قراءتها)
    if warning:
        container.add_item(discord.ui.Separator(visible=True))
        container.add_item(discord.ui.TextDisplay(emojize(
            f"### {emoji_manager.placeholder('alerttriangle')} تنبيه\n> {warning.replace(chr(10), chr(10) + '> ')}"
        )))

    container.add_item(discord.ui.Separator(visible=True))

    # الملفات داخل الرسالة
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('folderopen')} الملفات\n"
        f"> {emoji_manager.placeholder('list')} **النص المستخرج (اللغة الأصلية):** `{main_name}`"
    )))
    container.add_item(discord.ui.File(f"attachment://{main_name}"))
    if translation:
        container.add_item(discord.ui.TextDisplay(emojize(
            f"> {emoji_manager.placeholder('message')} **الترجمة الأدبية ({translation['target_label']}):** `{translation['filename']}`"
        )))
        container.add_item(discord.ui.File(f"attachment://{translation['filename']}"))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


async def deliver_extraction_result(channel, user_id, *, combined_text, translated_text, translated_error,
                                    settings, source_name, output_format, total_images, empty_images,
                                    extraction_started_at, profile_after, point_source, failed_pages=None):
    """يكتب الملفات ويرسل لوحة النتيجة النهائية مع كل الملفات داخلها."""
    output_dir = BASE_DIR / "data"
    output_dir.mkdir(parents=True, exist_ok=True)

    duration_text = format_duration(time.monotonic() - extraction_started_at)
    format_label = str(output_format).upper()
    points_label = "رصيد السيرفر" if point_source == "server" else "رصيدك"
    points_after = profile_after.get("points", 0)
    target_label = TRANSLATION_TARGETS.get(settings.get("translate_target", "ar"), "العربية")

    main_path = unique_output_path(output_dir, source_name, output_format, ascii_safe=True)
    if output_format == "docx":
        main_path.write_bytes(make_docx_bytes(combined_text).getvalue())
    else:
        main_path.write_text(combined_text, encoding="utf-8")

    translated_path = None
    if translated_text:
        translated_path = unique_output_path(output_dir, f"{source_name}_translated", output_format, ascii_safe=True)
        if output_format == "docx":
            translated_path.write_bytes(make_docx_bytes(translated_text).getvalue())
        else:
            translated_path.write_text(translated_text, encoding="utf-8")

    warning = None
    if translated_error:
        warning = f"تعذّرت الترجمة الأدبية وسأرسل النص الأصلي فقط — السبب: `{sanitize_error(translated_error)}`"
    if failed_pages:
        failed_note = "تعذّرت قراءة صفحة واحدة" if len(failed_pages) == 1 else f"تعذّرت قراءة `{len(failed_pages)}` صفحة"
        failed_names = ", ".join(f"`{name}`" for name in failed_pages[:5])
        extra_line = f"{failed_note} حتى بعد إعادة المحاولة والتقسيم الذكي: {failed_names} — أعد استخراج هذه الصفحات إن أردت"
        warning = f"{warning}\n{extra_line}" if warning else extra_line

    translation_info = None
    if translated_path:
        translation_info = {"filename": translated_path.name, "target_label": target_label}

    sent = None
    if V2_SUPPORTED:
        try:
            view = result_panel_view(
                user_id=user_id,
                total_images=total_images,
                empty_images=empty_images,
                duration_text=duration_text,
                format_label=format_label,
                points_label=points_label,
                points_after=points_after,
                main_name=main_path.name,
                translation=translation_info,
                warning=warning,
            )
            files = [discord.File(str(main_path))]
            if translated_path:
                files.append(discord.File(str(translated_path)))
            sent = await channel.send(view=view, files=files)
        except Exception as exc:
            print(f"[Result] فشل إرسال لوحة Components V2، سيتم استخدام البديل: {exc}")
            sent = None

    if sent is None:
        # بديل احتياطي — إعادة إنشاء الملفات لأنها قد تكون استُهلكت في المحاولة الأولى
        files = [discord.File(str(main_path))]
        if translated_path:
            files.append(discord.File(str(translated_path)))
        sections = [
            f"## {emoji_manager.placeholder('circlecheck')} تم استخراج الفصل",
            f"{emoji_manager.placeholder('user')} **المستخدم:** <@{user_id}>",
            f"{emoji_manager.placeholder('chartpie')} **تقرير الجودة:**\n"
            f"> الصور الفارغة: `{empty_images}` من `{total_images}`\n"
            f"> مدة المعالجة: `{duration_text}`\n"
            f"> صيغة الناتج: `{format_label}`",
            f"{emoji_manager.placeholder('star')} تم خصم نقطة واحدة من {points_label}. المتبقي: `{points_after}`.",
        ]
        if warning:
            sections.append(f"{emoji_manager.placeholder('alerttriangle')} {warning}")
        embed = themed_embed(description="\n\n".join(sections))
        sent = await channel.send(embed=embed, files=files)

    try:
        os.remove(main_path)
    except OSError:
        pass
    if translated_path:
        try:
            os.remove(translated_path)
        except OSError:
            pass
    return sent


# ============================================================
# لوحة نتيجة الترجمة المستقلة — Components V2
# ============================================================
def translation_panel_view(*, user_id, target_label, lines_count, duration_text, format_label,
                           points_label, points_after, file_name):
    if not V2_SUPPORTED:
        return None
    container = discord.ui.Container(accent_colour=discord.Colour(THEMES["gold"]["color"]))
    container.add_item(discord.ui.TextDisplay(emojize("## {emoji:message} تمت ترجمة الفصل")))
    container.add_item(discord.ui.Separator(visible=True))
    container.add_item(discord.ui.TextDisplay(emojize(
        f"**{emoji_manager.placeholder('user')} المستخدم:** <@{user_id}>"
    )))
    container.add_item(discord.ui.Separator(visible=True))
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('infocircle')} تفاصيل الترجمة\n"
        f"> {emoji_manager.placeholder('message')} **لغة الترجمة:** `{target_label}`\n"
        f"> {emoji_manager.placeholder('list')} **عدد الأسطر:** `{lines_count}`\n"
        f"> {emoji_manager.placeholder('clock')} **مدة الترجمة:** `{duration_text}`\n"
        f"> {emoji_manager.placeholder('settings')} **صيغة الملف:** `{format_label}`"
    )))
    container.add_item(discord.ui.Separator(visible=True))
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('star')} النقاط\n"
        f"> تم خصم `1` نقطة من **{points_label}** — المتبقي: `{points_after}`"
    )))
    container.add_item(discord.ui.Separator(visible=True))
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('folderopen')} الملف المترجم\n"
        f"> {emoji_manager.placeholder('list')} `{file_name}`"
    )))
    container.add_item(discord.ui.File(f"attachment://{file_name}"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


async def deliver_translation_result(channel, user_id, *, translated_text, target_label, source_stem,
                                     output_format, lines_count, translation_started_at,
                                     profile_after, point_source):
    """يكتب الملف المترجم ويرسل لوحة النتيجة النهائية مع الملف داخلها."""
    output_dir = BASE_DIR / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    duration_text = format_duration(time.monotonic() - translation_started_at)
    format_label = str(output_format).upper()
    points_label = "رصيد السيرفر" if point_source == "server" else "رصيدك"
    points_after = profile_after.get("points", 0)

    out_path = unique_output_path(output_dir, f"{source_stem}_translated", output_format, ascii_safe=True)
    if output_format == "docx":
        out_path.write_bytes(make_docx_bytes(translated_text).getvalue())
    else:
        out_path.write_text(translated_text, encoding="utf-8")

    sent = None
    if V2_SUPPORTED:
        try:
            view = translation_panel_view(
                user_id=user_id,
                target_label=target_label,
                lines_count=lines_count,
                duration_text=duration_text,
                format_label=format_label,
                points_label=points_label,
                points_after=points_after,
                file_name=out_path.name,
            )
            sent = await channel.send(view=view, files=[discord.File(str(out_path))])
        except Exception as exc:
            print(f"[Translate] فشل إرسال لوحة Components V2، سيتم استخدام البديل: {exc}")
            sent = None

    if sent is None:
        sections = [
            f"## {emoji_manager.placeholder('message')} تمت ترجمة الفصل",
            f"{emoji_manager.placeholder('user')} **المستخدم:** <@{user_id}>",
            f"{emoji_manager.placeholder('infocircle')} **اللغة:** `{target_label}` • **المدة:** `{duration_text}` • **الصيغة:** `{format_label}`",
            f"{emoji_manager.placeholder('star')} تم خصم نقطة واحدة من {points_label}. المتبقي: `{points_after}`.",
        ]
        embed = themed_embed(description="\n\n".join(sections))
        sent = await channel.send(embed=embed, file=discord.File(str(out_path)))

    try:
        os.remove(out_path)
    except OSError:
        pass
    return sent


async def run_translation_with_status(status_msg, channel, user_id, guild_id, chapter_text,
                                      source_stem, target, output_format, *, thinking_enabled=False):
    """المنطق المشترك لترجمة فصل جاهز — يُستخدم في /translate و !ترجمة مع دعم وضعي المعالجة."""
    target_label = TRANSLATION_TARGETS.get(target, TRANSLATION_TARGETS["ar"])
    lines_count = len([ln for ln in chapter_text.splitlines() if ln.strip()])
    mode_label = "دقة كاملة" if thinking_enabled else "سرعة عالية"
    await edit_status(
        status_msg,
        "جاري الترجمة الأدبية",
        f"الوضع: `{mode_label}` — لغة الترجمة: `{target_label}` — الأسطر: `{lines_count}`.",
    )
    started_at = time.monotonic()
    loop = asyncio.get_running_loop()

    def progress_cb(part, total):
        if total > 1:
            asyncio.run_coroutine_threadsafe(
                edit_status(
                    status_msg,
                    "جاري الترجمة الأدبية",
                    f"الوضع: `{mode_label}` — الجزء `{part}` من `{total}` — لغة الترجمة: `{target_label}`.",
                ),
                loop,
            )

    try:
        translated = await asyncio.to_thread(
            translate_chapter_text,
            user_id,
            chapter_text,
            {"translate_target": target},
            thinking_enabled=thinking_enabled,
            progress_cb=progress_cb,
        )
    except Exception as exc:
        await edit_status(status_msg, "{emoji:circlex} فشلت الترجمة", str(exc), error=True)
        return
    ok, profile_after, point_source = await resolve_and_consume_point(user_id, guild_id)
    if not ok:
        await edit_status(status_msg, "{emoji:circlex} لا توجد نقاط", "لا توجد نقاط كافية لإرسال النتيجة.", error=True)
        return
    await status_msg.close(delete=True)
    await deliver_translation_result(
        channel,
        user_id,
        translated_text=translated,
        target_label=target_label,
        source_stem=source_stem,
        output_format=output_format,
        lines_count=lines_count,
        translation_started_at=started_at,
        profile_after=profile_after,
        point_source=point_source,
    )


STATUS_HEARTBEAT_SECONDS = 20


def status_sections(stage, detail=None, *, current=None, total=None, heartbeat=0):
    progress = f"`{current}/{total}`" if current is not None and total else "`...`"
    pulse = "·" * ((heartbeat % 3) + 1)
    sections = [
        f"{emoji_manager.placeholder('clock')} **الحالة:** {emojize(stage)}",
        f"{emoji_manager.placeholder('chartpie')} **التقدم:** {progress} {pulse}",
        f"{emoji_manager.placeholder('infocircle')} {emojize(detail) if detail else 'سأبقي هذه اللوحة محدثة أثناء التحميل والاستخراج.'}",
    ]
    return [emojize(section) for section in sections]


def status_view(stage, detail=None, *, error=False, current=None, total=None, heartbeat=0):
    return components_v2_panel(
        title=None,
        sections=status_sections(stage, detail, current=current, total=total, heartbeat=heartbeat),
        error=error,
    )


class ExtractionStatus:
    def __init__(self, channel, title, description=None, *, error=False, current=None, total=None):
        self.channel = channel
        self.message = None
        self.title = title
        self.description = description
        self.error = error
        self.current = current
        self.total = total
        self.heartbeat = 0
        self._closed = False
        self._task = None
        self._lock = asyncio.Lock()

    def view(self):
        return status_view(self.title, self.description, error=self.error, current=self.current, total=self.total, heartbeat=self.heartbeat)

    def fallback_embed(self):
        return status_embed(
            None,
            "\n".join(status_sections(self.title, self.description, current=self.current, total=self.total, heartbeat=self.heartbeat)),
            error=self.error,
        )

    async def start(self):
        view = self.view()
        if view:
            self.message = await self.channel.send(view=view)
        else:
            self.message = await self.channel.send(embed=self.fallback_embed())
        self._task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def _heartbeat_loop(self):
        while not self._closed:
            await asyncio.sleep(STATUS_HEARTBEAT_SECONDS)
            if self._closed:
                break
            self.heartbeat += 1
            await self._safe_update()

    async def update(self, title, description=None, *, error=False, current=None, total=None):
        self.title = title
        self.description = sanitize_error(description) if error else description
        self.error = error
        self.current = current
        self.total = total
        self.heartbeat += 1
        await self._safe_update()

    async def _safe_update(self):
        async with self._lock:
            view = self.view()
            try:
                if self.message:
                    if view:
                        await self.message.edit(content=None, embeds=[], attachments=[], view=view)
                    else:
                        await self.message.edit(content=None, embeds=[self.fallback_embed()], attachments=[], view=None)
                    return
            except (discord.NotFound, discord.Forbidden):
                self.message = None
            except discord.HTTPException as exc:
                print(f"[ExtractionStatus] edit failed, sending replacement: {exc}")
                self.message = None
            try:
                if view:
                    self.message = await self.channel.send(view=view)
                else:
                    self.message = await self.channel.send(embed=self.fallback_embed())
            except discord.HTTPException as exc:
                print(f"[ExtractionStatus] replacement send failed: {exc}")

    async def close(self, *, delete=False):
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if delete and self.message:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


async def send_status(channel, title, description=None, *, error=False, current=None, total=None):
    return await ExtractionStatus(channel, title, description, error=error, current=current, total=total).start()


async def edit_status(status, title, description=None, *, error=False, current=None, total=None):
    if error:
        description = sanitize_error(description)
    if isinstance(status, ExtractionStatus):
        await status.update(title, description, error=error, current=current, total=total)
        if error:
            await status.close()
        return
    view = status_view(title, description, error=error, current=current, total=total)
    if view:
        await status.edit(content=None, embeds=[], attachments=[], view=view)
    else:
        await status.edit(content=None, embed=status_embed(None, "\n".join(status_sections(title, description, current=current, total=total)), error=error), view=None)


# ============================================================
# بناء برومبت الاستخراج
# ============================================================
def build_extraction_prompt(settings):
    """
    يبني برومبت الاستخراج بناءً على إعدادات المستخدم.
    صياغة إلزامية صارمة لضمان الامتثال الكامل.
    """
    # المؤثرات الصوتية — معطّلة افتراضيًا (تجاهل تام ما لم يفعّلها المستخدم)
    if settings.get("include_sfx", False):
        sfx_rule = (
            "Include all sound effects, onomatopoeia, and side text exactly as they appear."
        )
    else:
        sfx_rule = (
            "STRICT RULE: Completely ignore and omit ALL sound effects, onomatopoeia, "
            "background text, and decorative non-dialogue text "
            "(e.g. bang, crash, whoosh, thud, sfx, etc.). "
            "Do NOT write them. Do NOT reference them in any way."
        )

    # مسافات الفقاعات
    if settings.get("bubble_spacing", True):
        spacing_rule = "Leave one blank line between each speech bubble."
    else:
        spacing_rule = "No blank lines between bubbles. Output text continuously."

    return (
        "You are a precise text extraction tool for manga/manhwa images. "
        "Your sole function is to extract and output dialogue text. "
        "You do not explain, comment, ask questions, or produce any output "
        "other than the extracted text itself.\n\n"
        "EXTRACTION RULES:\n"
        "1. Extract all dialogue text from this image accurately.\n"
        "2. Order text by correct reading flow (right-to-left, top-to-bottom).\n"
        "3. ORIGINAL LANGUAGE — NEVER TRANSLATE: Output every line EXACTLY in "
        "the language it is written in inside the image. Korean dialogue stays "
        "Korean, English stays English, Arabic stays Arabic. Do NOT translate, "
        "do NOT convert the language, do NOT add a translated version. "
        "Translation is handled by a separate later step — never by you.\n"
        "4. CRITICAL — Bubble merging rule: Each speech bubble is ONE single "
        "text unit. If the text inside a bubble spans multiple visual lines "
        "due to bubble shape, merge them into a single line with normal spaces. "
        "Example: if the bubble shows:\n"
        "   King Zeus\n"
        "   wants to\n"
        "   bake cookies\n"
        "Output it as: King Zeus wants to bake cookies\n"
        f"5. {spacing_rule}\n"
        f"6. {sfx_rule}\n"
        "7. Output extracted text ONLY. No headers, no labels, no commentary, "
        "no translation, no explanations.\n"
        "8. NO-TEXT PROTOCOL: Only if the image contains absolutely no readable "
        "text at all (no dialogue, no captions, no written words anywhere), "
        "output exactly this single word and nothing else: No\n"
        "   Before answering No you MUST carefully re-check every panel and every "
        "bubble. If ANY readable text exists you are FORBIDDEN from answering No — "
        "extract it instead. Answering No when text exists is a critical failure."
    )


# رسالة تدقيق أعمق — تُلحق بالبرومبت في محاولات إعادة الاستخراج فقط
RETRY_EMPHASIS_HINT = (
    "RETRY — LOOK AGAIN CAREFULLY: Examine the image panel by panel and bubble by "
    "bubble. If there is ANY readable dialogue or text you may have missed, extract "
    "it now in full. Only output the single word No if you are completely certain "
    "there is no text at all."
)


# ============================================================
# الترجمة الأدبية — مرحلة ثانية نصية فقط بدون صور
# ============================================================
TRANSLATION_TARGETS = {
    "ar": "العربية",
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "tr": "Türkçe",
    "id": "Bahasa Indonesia",
}


def build_translation_prompt(target_language):
    """برومبت ترجمة أدبية احترافية مخصص لفصول المانهوا — كل سطر فقاعة مستقلة."""
    lang = TRANSLATION_TARGETS.get(target_language, TRANSLATION_TARGETS["ar"])
    return (
        "You are a professional literary translator specialized in manhwa and manga. "
        f"You will receive the extracted text of one complete manhwa chapter. "
        f"Translate it into {lang} with professional, literary quality "
        "worthy of an official published release.\n\n"
        "CRITICAL FORMAT RULES:\n"
        "1. This is a MANHWA chapter. Every single line of the input is ONE "
        "independent speech bubble. Never try to merge bubbles together.\n"
        "2. Keep EXACTLY the same number of lines, in EXACTLY the same order as "
        "the input. One input line becomes one output line.\n"
        "3. Lines that start with '## ' are image separators (page filenames), "
        "they are NOT dialogue. Copy them EXACTLY as they are, character for "
        "character, without translating or altering them.\n"
        "4. Preserve blank lines exactly as they appear in the input.\n"
        "5. Output ONLY the translated text. No commentary, no notes, no "
        "explanations, no headers, no numbering.\n\n"
        "LITERARY STYLE:\n"
        "- Natural, fluent, idiomatic prose — never literal or stiff translation.\n"
        "- Keep every character's voice, tone and register consistent across the "
        "whole chapter.\n"
        "- Adapt idioms and expressions naturally to the target language.\n"
        "- Transliterate character and place names consistently.\n"
        "- Sound effects may be rendered expressively in the target language.\n"
        "- Translate everything faithfully; do not censor, summarize or skip lines.\n"
        "\n"
        "REGISTER — STRICT:\n"
        f"- Write ONLY in the formal literary standard register of {lang}.\n"
        "- NEVER use colloquial dialects, street speech or slang — not even for "
        "casual dialogue, jokes or young characters. Their informal voice is "
        "conveyed through simple plain word choice inside the standard literary "
        "register, never through dialect.\n"
        "- NEVER add diacritics or vowel marks (التشكيل: الفتحة، الضمة، الكسرة، "
        "التنوين، الشدة، السكون). Output plain unvocalized text exactly like "
        "official published releases. A diacritic is allowed only in the rare "
        "case where leaving the word plain would create real ambiguity — "
        "otherwise absolutely none."
    )


def _strip_code_fences(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


# تذكير يُلحق بكل جزء في وضع السرعة (بدون تفكير) — يعوّض غياب المعالجة الأعمق
# ليضمن الفصحى الأدبية ومنع العامية والحركات الزائدة في الوضعين على حد سواء.
SPEED_MODE_REMINDER = (
    "QUALITY REMINDER (fast mode): This is a professional LITERARY translation. "
    "Formal literary standard language ONLY — strictly no colloquial dialects, "
    "no slang, no street speech. And strictly NO diacritics/vowel marks "
    "(تشكيل/حركات) except absolute necessity."
)


def request_qwen_text_completion(user_id, prompt, *, service_type="chat", max_attempts=3, thinking_enabled=False):
    """
    طلب محادثة نصي فقط (بدون أي صور) إلى المزوّد مع إعادة محاولات محدودة.
    يُستخدم لمرحلة الترجمة الأدبية — وضع الدقة الكاملة يفعّل المعالجة الأعمق.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            chat_id = create_new_chat(user_id, service_type)
            url = f"{BASE_QWEN_URL}/chat/completions?chat_id={chat_id}"
            headers = get_qwen_headers(user_id, service_type)
            headers['x-request-id'] = str(uuid.uuid4())
            headers['Accept'] = "*/*,text/event-stream"

            message_data = {
                "chat_type": "t2t",
                "content": prompt,
                "role": "user",
                "feature_config": {
                    "output_schema": "phase",
                    "thinking_enabled": bool(thinking_enabled),
                    "thinking_format": "summary",
                    "auto_thinking": bool(thinking_enabled),
                    "auto_search": False,
                },
                "timestamp": int(time.time()),
                "sub_chat_type": "t2t",
                "models": ["qwen3.8-max"],
                "user_action": "chat",
                "extra": {"meta": {"subChatType": "t2t"}},
                "files": [],
            }
            payload = {
                "stream": True,
                "incremental_output": True,
                "chat_id": chat_id,
                "chat_mode": "normal",
                "model": "qwen3.8-max",
                "messages": [message_data],
                "timestamp": int(time.time()),
                "share_id": "",
                "version": "2.1",
                "origin_branch_message_id": "",
            }

            response = requests.post(url, json=payload, headers=headers, stream=True, timeout=300)
            full_response = ""
            rate_limited = False
            for line_item in response.iter_lines():
                if not line_item:
                    continue
                line_str = line_item.decode("utf-8")
                if is_rate_limited_response(line_str):
                    rate_limited = True
                    break
                if line_str.startswith("data: "):
                    data_content = line_str[6:].strip()
                    if data_content == "[DONE]":
                        break
                    try:
                        data_json = json.loads(data_content)
                    except Exception:
                        continue
                    if is_rate_limited_response(data_json):
                        rate_limited = True
                        break
                    if "choices" in data_json and data_json["choices"]:
                        delta = data_json["choices"][0].get("delta", {})
                        if delta.get("phase") == "answer" and delta.get("content"):
                            full_response += delta["content"]

            if rate_limited:
                mark_account_rate_limited(user_id, service_type)
                last_error = Exception("ضغط مؤقت على محرك المعالجة — سيُعاد المحاولة.")
                time.sleep(2 * attempt)
                continue

            cleaned = _sanitize_translation(full_response)
            if cleaned:
                return cleaned
            last_error = Exception("تعذّر الحصول على ترجمة صالحة — حاول مرة أخرى.")
        except Exception as exc:
            last_error = exc
            time.sleep(2 * attempt)
    raise last_error or Exception("فشلت الترجمة بعد عدة محاولات.")

def _sanitize_translation(text):
    """
    تنظيف خاص بمخرجات الترجمة — بدون فحص الضجيج حتى لا تُرفض
    ترجمات مشروعة تحتوي عبارات إنجليزية شائعة (مثل Unfortunately...).
    """
    cleaned = _strip_code_fences(text)
    cleaned = re.sub(r"===\s*CHAPTER TEXT (START|END)\s*===", "", cleaned)
    return cleaned.strip()


# أنماط كشف التشكيل وحروف العربية — تخدم شبكة أمان الحركات
_TASHKEEL_RE = re.compile(r"[\u064B-\u0655\u0670]")
_ARABIC_LETTER_RE = re.compile(r"[\u0621-\u064A]")


def _strip_excessive_tashkeel(text):
    """
    شبكة أمان ضد الإفراط في الحركات (الفتحة، الضمة، الكسرة، التنوين، الشدة، السكون):
    الترجمة الأدبية المنشورة تكون نصًا ساديًا بلا تشكيل، فإن تجاوز عدد الحركات
    حدًّا صغيرًا زُلت كلها؛ أما الاستخدام الندري للضرورة (عدد قليل جدًا) فيبقى كما هو.
    """
    if not text:
        return text or ""
    marks = _TASHKEEL_RE.findall(text)
    if len(marks) <= max(8, len(_ARABIC_LETTER_RE.findall(text)) // 200):
        return text  # استخدام محدود جدًا — يُحتمل أنها ضرورية فتبقى
    return _TASHKEEL_RE.sub("", text)


def _split_translation_chunks(chapter_text, max_lines=None, max_chars=None):
    """
    يقسم نص الفصل إلى مجموعات ترجمة متوازنة:
    - حدود المجموعات عند فواصل الصفحات (## name) كلما أمكن فلا ينقطع سياق صفحة.
    - كل مجموعة تحت سقف أسطر/أحرف حتى لا ينقطع الناتج عند المزوّد.
    """
    max_lines = max_lines or TRANSLATION_CHUNK_LINES
    max_chars = max_chars or TRANSLATION_CHUNK_CHARS
    text = (chapter_text or "").strip()
    if not text:
        return []
    lines = text.split("\n")

    # تجميع السطور في كتل — كل فاصل صفحة يبدأ كتلة جديدة
    blocks = []
    current = []
    for ln in lines:
        if ln.strip().startswith("## ") and current:
            blocks.append(current)
            current = [ln]
        else:
            current.append(ln)
    if current:
        blocks.append(current)

    def weight(blk):
        return sum(1 for l in blk if l.strip()), sum(len(l) for l in blk)

    chunks = []
    cur, cur_cnt, cur_len = [], 0, 0
    for blk in blocks:
        cnt, length = weight(blk)
        if cur and (cur_cnt + cnt > max_lines or cur_len + length > max_chars):
            chunks.append("\n".join(cur).strip("\n"))
            cur, cur_cnt, cur_len = [], 0, 0
        cur.extend(blk)
        cur_cnt += cnt
        cur_len += length
    if cur:
        chunks.append("\n".join(cur).strip("\n"))

    # كتلة واحدة ضخمة بلا فواصل؟ تُشطر بالأسطر مباشرة
    final_chunks = []
    for chunk in chunks:
        chunk_lines = chunk.split("\n")
        cnt = sum(1 for l in chunk_lines if l.strip())
        if cnt <= max_lines and len(chunk) <= max_chars:
            final_chunks.append(chunk)
            continue
        cur, cur_cnt, cur_len = [], 0, 0
        for ln in chunk_lines:
            if ln.strip() and cur and (cur_cnt + 1 > max_lines or cur_len + len(ln) > max_chars):
                final_chunks.append("\n".join(cur).strip("\n"))
                cur, cur_cnt, cur_len = [], 0, 0
            cur.append(ln)
            if ln.strip():
                cur_cnt += 1
            cur_len += len(ln)
        if cur:
            final_chunks.append("\n".join(cur).strip("\n"))
    return [c for c in final_chunks if c.strip()]


def translate_chapter_text(user_id, chapter_text, settings, *, thinking_enabled=False, progress_cb=None):
    """
    يترجم نص الفصل أدبيًا في مرحلة نصية فقط بدون صور.
    الفصول الطويلة تُقسم تلقائيًا إلى مجموعات مترجمة بالتتابع
    (مع تقدم حي عبر progress_cb) حتى لا ينقطع الناتج،
    ووضع الدقة الكاملة يفعّل المعالجة الأعمق لكل مجموعة.
    الناتج النهائي يخضع لمراجعتين آليتين:
    1) مراجعة المقاطع الأجنبية المتبقية — تُترجم وتُلصق مكانها (حروف التصنيف محمية).
    2) شبكة أمان الحركات — التشكيل المفرط يُزال إلا الضرورة النادرة.
    """
    target = (settings or {}).get("translate_target", "ar")
    chunks = _split_translation_chunks(chapter_text)
    if not chunks:
        raise Exception("لا يوجد نص لترجمته.")
    total = len(chunks)
    outputs = []
    for i, chunk in enumerate(chunks, start=1):
        if progress_cb:
            try:
                progress_cb(i, total)
            except Exception:
                pass
        part_note = (
            f"\n\n(This is part {i} of {total} of the same chapter. Translate this part only, "
            "continuing naturally from the previous part, with no preamble.)"
            if total > 1 else ""
        )
        prompt = (
            build_translation_prompt(target)
            + ("\n\n" + SPEED_MODE_REMINDER if not thinking_enabled else "")
            + part_note
            + "\n\n=== CHAPTER TEXT START ===\n"
            + chunk
            + "\n=== CHAPTER TEXT END ==="
        )
        part = request_qwen_text_completion(
            user_id, prompt, service_type="chat", thinking_enabled=thinking_enabled,
        )
        cleaned = _sanitize_translation(part)
        if not cleaned:
            raise Exception(f"جاءت ترجمة الجزء {i} فارغة — حاول مرة أخرى.")
        outputs.append(cleaned)

    final_text = "\n\n".join(outputs) if total > 1 else outputs[0]
    # المراجعة الآلية على مخرجات الترجمة — أي مقطع أجنبي متبقٍ يُترجم ويُلصق مكانه
    # (حروف التصنيف A/S/SS/C محمية، وأي خلل يعيد النص كما هو)
    final_text = _review_foreign_text(user_id, final_text, target_code=target)
    if target == "ar":
        final_text = _strip_excessive_tashkeel(final_text)
    return final_text


# ============================================================
# دوال إدارة الحسابات — التخزين عبر MongoDB في utils/storage.py
# ============================================================
def get_remaining_time(limit_timestamp):
    now_ts = int(time.time())
    remaining = limit_timestamp - now_ts
    if remaining <= 0:
        return emojize("{emoji:circlecheck} **نشط**")
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    minutes = (remaining % 3600) // 60
    seconds = remaining % 60
    parts = []
    if days > 0:
        parts.append(f"{days} يوم")
    if hours > 0:
        parts.append(f"{hours} ساعة")
    if minutes > 0:
        parts.append(f"{minutes} دقيقة")
    if seconds > 0:
        parts.append(f"{seconds} ثانية")
    return emojize("{emoji:circlex} **" + " و ".join(parts) + "**")

def get_remaining_time_short(limit_timestamp):
    now_ts = int(time.time())
    remaining = limit_timestamp - now_ts
    if remaining <= 0:
        return "0s"
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    minutes = (remaining % 3600) // 60
    seconds = remaining % 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

def get_base_headers(content_type="application/json", is_app=True):
    headers = {}
    if is_app:
        headers['User-Agent'] = "Dalvik/2.1.0 (Linux; U; Android 16; CPH2631 Build/BP2A.250605.015) AliApp(QWENCHAT/2.7.2) AppType/Release AplusBridgeLite"
    else:
        headers['User-Agent'] = generate_user_agent()
    if content_type:
        headers['Content-Type'] = content_type
    return headers

def is_rate_limited_response(response_obj):
    if isinstance(response_obj, dict):
        if response_obj.get("code") == "RateLimited" or response_obj.get("data", {}).get("code") == "RateLimited":
            return True
        if "RateLimited" in json.dumps(response_obj):
            return True
    elif isinstance(response_obj, str):
        if "RateLimited" in response_obj:
            return True
    return False

def create_temp_email():
    try:
        r = requests.post(
            f"{TEMP_API_URL}/email/new",
            headers=get_base_headers(is_app=False),
            json={"min_name_length": 10, "max_name_length": 10},
            timeout=30,
        )
        if r.ok:
            return r.json().get("email")
    except Exception:
        pass
    return None

def signup_qwen(email, name, password):
    url = f"{BASE_QWEN_URL}/auths/signup"
    headers = get_base_headers()
    payload = {
        "name": name,
        "email": email,
        "password": password,
        "profile_image_url": "",
        "oauth_sub": "",
        "oauth_token": "",
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        return r.status_code in [200, 201]
    except Exception:
        return False

def get_activation_link(email, max_attempts=8, delay=3):
    ua = generate_user_agent()
    for _ in range(max_attempts):
        try:
            r = requests.get(
                f"{TEMP_API_URL}/email/{email}/messages",
                headers={"User-Agent": ua},
                timeout=30,
            )
            if r.ok:
                for m in r.json():
                    body = m.get("body_text") or m.get("body_html") or m.get("body") or ""
                    match = re.search(
                        r"https://chat\.qwen\.ai/api/v1/auths/activate\?[^\s\)\"\']+", body
                    )
                    if match:
                        return match.group(0)
        except Exception:
            pass
        time.sleep(delay)
    return None

def activate_account(activation_url):
    try:
        r = requests.get(activation_url, headers=get_base_headers(content_type=None, is_app=False), timeout=30)
        return r.status_code in [200, 201]
    except Exception:
        return False

def signin_qwen(email, password):
    url = f"{BASE_QWEN_URL}/auths/signin"
    headers = get_base_headers()
    payload = {"email": email, "password": password}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        if r.ok:
            data = r.json()
            if data.get("success") and "data" in data:
                return data["data"].get("token")
    except Exception:
        pass
    return None

def create_and_save_new_account(user_id):
    password = "899409576f885e962bb8aecc95ed24efc9b46a0872fdd8e79ed1d6fd72aeb358"
    name = "User_" + uuid.uuid4().hex[:6]
    for attempt in range(5):
        email = create_temp_email()
        if not email:
            continue
        if not signup_qwen(email, name, password):
            continue
        act_link = get_activation_link(email)
        if not act_link:
            continue
        if activate_account(act_link):
            token = signin_qwen(email, password)
            if token:
                data = load_accounts_data(user_id)
                new_acc = {
                    "email": email,
                    "password": password,
                    "token": token,
                    "image_limit_until": 0,
                    "video_limit_until": 0,
                    "image_edit_limit_until": 0,
                    "ocr_limit_until": 0,
                    "created_at": int(time.time()),
                    "image_count": 0,
                    "video_count": 0,
                    "image_edit_count": 0,
                    "ocr_count": 0,
                }
                data["accounts"].append(new_acc)
                if data["active_image_index"] == -1:
                    data["active_image_index"] = len(data["accounts"]) - 1
                if data["active_video_index"] == -1:
                    data["active_video_index"] = len(data["accounts"]) - 1
                if data["active_image_edit_index"] == -1:
                    data["active_image_edit_index"] = len(data["accounts"]) - 1
                if data["active_ocr_index"] == -1:
                    data["active_ocr_index"] = len(data["accounts"]) - 1
                save_accounts_data(user_id, data)
                return token
    raise Exception("فشل إنشاء حساب جديد بعد عدة محاولات.")

SERVICE_KEYS = {
    "image": ("image_limit_until", "active_image_index", "image_count"),
    "video": ("video_limit_until", "active_video_index", "video_count"),
    "image_edit": ("image_edit_limit_until", "active_image_edit_index", "image_edit_count"),
    "ocr": ("ocr_limit_until", "active_ocr_index", "ocr_count"),
    "chat": ("image_limit_until", "active_image_index", "image_count"),
}

def get_valid_qwen_token(user_id, service_type="chat"):
    data = load_accounts_data(user_id)
    now_ts = int(time.time())
    limit_key, active_key, _ = SERVICE_KEYS.get(service_type, SERVICE_KEYS["chat"])
    active_idx = data.get(active_key, -1)
    if 0 <= active_idx < len(data["accounts"]):
        if data["accounts"][active_idx].get(limit_key, 0) <= now_ts:
            return data["accounts"][active_idx]["token"]
    for idx, acc in enumerate(data["accounts"]):
        if acc.get(limit_key, 0) <= now_ts:
            data[active_key] = idx
            save_accounts_data(user_id, data)
            return acc["token"]
    return create_and_save_new_account(user_id)

def mark_account_rate_limited(user_id, service_type="chat"):
    data = load_accounts_data(user_id)
    unban_time = int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
    limit_key, active_key, _ = SERVICE_KEYS.get(service_type, SERVICE_KEYS["chat"])
    active_idx = data.get(active_key, -1)
    if 0 <= active_idx < len(data["accounts"]):
        data["accounts"][active_idx][limit_key] = unban_time
        data[active_key] = -1
        save_accounts_data(user_id, data)

def increment_account_usage(user_id, service_type="chat"):
    data = load_accounts_data(user_id)
    _, active_key, count_key = SERVICE_KEYS.get(service_type, SERVICE_KEYS["chat"])
    active_idx = data.get(active_key, -1)
    if 0 <= active_idx < len(data["accounts"]):
        data["accounts"][active_idx][count_key] = data["accounts"][active_idx].get(count_key, 0) + 1
        save_accounts_data(user_id, data)

def get_qwen_headers(user_id, service_type="chat"):
    token = get_valid_qwen_token(user_id, service_type)
    headers = get_base_headers(content_type="application/json; charset=UTF-8")
    headers.update({
        'Accept': "*/*,text/event-stream" if service_type in ["chat", "image", "image_edit", "ocr"] else "application/json",
        'Authorization': f"Bearer {token}",
        'x-device-id': "0",
        'source': "app",
        'Accept-Language': "en-US",
        'Cookie': f"x-ap=eu-central-1; token={token}",
    })
    return headers


# ============================================================
# دوال رفع الصور إلى Qwen OSS
# ============================================================
def generate_oss_signature(secret_key, method, content_md5, content_type, date, canonical_headers, canonical_resource):
    string_to_sign = f"{method}\n{content_md5}\n{content_type}\n{date}\n{canonical_headers}{canonical_resource}"
    h = hmac.new(secret_key.encode('utf-8'), string_to_sign.encode('utf-8'), hashlib.sha1)
    return base64.b64encode(h.digest()).decode('utf-8')

def upload_image_to_qwen_oss(user_id, photo_bytes, service_type="chat", _attempt=1):
    file_size = str(len(photo_bytes))
    filename = f"{uuid.uuid4()}_IMG.jpg"
    sts_url = "https://chat.qwen.ai/api/v2/files/getstsToken"
    payload = {"filename": filename, "filetype": "image", "filesize": file_size}
    headers = get_qwen_headers(user_id, service_type)
    headers['x-request-id'] = str(uuid.uuid4())
    session = requests.Session()
    session.headers.update(headers)
    res = session.post(sts_url, json=payload, timeout=60).json()
    if is_rate_limited_response(res):
        mark_account_rate_limited(user_id, service_type)
        # حد أقصى للمحاولات — تدوير الحسابات قد يُنشئ حسابات جديدة، فلا نسمح بحلقة مفتوحة
        if _attempt >= 3:
            raise Exception("تعذّر رفع الصورة إلى محرك المعالجة بسبب ضغط مؤقت.")
        time.sleep(2 * _attempt)
        return upload_image_to_qwen_oss(user_id, photo_bytes, service_type, _attempt + 1)
    if "data" not in res:
        raise Exception(f"فشل تصريح الرفع:\n{json.dumps(res, ensure_ascii=False)}")
    sts_res = res["data"]
    access_key_id = sts_res["access_key_id"]
    access_key_secret = sts_res["access_key_secret"]
    security_token = sts_res["security_token"]
    file_path = sts_res["file_path"]
    file_id = sts_res["file_id"]
    bucket = sts_res["bucketname"]
    host = f"{bucket}.{sts_res['endpoint']}"

    init_url = f"https://{host}/{file_path}?uploads"
    gmt_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    canon_headers = f"x-oss-security-token:{security_token}\n"
    canon_resource = f"/{bucket}/{file_path}?uploads"
    sig = generate_oss_signature(access_key_secret, "POST", "", "image/jpeg", gmt_date, canon_headers, canon_resource)
    init_headers = {
        'Authorization': f'OSS {access_key_id}:{sig}',
        'User-Agent': 'aliyun-sdk-android/2.9.21',
        'Host': host,
        'x-oss-security-token': security_token,
        'Date': gmt_date,
        'Content-Type': 'image/jpeg',
        'Content-Length': '0',
    }
    init_res = requests.post(init_url, headers=init_headers, timeout=60)
    root = ET.fromstring(init_res.text)
    upload_id = root.find('{*}UploadId').text

    part_url = f"https://{host}/{file_path}?uploadId={upload_id}&partNumber=1"
    gmt_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    content_md5 = base64.b64encode(hashlib.md5(photo_bytes).digest()).decode('utf-8')
    canon_resource = f"/{bucket}/{file_path}?partNumber=1&uploadId={upload_id}"
    sig = generate_oss_signature(access_key_secret, "PUT", content_md5, "image/jpeg", gmt_date, canon_headers, canon_resource)
    part_headers = {
        'Authorization': f'OSS {access_key_id}:{sig}',
        'User-Agent': 'aliyun-sdk-android/2.9.21',
        'Host': host,
        'x-oss-security-token': security_token,
        'Date': gmt_date,
        'Content-MD5': content_md5,
        'Content-Type': 'image/jpeg',
        'Content-Length': file_size,
    }
    part_res = requests.put(part_url, data=photo_bytes, headers=part_headers, timeout=120)
    etag = part_res.headers.get("ETag", "").replace('"', '')

    complete_url = f"https://{host}/{file_path}?uploadId={upload_id}"
    gmt_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    complete_body = f"<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>{etag}</ETag></Part></CompleteMultipartUpload>"
    canon_resource = f"/{bucket}/{file_path}?uploadId={upload_id}"
    sig = generate_oss_signature(access_key_secret, "POST", "", "image/jpeg", gmt_date, canon_headers, canon_resource)
    complete_headers = {
        'Authorization': f'OSS {access_key_id}:{sig}',
        'User-Agent': 'aliyun-sdk-android/2.9.21',
        'Host': host,
        'x-oss-security-token': security_token,
        'Date': gmt_date,
        'Content-Type': 'image/jpeg',
        'Content-Length': str(len(complete_body)),
    }
    requests.post(complete_url, data=complete_body, headers=complete_headers, timeout=60)
    signed_url = sts_res.get("file_url", f"https://{host}/{file_path}")
    return {
        "type": "image",
        "file": {"data": {}, "filename": filename, "id": file_id, "meta": {"name": filename}},
        "id": file_id,
        "filename": filename,
        "name": filename,
        "url": signed_url,
    }

def create_new_chat(user_id, service_type="chat", _attempt=1):
    url = "https://chat.qwen.ai/api/v2/chats/new"
    payload = {"chat_mode": "normal", "project_id": ""}
    headers = get_qwen_headers(user_id, service_type)
    headers['x-request-id'] = str(uuid.uuid4())
    res = requests.post(url, json=payload, headers=headers, timeout=60).json()
    if is_rate_limited_response(res):
        mark_account_rate_limited(user_id, service_type)
        if _attempt >= 3:
            raise Exception("تعذّر بدء جلسة معالجة جديدة بسبب ضغط مؤقت.")
        time.sleep(2 * _attempt)
        return create_new_chat(user_id, service_type, _attempt + 1)
    if "data" not in res:
        raise Exception(f"فشل إنشاء المحادثة:\n{json.dumps(res, ensure_ascii=False)}")
    return res["data"]["id"]

def delete_chat(user_id, chat_id, service_type="chat", timeout=10):
    if not chat_id:
        return False
    url = f"https://chat.qwen.ai/api/v2/chats/{chat_id}"
    headers = get_qwen_headers(user_id, service_type)
    headers['x-request-id'] = str(uuid.uuid4())
    headers['Content-Type'] = "application/x-www-form-urlencoded"
    try:
        r = requests.delete(url, headers=headers, timeout=timeout)
        return r.status_code in [200, 204]
    except Exception:
        return False


# ============================================================
# الكلمات الدالة على ردود غير مرغوب فيها من الموديل
# ============================================================
# إذا جاء الرد يحتوي على هذه العبارات فهو رد توضيحي وليس استخراجاً
_AI_NOISE_PATTERNS = [
    r"i('m| am) (an |a )?ai",
    r"i('m| am) (an |a )?language model",
    r"i('m| am) (an |a )?chatbot",
    r"as an ai",
    r"i cannot (see|view|access|process|analyze)",
    r"i('m| am) unable to",
    r"i don'?t (see|have access to|find) (an? )?(image|picture|photo|attachment)",
    r"no image (was |has been |is )?(?:provided|attached|found|uploaded|sent)",
    r"please (provide|send|attach|share|upload)",
    r"could you (please )?(provide|send|attach|share)",
    r"it (seems|appears|looks like) (there('s| is) )?no",
    r"i notice (that )?no image",
    r"unfortunately",
    r"i (need|require|would need)",
    r"to (assist|help) you",
    r"what (would you like|can i help)",
    r"how can i (help|assist)",
    r"is there anything (else|i can)",
]
_AI_NOISE_RE = re.compile("|".join(_AI_NOISE_PATTERNS), re.IGNORECASE)

# رد الفشل الموحد المُعاد من الدالة عند الفشل الحقيقي
_EXTRACTION_FAILED_MARKER = "[no text extracted — retry]"


def _is_ai_noise(text: str) -> bool:
    """يكشف إذا كان الرد ردًا توضيحيًا من الموديل بدلاً من نص مستخرج."""
    if not text or not text.strip():
        return False
    stripped = text.strip()
    # الردود القصيراً جداً تعتبر نص حوار حقيقي محتمل وليست ضجيجاً
    if len(stripped) < 10:
        return False
    return bool(_AI_NOISE_RE.search(stripped))


def _is_no_text_response(text: str) -> bool:
    """
    يكشف رد (لا يوجد نص) المتوقع كلمة واحدة (No) حسب البروتوكول،
    مع تغطية الصيغ القديمة والبدائل الشائعة.
    """
    if not text:
        return False
    compact = re.sub(r"\s+", "", text.strip().lower())
    if compact in {"no", "no.", "لا", "none", "notext", "لايوجد"}:
        return True
    return "notextextracted" in compact


def _sanitize_extraction(text: str) -> str:
    """
    ينظف نص الاستخراج:
    - يُعيد النص كما هو إذا كان سليماً
    - يُعيد _EXTRACTION_FAILED_MARKER إذا كان الرد (No) أو ضجيجاً توضيحياً
    """
    if not text or not text.strip():
        return ""
    if _is_no_text_response(text):
        return _EXTRACTION_FAILED_MARKER
    if _is_ai_noise(text):
        return _EXTRACTION_FAILED_MARKER
    return text.strip()


# ============================================================
# استخراج النص من صورة واحدة — مع إعادة المحاولة
# ============================================================
def get_image_dimensions(image_bytes):
    """تقرأ أبعاد الصورة الفعلية من بيانات الصورة."""
    img = Image.open(io.BytesIO(image_bytes))
    return img.width, img.height


def _do_single_extraction(user_id, image_bytes, image_name, thinking_enabled, settings, chat_id, retry_hint=False):
    """
    الدالة الأساسية لطلب واحد إلى المزوّد.
    تُعيد (النص الخام، معرف المحادثة المستخدمة) أو ترفع استثناءً عند أي فشل.
    retry_hint=True يضيف تعليمات تدقيق أعمق (تُستخدم في محاولات الإعادة فقط).
    """
    uploaded = upload_image_to_qwen_oss(user_id, image_bytes, "ocr")
    if not chat_id:
        chat_id = create_new_chat(user_id, "ocr")

    url = f"https://chat.qwen.ai/api/v2/chat/completions?chat_id={chat_id}"
    headers = get_qwen_headers(user_id, "ocr")
    headers['x-request-id'] = str(uuid.uuid4())
    headers['Accept'] = "*/*,text/event-stream"

    # قراءة الأبعاد الفعلية للصورة
    width, height = get_image_dimensions(image_bytes)

    file_payload = {
        "type": "image",
        "file": {"data": {}, "filename": uploaded["filename"], "id": uploaded["id"], "meta": {"name": uploaded["filename"]}},
        "id": uploaded["id"],
        "url": uploaded["url"],
        "name": uploaded["filename"],
        "image_width": width,
        "image_height": height,
    }

    prompt = build_extraction_prompt(settings or {})
    if retry_hint:
        prompt += "\n\n" + RETRY_EMPHASIS_HINT

    message_data = {
        "chat_type": "t2t",
        "content": prompt,
        "role": "user",
        "feature_config": {
            "output_schema": "phase",
            "thinking_enabled": thinking_enabled,
            "thinking_format": "summary",
            "auto_thinking": thinking_enabled,
            "auto_search": False,
        },
        "timestamp": int(time.time()),
        "sub_chat_type": "t2t",
        "models": ["qwen3.8-max"],
        "user_action": "chat",
        "extra": {"meta": {"subChatType": "t2t"}},
        "files": [file_payload],
    }

    payload = {
        "stream": True,
        "incremental_output": True,
        "chat_id": chat_id,
        "chat_mode": "normal",
        "model": "qwen3.8-max",
        "messages": [message_data],
        "timestamp": int(time.time()),
        "share_id": "",
        "version": "2.1",
        "origin_branch_message_id": "",
    }

    response = requests.post(url, json=payload, headers=headers, stream=True, timeout=300)
    if response.status_code != 200:
        body = ""
        try:
            body = response.text[:300]
        except Exception:
            pass
        if is_rate_limited_response(body):
            mark_account_rate_limited(user_id, "ocr")
            raise Exception("ضغط مؤقت على محرك المعالجة — سيُعاد المحاولة تلقائيًا.")
        raise Exception(f"فشل الاتصال بمحرك المعالجة (HTTP {response.status_code}).")

    full_response = ""
    rate_limited = False
    for line in response.iter_lines():
        if not line:
            continue
        line_str = line.decode('utf-8')
        if is_rate_limited_response(line_str):
            rate_limited = True
            break
        if line_str.startswith("data: "):
            data_content = line_str[6:].strip()
            if data_content == "[DONE]":
                break
            try:
                data_json = json.loads(data_content)
                if is_rate_limited_response(data_json):
                    rate_limited = True
                    break
                if "choices" in data_json and len(data_json["choices"]) > 0:
                    delta = data_json["choices"][0].get("delta", {})
                    if delta.get("phase") == "answer" and delta.get("content"):
                        full_response += delta["content"]
            except Exception:
                continue

    if rate_limited:
        mark_account_rate_limited(user_id, "ocr")
        raise Exception("ضغط مؤقت على محرك المعالجة — سيُعاد المحاولة تلقائيًا.")

    # الرد الفارغ يعني فشلًا فعليًا (انقطاع/خطأ غير معلن) وليس صورة بلا نص —
    # رفعه كاستثناء يجعل نظام المحاولات يعمل بدلاً من قبول صفحة فارغة بصمت.
    if not full_response.strip():
        raise Exception("استجابة فارغة من محرك المعالجة — سيُعاد المحاولة.")

    return full_response.strip(), chat_id


def _delete_chat_quiet(user_id, chat_id):
    """حذف المحادثة المؤقتة بعد انتهاء الصفحة — نظافة حساب بلا أي تأثير على النتيجة."""
    if not chat_id:
        return
    try:
        delete_chat(user_id, chat_id, "ocr")
    except Exception:
        pass


def choose_tile_part_count(height):
    """عدد الأجزاء المناسب لصورة طولها معيّن — لا يتجاوز TILE_MAX_PARTS أبدًا."""
    if height <= TILE_MAX_EDGE:
        return 1
    return min(TILE_MAX_PARTS, max(2, -(-height // TILE_HEIGHT)))


def split_long_image(image_bytes):
    """
    يقسم الصورة الشريطية الطويلة إلى أجزاء متساوية قابلة للقراءة
    بحد أقصى TILE_MAX_PARTS أجزاء (ثلاثة فقط — لا سبعة).
    يُستدعى فقط بعد فشل الصورة كاملة بسبب طولها — ليس خطوة افتراضية أبدًا.
    يعيد قائمة (bytes, label, height) — بعنصر واحد إذا كانت الصورة عادية لا تحتاج تقسيمًا.
    """
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    w, h = img.size
    if h <= TILE_MAX_EDGE:
        return [(image_bytes, "full", h)]
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    parts = choose_tile_part_count(h)
    step = -(-h // parts)
    tiles = []
    for i in range(parts):
        top = 0 if i == 0 else max(0, i * step - TILE_OVERLAP)
        bottom = h if i == parts - 1 else min(h, (i + 1) * step)
        if bottom - top < 40:
            continue
        crop = img.crop((0, top, w, bottom))
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=92)
        tiles.append((buf.getvalue(), f"part{i + 1}", bottom - top))
    if not tiles:
        return [(image_bytes, "full", h)]
    return tiles


def _merge_tile_texts(parts):
    """
    يدمج نصوص أجزاء الصورة الواحدة مع إزالة التكرار الناتج عن مناطق التداخل:
    السطور الأولى لكل جزء التي تكرر نهاية الجزء السابق تُتجاهل.
    """
    def norm(s):
        return re.sub(r"\s+", "", s or "").lower()

    merged = []
    for part in parts:
        lines = (part or "").splitlines()
        start = 0
        if merged:
            tail_norms = {norm(ln) for ln in merged[-14:] if ln.strip()}
            while start < len(lines):
                line_norm = norm(lines[start])
                if not line_norm or line_norm not in tail_norms:
                    break
                start += 1
        new_lines = lines[start:]
        if not new_lines:
            continue
        # فاصل بصري خفيف بين حدود الأجزاء حتى لا يلتصق حواران
        if merged and merged[-1].strip() and new_lines[0].strip():
            merged.append("")
        merged.extend(new_lines)
    return "\n".join(merged).strip()


def _sub_split_tile(user_id, tile_bytes, shown_label, thinking_enabled, settings, depth, progress_cb, display_name):
    """
    خطة الضمان للصفحات العملاقة: جزء واحد ما زال طويلًا وفشل وحده →
    يُعاد تقسيمه وحده (وليس الصورة كلها) ويُستخرج. يعيد النص أو None عند الاستحالة.
    """
    if depth >= TILE_SUB_SPLIT_DEPTH:
        return None
    try:
        sub_tiles = split_long_image(tile_bytes)
    except Exception:
        return None
    if len(sub_tiles) <= 1:
        return None
    if progress_cb and display_name:
        try:
            progress_cb("sub_split_start", display_name, 0, 0, shown_label)
        except Exception:
            pass
    return _extract_tiled(
        user_id, sub_tiles, thinking_enabled, settings,
        depth=depth + 1, progress_cb=progress_cb,
        display_name=display_name, label_prefix=f"{shown_label}/",
    )


def _extract_tiled(user_id, tiles, thinking_enabled, settings, *, depth=0, progress_cb=None, display_name="", label_prefix=""):
    """
    يستخرج نص كل جزء من أجزاء الصورة المقطوعة ثم يدمجها.
    يرفع استثناءً إذا فشل أي جزء فشلاً تقنيًا (ليُعاد للصفحة في مرحلة الإنقاذ).
    الجزء القصير الذي يجيب المزوّد فيه (No) يُقبل كمنطقة فارغة مشروعة بعد تدقيق الإعادة —
    أما الجزء الذي ما زال طويلًا فلا يُوثق ردّه (No) لأن الصورة مضغوطة: يُعاد تقسيمه وحده.
    """
    texts = []
    last_error = None
    total = len(tiles)
    for t_idx, (tile_bytes, tile_label, tile_height) in enumerate(tiles, start=1):
        shown_label = f"{label_prefix}{tile_label}"
        if progress_cb and display_name:
            try:
                progress_cb(
                    "split_sub" if depth > 0 else "split_tile",
                    display_name, t_idx, total, shown_label,
                )
            except Exception:
                pass
        accepted = False
        for attempt in range(1, TILE_MAX_RETRIES + 1):
            chat_id = None
            try:
                raw, chat_id = _do_single_extraction(
                    user_id, tile_bytes, shown_label, thinking_enabled, settings, None,
                    retry_hint=attempt >= 2,
                )
                cleaned = _sanitize_extraction(raw)
                if cleaned == _EXTRACTION_FAILED_MARKER:
                    # (No) على جزء ما زال طويلًا لا يُصدَّق — تقسيم فرعي للجزء وحده
                    if tile_height > TILE_MAX_EDGE:
                        sub_text = _sub_split_tile(
                            user_id, tile_bytes, shown_label, thinking_enabled,
                            settings, depth, progress_cb, display_name,
                        )
                        if sub_text is not None:
                            texts.append(sub_text)
                            accepted = True
                            break
                    # جزء قصير أعلن (No)؟ لا يُقبل من المحاولة الأولى — تدقيق إعادة
                    # بتلميح أعمق أولًا حمايةً من إعلان كاذب، ثم يُقبل كمنطقة فارغة مشروعة
                    if attempt < TILE_MAX_RETRIES:
                        continue
                    texts.append("")
                    accepted = True
                    break
                texts.append(cleaned)
                accepted = True
                break
            except Exception as exc:
                last_error = exc
                time.sleep(2 * attempt)
            finally:
                _delete_chat_quiet(user_id, chat_id)
        if not accepted:
            # فشل تقني متكرر في جزء ما زال طويلًا؟ تقسيم فرعي أخير قبل الاستسلام
            if tile_height > TILE_MAX_EDGE:
                sub_text = _sub_split_tile(
                    user_id, tile_bytes, shown_label, thinking_enabled,
                    settings, depth, progress_cb, display_name,
                )
                if sub_text is not None and sub_text.strip():
                    texts.append(sub_text)
                    continue
            raise last_error or Exception(f"تعذّرت قراءة جزء من الصورة ({shown_label}).")
    return _merge_tile_texts(texts)


def extract_text_from_single_image(user_id, image_bytes, image_name, thinking_enabled, settings=None, chat_id=None, progress_cb=None):
    """
    استخراج كامل الدورة لصورة واحدة:
    1) الصورة تُجرّب كاملة أولًا دائمًا في الوضعين — لا تقسيم استباقي أبدًا.
    2) فقط إذا فشلت الصورة الطويلة بسبب طولها (خطأ تقني أو إعلان No غير قابل للتصديق)
       تُقسّم إلى حد أقصى TILE_MAX_PARTS أجزاء ويُدمج نصها.
    3) الفشل التقني الحقيقي يرفع استثناءً ليستلمه خط الأنابيب في مرحلة الإنقاذ.
    4) الناتج يبقى دائمًا باللغة الأصلية كما في الصورة — لا ترجمة هنا إطلاقًا
       (الترجمة والمراجعة الآلية تحدث لاحقًا على مخرجات الترجمة فقط).
    progress_cb: دالة اختيارية (stage, name, part, total, label) لتحديث مؤشر الانتظار
    بأمان من داخل خيوط المعالجة.
    """
    last_error = None

    # هل الصورة شريطًا طويلًا؟ (فقط الطويلة هي المرشحة للتقسيم عند الفشل)
    long_image = False
    try:
        _, img_height = get_image_dimensions(image_bytes)
        long_image = img_height > TILE_MAX_EDGE
    except Exception:
        long_image = False

    for attempt in range(1, MAX_RETRY_PER_IMAGE + 1):
        chat_id_used = None
        try:
            raw, chat_id_used = _do_single_extraction(
                user_id, image_bytes, image_name, thinking_enabled, settings, chat_id,
                retry_hint=attempt >= 2,
            )
            cleaned = _sanitize_extraction(raw)
            if cleaned != _EXTRACTION_FAILED_MARKER:
                return cleaned  # نص أصلي خام — لا مراجعة ولا ترجمة هنا
            last_error = None  # إعلان (No) موثّق — ليس خطأ تقنيًا
            if long_image:
                # صورة طويلة أُعلن (No) عليها؟ الرد غير قابل للوثيق لأن الصورة
                # تُضغط بعنف — انتقال مباشر للتقسيم بدل حرق المحاولات عبثًا.
                break
        except Exception as exc:
            last_error = exc
        finally:
            _delete_chat_quiet(user_id, chat_id_used)
        if attempt < MAX_RETRY_PER_IMAGE:
            time.sleep(2 * attempt)
            chat_id = None

    # خطة التقسيم الذكي — فقط للصور الطويلة التي فشلت فعلاً (في الوضعين)
    if long_image:
        if progress_cb:
            try:
                progress_cb("split_start", image_name, 0, 0, "")
            except Exception:
                pass
        try:
            tiles = split_long_image(image_bytes)
        except Exception:
            tiles = []
        if len(tiles) > 1:
            merged = _extract_tiled(
                user_id, tiles, True, settings,
                progress_cb=progress_cb, display_name=image_name,
            )
            if merged.strip():
                return merged  # نص أصلي خام — لا مراجعة ولا ترجمة هنا
            # حتى مع التقسيم: لا نص إطلاقًا — صورة فارغة مؤكدة
            return ""

    if last_error is not None:
        raise last_error
    return ""


# ============================================================
# المراجعة الآلية — ترجمة المقاطع الأجنبية المتبقية في النص المُترجم
# أحيانًا تُهلوس الترجمة فتبقى فيها كلمات أو حروف أجنبية —
# تُكتشف آليًا، تُترجم، ثم يُلصق الناتج مكانها بدقة دون المساس بأي شيء آخر.
# حروف التصنيف مثل A, S, SS, C تُستثنى — فهي صحيحة كما هي.
# تُطبّق على مخرجات الترجمة فقط — نص الاستخراج الخام لا يُمس إطلاقًا.
# ============================================================
_FOREIGN_RANK_TOKEN_RE = re.compile(r"^[A-FS]{1,3}$")
# عبارة لاتينية متصلة (كلمات متتابعة بمسافات تُترجم معًا كجملة واحدة) مع علامات الترقيم الختامية
# حتى لا تبقى علامة استفهام/تعجب يتيمة بعد الترجمة (مثل: ؟?)
_FOREIGN_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*(?:[ \t]+[A-Za-z][A-Za-z'’\-]*)*[?!,.;:؟]*")
# مسار متصل من الحروف الآسيوية (كورية/صينية/يابانية) مع مسافاتها الداخلية
_FOREIGN_ASIAN_RE = re.compile(
    "[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\uAC00-\uD7A3\u3130-\u318F\u1100-\u11FF]"
    "(?:[ \t]*[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\uAC00-\uD7A3\u3130-\u318F\u1100-\u11FF])*"
)
_FOREIGN_REVIEW_MAX_SNIPPETS = 80
# مسار متصل من الحروف العربية — يُستخدم عندما تكون لغة الهدف لاتينية (الترجمة إلى إنجليزي مثلًا)
_FOREIGN_ARABIC_RE = re.compile(
    "[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
    "(?:[ \t]*[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF])*"
)


_FOREIGN_RANK_STRIP = "?!,.;:؟*"


def _is_rank_letters(phrase):
    """هل العبارة اللاتينية كلها حروف تصنيف (A, S, SS, C...)؟ هذه تبقى كما هي."""
    tokens = [t.strip(_FOREIGN_RANK_STRIP) for t in phrase.split()]
    return bool(tokens) and all(t and _FOREIGN_RANK_TOKEN_RE.match(t) for t in tokens)


def _collect_foreign_segments(text, target_code="ar"):
    """
    يجمع المقاطع الأجنبية الفريدة (إنجليزية/كورية/صينية/يابانية) من نص مستخرج.
    - حروف التصنيف المفردة/المزدوجة بأحرف كبيرة (A, S, SS, C...) تبقى كما هي.
    - فاصل الصفحات (## name) لا يُمس أبدًا.
    يعيد قائمة {"text", "line"} بلا تكرار.
    """
    segments = []
    seen = set()
    for line_txt in (text or "").splitlines():
        if not line_txt.strip() or line_txt.strip().startswith("## "):
            continue
        found = []
        if target_code == "ar":
            for m in _FOREIGN_LATIN_RE.finditer(line_txt):
                if _is_rank_letters(m.group()):
                    continue  # حروف تصنيف — تبقى كما هي
                found.append(m.group())
            for m in _FOREIGN_ASIAN_RE.finditer(line_txt):
                found.append(m.group())
        else:
            # لغة هدف لاتينية (إنجليزي/فرنسي...) — المتبقي هو العربي والآسيوي
            for m in _FOREIGN_ARABIC_RE.finditer(line_txt):
                found.append(m.group())
            for m in _FOREIGN_ASIAN_RE.finditer(line_txt):
                found.append(m.group())
        for seg in found:
            seg = seg.strip()
            if not seg:
                continue
            key = seg.lower()
            if key in seen:
                continue
            seen.add(key)
            segments.append({"text": seg, "line": line_txt.strip()})
    return segments


def _review_foreign_text(user_id, text, *, target_code="ar"):
    """
    المراجعة الآلية لنتيجة الترجمة:
    إذا بقي في النص المترجم أي مقطع أجنبي (لا يطابق لغة الهدف) يُرسل مع سياقه
    للترجمة، ثم يُلصق الناتج مكانه. حروف التصنيف A/S/SS/C محمية.
    أي خلل في المراجعة يعيد النص الأصلي كما هو — لا يفسد النص أبدًا.
    """
    if not FOREIGN_REVIEW_ENABLED or not (text or "").strip():
        return text or ""
    try:
        segments = _collect_foreign_segments(text, target_code)
        if not segments:
            return text
        if len(segments) > _FOREIGN_REVIEW_MAX_SNIPPETS:
            segments = segments[:_FOREIGN_REVIEW_MAX_SNIPPETS]
        payload = [
            {"id": str(i + 1), "text": seg["text"], "context": seg["line"][:200]}
            for i, seg in enumerate(segments)
        ]
        lang = TRANSLATION_TARGETS.get(target_code, TRANSLATION_TARGETS["ar"])
        if target_code == "ar":
            role_line = "You are a precise post-editing assistant for Arabic manhwa dialogue."
            leftover_line = (
                "The translated text was supposed to be fully Arabic, but a few foreign "
                "fragments (English, Korean, Chinese or Japanese) were left inside it."
            )
            style_line = (
                "Translate every fragment into natural, fluent literary Modern Standard Arabic "
                "(fusha — never slang, never dialects, never diacritics/tashkeel) that fits its "
                "context — concise manhwa dialogue style, exactly as it should read in place."
            )
        else:
            role_line = f"You are a precise post-editing assistant for {lang} manhwa dialogue."
            leftover_line = (
                f"The translated text was supposed to be fully {lang}, but a few foreign "
                "fragments (Arabic, Korean, Chinese or Japanese) were left inside it."
            )
            style_line = (
                f"Translate every fragment into natural, fluent {lang} that fits its "
                "context — concise manhwa dialogue style, exactly as it should read in place."
            )
        prompt = (
            role_line + "\n"
            + leftover_line + "\n"
            "You will receive a JSON array of these fragments; each fragment has the "
            "line it appeared in as context.\n"
            + style_line + "\n"
            "Return ONLY a strict JSON object mapping each id to its translation, "
            'like {"1": "...", "2": "..."} — no commentary, no code fences, no extra keys.\n\n'
            "FRAGMENTS:\n" + json.dumps(payload, ensure_ascii=False)
        )
        raw = request_qwen_text_completion(
            user_id, prompt, service_type="chat", max_attempts=2, thinking_enabled=False,
        )
        data = json.loads(_strip_code_fences(raw))
        translations = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and value.strip():
                    translations[str(key).strip()] = value.strip()
        elif isinstance(data, list):
            for item in data:
                if (
                    isinstance(item, dict)
                    and item.get("id") is not None
                    and isinstance(item.get("ar"), str)
                    and item["ar"].strip()
                ):
                    translations[str(item["id"]).strip()] = item["ar"].strip()
        if not translations:
            return text
        result = text
        # الأطول أولًا حتى لا يفسد مقطع قصير جزءًا من مقطع أطول
        indexed = sorted(enumerate(segments), key=lambda kv: len(kv[1]["text"]), reverse=True)
        for i, seg in indexed:
            translated = translations.get(str(i + 1), "")
            original = seg["text"]
            if not translated or translated.lower() == original.lower():
                continue
            if len(translated) > max(40, len(original) * 4 + 20):
                continue  # حماية من هلوسة تضخّم النص
            result = result.replace(original, translated)
        return result
    except Exception:
        return text  # أي خلل لا يفسد النص الأصلي أبدًا


# ============================================================
# دوال معالجة الصور من مصادر مختلفة
# ============================================================
def natural_sort_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]

def download_image_from_url(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    r = requests.get(url, headers=headers, stream=True, timeout=60)
    r.raise_for_status()
    return r.content

def sanitize_output_stem(value, default="zeus"):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", value or "").strip()
    value = re.sub(r"\s+", "_", value).strip("._")
    return (value or default)[:80]

def unique_output_path(directory, stem, suffix, ascii_safe=False):
    stem = sanitize_output_stem(stem)
    if ascii_safe:
        # أسماء مرفقات Components V2 يجب أن تكون ASCII لتعمل إشارة attachment://
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "zeus"
    candidate = directory / f"{stem}.{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}.{suffix}"
        counter += 1
    return candidate

def make_docx_bytes(text):
    def esc(v):
        return (v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    paragraphs = "".join(f"<w:p><w:r><w:t xml:space='preserve'>{esc(part)}</w:t></w:r></w:p>" for part in text.split("\n"))
    document = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body>{paragraphs}<w:sectPr/></w:body></w:document>"""
    content_types = """<?xml version='1.0' encoding='UTF-8'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/></Types>"""
    rels = """<?xml version='1.0' encoding='UTF-8'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/></Relationships>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    buffer.seek(0)
    return buffer


def read_docx_text(data: bytes) -> str:
    """يقرأ نص ملف DOCX (word/document.xml) ويعيد الفقرات كأسطر."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_bytes = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise Exception("ملف DOCX غير صالح أو تالف.")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise Exception("تعذّرت قراءة محتوى ملف DOCX.")
    paragraphs = []
    for p in root.iter():
        if not str(p.tag).endswith("}p"):
            continue
        chunks = [t.text or "" for t in p.iter() if str(t.tag).endswith("}t")]
        paragraphs.append("".join(chunks))
    return "\n".join(paragraphs).strip()


def read_upload_chapter_text(filename: str, data: bytes) -> str:
    """يستخرج نص الفصل من ملف TXT أو DOCX مرفوع."""
    name = (filename or "").lower()
    if name.endswith(".txt"):
        text = data.decode("utf-8", errors="replace").strip()
    elif name.endswith(".docx"):
        text = read_docx_text(data)
    else:
        raise Exception("صيغة غير مدعومة — أرفق ملف `TXT` أو `DOCX` فقط.")
    if not text.strip():
        raise Exception("الملف فارغ — لا يوجد نص لترجمته.")
    return text


def ensure_valid_image(data: bytes, source_label: str = "") -> bytes:
    """
    يتأكد أن البيانات صورة حقيقية قابلة للفك — يرفض صفحات الخطأ HTML
    أو الملفات التالفة مبكرًا برسالة واضحة بدل فشل غامض في مرحلة الاستخراج.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return data
    except Exception as exc:
        label = f" (`{source_label}`)" if source_label else ""
        raise Exception(f"المصدر{label} ليس صورة صالحة — قد يكون التحميل أعاد صفحة خطأ بدلاً منه، أعد المحاولة.") from exc


def extract_gdrive_folder_id(url):
    patterns = [
        r'/folders/([a-zA-Z0-9_-]+)',
        r'[?&]id=([a-zA-Z0-9_-]+)',
        r'/drive/([a-zA-Z0-9_-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def extract_gdrive_file_id(url):
    patterns = [
        r'/file/d/([a-zA-Z0-9_-]+)',
        r'id=([a-zA-Z0-9_-]+)',
        r'/open\?id=([a-zA-Z0-9_-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def get_drive_item_name(item_id):
    if not GDRIVE_API_KEY:
        return "zeus"
    url = f"https://www.googleapis.com/drive/v3/files/{item_id}"
    r = requests.get(url, params={"key": GDRIVE_API_KEY, "fields": "name"}, timeout=30)
    if r.status_code == 200:
        return r.json().get("name") or "zeus"
    return "zeus"

def list_drive_images(folder_id):
    """
    يجلب صور المجلد مع شبكة أمان إضافية:
    - mimeType يحتوي image/ أو الاسم بامتداد صورة (بعض الملفات تُخزن بميم خاطئ فتُحذف من القائمة)
    - استبعاد عناصر Google Docs/Sheets والمحذوفة
    """
    url = "https://www.googleapis.com/drive/v3/files"
    name_match = " or ".join(
        [f"name contains '{ext}'" for ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp")]
    )
    query = (
        f"'{folder_id}' in parents and trashed = false "
        f"and (mimeType contains 'image/' or {name_match})"
    )
    params = {
        "q": query,
        "key": GDRIVE_API_KEY,
        "fields": "files(id,name,mimeType)",
        "orderBy": "name",
        "pageSize": 1000,
    }
    r = requests.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise Exception(f"فشل جلب قائمة الصور: {r.status_code} - {r.text[:200]}")
    files = [
        f for f in r.json().get("files", [])
        if not str(f.get("mimeType", "")).startswith("application/vnd.google-apps")
    ]
    files.sort(key=lambda x: natural_sort_key(x.get("name", "")))
    return files

def download_drive_image(file_id):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = requests.get(url, headers=headers, allow_redirects=True, stream=True, timeout=120)
    if response.status_code != 200 or "text/html" in response.headers.get("Content-Type", ""):
        url = f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"
        response = requests.get(url, headers=headers, stream=True, timeout=120)
        if response.status_code != 200:
            raise Exception(f"فشل تحميل الصورة من Drive: {file_id}")
    return response.content

def process_drive_link(link, *, include_name=False):
    folder_id = extract_gdrive_folder_id(link)
    file_id = extract_gdrive_file_id(link)
    if folder_id:
        files = list_drive_images(folder_id)
        images = []
        for f in files:
            name = f.get("name", "")
            mime = str(f.get("mimeType", ""))
            if not (
                mime.startswith("image/")
                or name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp"))
            ):
                continue
            data = ensure_valid_image(download_drive_image(f["id"]), name)
            images.append((data, name))
        if not images:
            raise Exception("المجلد لا يحتوي صورًا صالحة.")
        source_name = get_drive_item_name(folder_id)
        return (images, source_name) if include_name else images
    elif file_id:
        data = download_drive_image(file_id)
        if zipfile.is_zipfile(io.BytesIO(data)):
            images = extract_images_from_zip_bytes(data)
        else:
            images = [(data, f"image_{file_id}.jpg")]
        source_name = get_drive_item_name(file_id)
        return (images, Path(source_name).stem) if include_name else images
    else:
        raise Exception("رابط Drive غير صالح.")

def extract_images_from_zip_bytes(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        image_names = [
            name for name in zf.namelist()
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
            and not name.startswith("__MACOSX")
        ]
        image_names.sort(key=lambda x: natural_sort_key(os.path.basename(x)))
        images = []
        for name in image_names:
            data = zf.read(name)
            try:
                data = ensure_valid_image(data, os.path.basename(name))
            except Exception:
                continue  # ملف تالف داخل الأرشيف — تجاوزه ولا تُفشّل الفصل كله
            images.append((data, os.path.basename(name)))
        return images


# ============================================================
# منطق نقاط الاستهلاك الموحد — مستخدم أو سيرفر
# ============================================================
async def resolve_and_consume_point(user_id: int, guild_id) -> tuple:
    """
    يحدد مصدر النقاط تلقائياً:
    - إذا كان السيرفر لديه نقاط → يخصم منه دائماً (الأولوية القصوى لرصيد السيرفر)
    - وإلا → يخصم من المستخدم
    يعيد: (ok, profile_after, source)  حيث source = "server" أو "user"
    """
    if guild_id and server_has_points(guild_id):
        ok, profile_after = consume_server_point(guild_id)
        if ok:
            return ok, profile_after, "server"
    ok, profile_after = consume_point(user_id)
    return ok, profile_after, "user"


async def _push_split_status(status_msg, stage, name, part, total_parts, label, current, total):
    """رسائل مرحلة التقسيم الذكي في مؤشر الانتظار (تُستدعى بأمان من خيوط المعالجة)."""
    try:
        if stage == "split_start":
            await edit_status(
                status_msg,
                "{emoji:alerttriangle} فشل استخراج صورة لكبر حجمها",
                f"الصفحة `{name}` أطول من أن تُقرأ كاملة — جاري تقسيمها إلى أجزاء قابلة للقراءة.",
                current=current, total=total,
            )
        elif stage == "split_tile":
            await edit_status(
                status_msg,
                "{emoji:photo} استخراج صفحة مقسّمة",
                f"الصفحة `{name}` — جاري استخراج الجزء `{part}` من `{total_parts}`.",
                current=current, total=total,
            )
        elif stage == "sub_split_start":
            await edit_status(
                status_msg,
                "{emoji:alerttriangle} جزء عملاق يحتاج تقسيمًا إضافيًا",
                f"الصفحة `{name}` — الجزء `{label}` ما زال طويلًا، جاري تقسيمه وحده.",
                current=current, total=total,
            )
        elif stage == "split_sub":
            await edit_status(
                status_msg,
                "{emoji:photo} استخراج صفحة مقسّمة",
                f"الصفحة `{name}` — جاري استخراج الجزء الفرعي `{label}` (`{part}` من `{total_parts}`).",
                current=current, total=total,
            )
    except Exception:
        pass


# ============================================================
# خط أنابيب استخراج الصفحات — نظام محاولات مزدوج للصفحات المتعثرة
# ============================================================
async def run_extraction_pipeline(status_msg, user_id, images, thinking_enabled, settings):
    """
    يستخرج نص كل صفحة مع نظام محاولات قوي ومعالجة متوازية:
    - كل صفحة تحصل على محاولات داخلية كاملة + تقسيم ذكي للشرائط الطويلة.
    - عدة صفحات تُعالج في نفس اللحظة (EXTRACTION_CONCURRENCY) لتقصير المدة الكلية.
    - الصفحات التي تفشل فعلياً تُعاد مرة أخيرة بوضع الدقة الكاملة قبل الاستسلام.
    - لا يتوقف الفصل كله بسبب صفحة واحدة فاشلة.
    يعيد: (combined_text, empty_images, failed_page_names)
    """
    total_images = len(images)
    page_texts = {}
    failed_notices = {}
    completed = 0
    progress_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max(1, EXTRACTION_CONCURRENCY))
    loop = asyncio.get_running_loop()

    def thread_progress(stage, name, part=0, total_parts=0, label=""):
        """يحوّل تحديثات مراحل التقسيم من خيوط المعالجة إلى مؤشر الانتظار بأمان."""
        asyncio.run_coroutine_threadsafe(
            _push_split_status(status_msg, stage, name, part, total_parts, label, completed, total_images),
            loop,
        )

    async def worker(idx: int, img_bytes: bytes, img_name: str):
        nonlocal completed
        async with semaphore:
            try:
                page_texts[idx] = await asyncio.to_thread(
                    extract_text_from_single_image,
                    user_id,
                    img_bytes,
                    img_name,
                    thinking_enabled,
                    settings,
                    progress_cb=thread_progress,
                )
            except Exception as e:
                page_texts[idx] = None
                failed_notices[idx] = str(e)
            completed += 1
            detail = f"اكتملت معالجة `{completed}` من `{total_images}` صفحة."
            if failed_notices:
                names = "، ".join(f"`{name}`" for name in list(failed_notices.keys())[:4])
                detail += f"\nصفحات ستحصل على محاولة إنقاذ بعد اكتمال البقية: {names}"
            async with progress_lock:
                await edit_status(
                    status_msg,
                    "المعالجة مستمرة",
                    detail,
                    current=completed,
                    total=total_images,
                )

    tasks = [
        asyncio.create_task(worker(idx, img_bytes, img_name))
        for idx, (img_bytes, img_name) in enumerate(images, start=1)
    ]
    await asyncio.gather(*tasks)

    retry_pages = [idx for idx, text in page_texts.items() if text is None]
    if retry_pages:
        await edit_status(
            status_msg,
            "محاولة إنقاذ أخيرة للصفحات المتعثرة",
            f"عدد الصفحات: `{len(retry_pages)}` — جاري إعادة المحاولة بوضع دقة كاملة وتقسيم ذكي.",
        )
        for idx in retry_pages:
            img_bytes, img_name = images[idx - 1]
            try:
                page_texts[idx] = await asyncio.to_thread(
                    extract_text_from_single_image,
                    user_id,
                    img_bytes,
                    img_name,
                    True,  # وضع الدقة الكاملة في المحاولة الأخيرة
                    settings,
                    progress_cb=thread_progress,
                )
            except Exception:
                page_texts[idx] = None

    combined_text = ""
    empty_images = 0
    failed_pages = []
    for idx, (img_bytes, img_name) in enumerate(images, start=1):
        text = page_texts.get(idx)
        if text is None:
            failed_pages.append(img_name)
            text = ""
        if not (text or "").strip():
            empty_images += 1
        combined_text += output_separator(settings, idx, img_name) + (text or "")
    return combined_text, empty_images, failed_pages


# ============================================================
# واجهة اختيار الوضع
# ============================================================
class ModeSelectView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.thinking_enabled = None
        self.confirmed = False
        if len(self.children) >= 2:
            self.children[0].emoji = emoji_manager.partial("bolt")
            self.children[1].emoji = emoji_manager.partial("circlecheck")

    @discord.ui.button(label="سرعة عالية", emoji=emoji_manager.partial("bolt"), style=discord.ButtonStyle.secondary)
    async def high_speed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذا الزر ليس لك.", ephemeral=True)
            return
        self.thinking_enabled = False
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="دقة كاملة", emoji=emoji_manager.partial("circlecheck"), style=discord.ButtonStyle.secondary)
    async def high_accuracy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذا الزر ليس لك.", ephemeral=True)
            return
        self.thinking_enabled = True
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

def mode_selection_sections():
    return [
        f"## {emoji_manager.placeholder('settings')} اختر وضع المعالجة",
        (
            f"### {emoji_manager.placeholder('bolt')} سرعة عالية — موصى به\n"
            "يحافظ على الدقة بشكل ممتاز ويعطي نتيجة أسرع لمعظم الفصول.\n"
            "**استخدمه افتراضيًا** إلا إذا كان الفصل صعبًا جدًا أو النص غير واضح."
        ),
        (
            f"### {emoji_manager.placeholder('circlecheck')} دقة كاملة\n"
            "يفعّل معالجة أعمق للحالات الصعبة جدًا.\n"
            f"{emoji_manager.placeholder('alerttriangle')} **تنبيه:** قد يطول كثيرًا مع الفصول الطويلة أو الشريطية (معالجة متوازية دقيقة الجزء بالجزء)."
        ),
    ]

async def send_mode_selection(destination, view):
    if V2_SEPARATOR_SUPPORTED:
        await send_panel(destination, sections=mode_selection_sections())
        return await destination.send(content=emojize("{emoji:settings} **اختر الوضع من الأزرار بالأسفل:**"), view=view)
    return await destination.send(embed=themed_embed(description="\n\n".join(mode_selection_sections())), view=view)

def upload_prompt_sections():
    return [
        f"## {emoji_manager.placeholder('folderopen')} خطوة إرفاق صور الفصل.",
        "## توجد ثلاث طرق لكي ترسل صور فصلك.",
        f"{emoji_manager.placeholder('photo')} **الطريقة الأولى:** إرفاق ما يصل إلى `{MAX_IMAGES_PER_REQUEST}` صور مباشرة في نفس الرسالة، مع ترتيب الصور بالأرقام قبل الإرسال.",
        f"{emoji_manager.placeholder('folder')} **الطريقة الثانية:** إرسال ملف ZIP بداخله صور الفصل مرتبة بأسمائها.",
        f"{emoji_manager.placeholder('folderopen')} **الطريقة الثالثة:** إرسال رابط Google Drive يحتوي على صور الفصل مرتبة بأسمائها.",
    ]

async def send_upload_prompt(destination):
    return await send_panel(destination, sections=upload_prompt_sections())


# ============================================================
# الواجهة التفاعلية لإدارة الحسابات
# ============================================================
class AccountsView(discord.ui.View):
    def __init__(self, user_id, page=0):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.page = page
        self.update_buttons()

    def get_data(self):
        return load_accounts_data(self.user_id)

    def update_buttons(self):
        data = self.get_data()
        accounts = data["accounts"]
        per_page = 5
        start = self.page * per_page
        end = start + per_page
        self.page_accounts = accounts[start:end]
        self.clear_items()

        for idx, acc in enumerate(self.page_accounts):
            real_idx = start + idx
            status = ""
            if data["active_ocr_index"] == real_idx:
                status += "نشط"
            btn = discord.ui.Button(
                label=f"حساب {real_idx + 1} {status}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"acc_{real_idx}",
                row=0,
            )
            btn.callback = self.make_callback(real_idx)
            self.add_item(btn)

        if self.page > 0:
            prev_btn = discord.ui.Button(label="السابق", emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id="prev", row=1)
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)
        if end < len(accounts):
            next_btn = discord.ui.Button(label="التالي", emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="next", row=1)
            next_btn.callback = self.next_page
            self.add_item(next_btn)

        create_btn = discord.ui.Button(label="إنشاء حساب", emoji=emoji_manager.partial("circlecheck"), style=discord.ButtonStyle.secondary, custom_id="create", row=1)
        create_btn.callback = self.create_account
        self.add_item(create_btn)

        status_btn = discord.ui.Button(label="الحالة", emoji=emoji_manager.partial("chartpie"), style=discord.ButtonStyle.secondary, custom_id="status", row=1)
        status_btn.callback = self.show_status
        self.add_item(status_btn)

    def make_callback(self, idx):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
                return
            await self.show_account_detail(interaction, idx)
        return callback

    async def prev_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    async def create_account(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await asyncio.to_thread(create_and_save_new_account, self.user_id)
            self.update_buttons()
            await interaction.followup.send("{emoji:circlecheck} تم إنشاء حساب جديد بنجاح.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"{{emoji:circlex}} فشل: {str(e)}", ephemeral=True)

    async def show_status(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = self.get_data()
        total = len(data["accounts"])
        now_ts = int(time.time())
        active = sum(1 for a in data["accounts"] if a.get("ocr_limit_until", 0) <= now_ts)
        embed = themed_embed("{emoji:chartpie} حالة الحسابات", color_name="blue")
        embed.add_field(name="إجمالي الحسابات", value=str(total))
        embed.add_field(name="نشطة للمعالجة", value=str(active))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def show_account_detail(self, interaction: discord.Interaction, idx):
        data = self.get_data()
        if idx >= len(data["accounts"]):
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)
            return
        acc = data["accounts"][idx]
        embed = themed_embed(f"{emoji_manager.placeholder('user')} تفاصيل الحساب {idx + 1}", color_name="green")
        embed.add_field(name=emojize("{emoji:mail} البريد"), value=acc["email"][:40], inline=False)
        embed.add_field(name=emojize("{emoji:lock} التوكن"), value=acc["token"][:30] + "...", inline=False)
        ocr_limit = acc.get("ocr_limit_until", 0)
        embed.add_field(name=emojize("{emoji:bookmark} حالة الخدمة"), value=get_remaining_time(ocr_limit), inline=True)
        embed.add_field(name=emojize("{emoji:chartpie} عدد الاستخدامات"), value=str(acc.get("ocr_count", 0)), inline=True)
        view = AccountDetailView(self.user_id, idx)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class AccountDetailView(discord.ui.View):
    _BUTTON_EMOJIS = {
        "تعيين كخدمة نشطة": "bookmark",
        "تجديد التوكن": "clock",
        "فك الحظر": "tv_unlock",
        "حذف الحساب": "trash",
    }

    def __init__(self, user_id, acc_idx):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.acc_idx = acc_idx
        # إعادة تطبيق الإيموجي المخصص وقت التشغيل (المزخرفات تُقيّم عند الاستيراد قبل تحميل الإيموجيات)
        for child in self.children:
            emoji_key = self._BUTTON_EMOJIS.get(getattr(child, "label", ""))
            if emoji_key:
                child.emoji = emoji_manager.partial(emoji_key)

    @discord.ui.button(label="تعيين كخدمة نشطة", emoji=emoji_manager.partial("bookmark"), style=discord.ButtonStyle.secondary)
    async def set_active(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            data["active_ocr_index"] = self.acc_idx
            data["accounts"][self.acc_idx]["ocr_limit_until"] = 0
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message("{emoji:circlecheck} تم تعيين الحساب كخدمة نشطة.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="تجديد التوكن", emoji=emoji_manager.partial("clock"), style=discord.ButtonStyle.secondary)
    async def refresh_token(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            acc = data["accounts"][self.acc_idx]
            new_token = await asyncio.to_thread(signin_qwen, acc["email"], acc["password"])
            if new_token:
                data["accounts"][self.acc_idx]["token"] = new_token
                save_accounts_data(self.user_id, data)
                await interaction.response.send_message("{emoji:circlecheck} تم تجديد التوكن.", ephemeral=True)
            else:
                await interaction.response.send_message("{emoji:circlex} فشل تجديد التوكن.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="فك الحظر", emoji=emoji_manager.partial("tv_unlock"), style=discord.ButtonStyle.secondary)
    async def unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            data["accounts"][self.acc_idx]["ocr_limit_until"] = 0
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message("{emoji:circlecheck} تم فك الحظر.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="حذف الحساب", emoji=emoji_manager.partial("trash"), style=discord.ButtonStyle.secondary)
    async def delete_account(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            del data["accounts"][self.acc_idx]
            if data["active_ocr_index"] >= len(data["accounts"]):
                data["active_ocr_index"] = -1
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message("{emoji:trash} تم حذف الحساب.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)


# ============================================================
# واجهات المستخدم: الإعدادات والملف الشخصي ونظام النقاط
# ============================================================
# ============================================================
# لوحة الإعدادات — Components V2 احترافية بأقسام على شكل صفحات
# ============================================================
SETTINGS_PAGE_HOME = "home"
SETTINGS_PAGE_EXTRACTION = "extraction"
SETTINGS_PAGE_TRANSLATION = "translation"


class SettingsPanel(discord.ui.LayoutView):
    """
    لوحة إعدادات فاخرة بنظام Components V2:
    - الصفحة الرئيسية: شرح الأمر وأقسامه + سيلكت منيو لاختيار القسم
    - صفحة الاستخراج: صيغة الناتج، المسافات، المؤثرات
    - صفحة الترجمة: تفعيل الترجمة الأدبية واختيار اللغة
    - زر رجوع في كل صفحة فرعية
    - علنية (غير خاصة) — لكن التغيير لصاحب اللوحة فقط
    """

    def __init__(self, user):
        super().__init__(timeout=900)
        self.user = user
        self.profile = get_user_profile(user)
        self.page = SETTINGS_PAGE_HOME
        self.build()

    # ---------- أدوات مساعدة ----------
    async def _guard(self, interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "{emoji:lock} هذه اللوحة ليست لك، افتح نسختك الخاصة عبر `/setting`.",
                ephemeral=True,
            )
            return False
        return True

    def _settings(self):
        return self.profile["settings"]

    def _target_label(self):
        return TRANSLATION_TARGETS.get(self._settings().get("translate_target", "ar"), "العربية")

    def _add_sections(self, container, sections):
        for idx, part in enumerate(sections):
            if idx:
                container.add_item(discord.ui.Separator(visible=True))
            container.add_item(discord.ui.TextDisplay(emojize(part)))

    def _add_back_row(self, container):
        back_row = discord.ui.ActionRow()
        back_btn = discord.ui.Button(
            label="رجوع للصفحة الرئيسية",
            style=discord.ButtonStyle.secondary,
        )
        back_btn.callback = self.on_back
        back_row.add_item(back_btn)
        container.add_item(back_row)

    # ---------- بناء الصفحات ----------
    def build(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Colour(THEMES["gold"]["color"]))
        if self.page == SETTINGS_PAGE_HOME:
            self._build_home(container)
        elif self.page == SETTINGS_PAGE_EXTRACTION:
            self._build_extraction(container)
        else:
            self._build_translation(container)
        self.add_item(container)

    def _build_home(self, container):
        self._add_sections(container, [
            "# {emoji:settings} مركز إعدادات ZEUS Text",
            (
                "> أهلاً بك في لوحة التحكم الكاملة ببوت استخراج نصوص المانهوا.\n"
                "> من هنا تدير كل ما يخص نتائجك: **صيغة الملف، طريقة الاستخراج، وميزة الترجمة الأدبية**.\n"
                "> **اختر القسم الذي تريده من قائمة الاختيار بالأسفل.**"
            ),
            (
                "### {emoji:photo} قسم الاستخراج\n"
                "> صيغة الناتج `TXT/DOCX` • المسافات بين الفقاعات • المؤثرات الصوتية."
            ),
            (
                "### {emoji:message} قسم الترجمة\n"
                "> تفعيل **الترجمة الأدبية الاحترافية** واختيار لغة الترجمة — معطّلة افتراضيًا."
            ),
            "-# اللوحة عامة ويمكن للجميع رؤيتها، لكن تعديل الإعدادات متاح لصاحب الأمر فقط.",
        ])
        select_row = discord.ui.ActionRow()
        select = discord.ui.Select(
            placeholder="اختر قسم الإعدادات...",
            options=[
                discord.SelectOption(
                    label="قسم الاستخراج",
                    value=SETTINGS_PAGE_EXTRACTION,
                    description="صيغة الناتج والمسافات والمؤثرات",
                    emoji=emoji_manager.partial("photo"),
                ),
                discord.SelectOption(
                    label="قسم الترجمة",
                    value=SETTINGS_PAGE_TRANSLATION,
                    description="الترجمة الأدبية ولغة الترجمة",
                    emoji=emoji_manager.partial("message"),
                ),
            ],
        )
        select.callback = self.on_section_select
        select_row.add_item(select)
        container.add_item(select_row)

    def _build_extraction(self, container):
        s = self._settings()
        self._add_sections(container, [
            "# {emoji:photo} قسم الاستخراج",
            "> تحكم بكل تفاصيل ناتج الاستخراج من هنا. كل تغيير يُحفظ **فورًا** على حسابك ويُطبق على الفصول القادمة.",
            (
                f"### {emoji_manager.placeholder('list')} صيغة الناتج — الحالية: `{s.get('output_format', 'txt').upper()}`\n"
                "> - **TXT** — ملف نصي عادي، خفيف ومتوافق مع كل الأجهزة والبرامج.\n"
                "> - **DOCX** — ملف Word منسق، كل فقاعة تظهر كفقرة مستقلة جاهزة للتحرير."
            ),
            (
                f"### {emoji_manager.placeholder('infocircle')} المسافات بين الفقاعات — الحالية: `{fmt_bool(s.get('bubble_spacing', True))}`\n"
                "> - **مفعّل:** سطر فارغ بين كل فقاعة حوار لقراءة مريحة.\n"
                "> - **معطّل:** نص متصل بدون فراغات."
            ),
            (
                f"### {emoji_manager.placeholder('bellringing')} المؤثرات الصوتية — الحالية: `{fmt_bool(s.get('include_sfx', False))}`\n"
                "> - **معطّل (الافتراضي والموصى به):** تجاهل تام لأصوات مثل `بووم!` و`كراش!` والكلمات الجانبية، ويُخرج الحوار فقط.\n"
                "> - **مفعّل:** استخراج المؤثرات والأصوات والنصوص الجانبية كما تظهر في الصورة."
            ),
        ])
        fmt_row = discord.ui.ActionRow()
        fmt_select = discord.ui.Select(
            placeholder="صيغة الناتج",
            options=[
                discord.SelectOption(label="TXT — ملف نص عادي", value="txt", emoji=emoji_manager.partial("list"), default=s.get("output_format", "txt") == "txt"),
                discord.SelectOption(label="DOCX — ملف Word", value="docx", emoji=emoji_manager.partial("briefcase"), default=s.get("output_format", "txt") == "docx"),
            ],
        )
        fmt_select.callback = self.on_format_select
        fmt_row.add_item(fmt_select)
        container.add_item(fmt_row)

        spacing_row = discord.ui.ActionRow()
        spacing_select = discord.ui.Select(
            placeholder="المسافات بين الفقاعات",
            options=[
                discord.SelectOption(label="مفعّل — سطر فارغ بين كل فقاعة", value="on", emoji=emoji_manager.partial("circlecheck"), default=s.get("bubble_spacing", True) is True),
                discord.SelectOption(label="معطّل — نص متصل بدون فراغات", value="off", emoji=emoji_manager.partial("circlex"), default=s.get("bubble_spacing", True) is False),
            ],
        )
        spacing_select.callback = self.on_spacing_select
        spacing_row.add_item(spacing_select)
        container.add_item(spacing_row)

        sfx_row = discord.ui.ActionRow()
        sfx_select = discord.ui.Select(
            placeholder="المؤثرات الصوتية",
            options=[
                discord.SelectOption(label="مفعّل — استخراج المؤثرات والأصوات", value="on", emoji=emoji_manager.partial("bellringing"), default=s.get("include_sfx", False) is True),
                discord.SelectOption(label="معطّل — تجاهل المؤثرات تمامًا (افتراضي)", value="off", emoji=emoji_manager.partial("circlex"), default=s.get("include_sfx", False) is False),
            ],
        )
        sfx_select.callback = self.on_sfx_select
        sfx_row.add_item(sfx_select)
        container.add_item(sfx_row)

        self._add_back_row(container)

    def _build_translation(self, container):
        s = self._settings()
        enabled = bool(s.get("translate_enabled", False))
        self._add_sections(container, [
            "# {emoji:message} قسم الترجمة الأدبية",
            (
                "> ميزة اختيارية: بعد انتهاء الاستخراج تُعالَج نسخة أدبية ثانية من النص كاملاً — "
                "**نص فقط بدون أي صور** — بأسلوب ترجمة احترافية يُراعي أن كل سطر فقاعة حوار "
                "مستقلة فلا تُدمج الفقاعات، لتصلك نسخة مترجمة بجودة قريبة من الإصدارات الرسمية.\n"
                "> عند التفعيل يصلك **ملف ثانٍ مترجم** بجانب ملف النص الأصلي."
            ),
            (
                f"### {emoji_manager.placeholder('bolt')} حالة الميزة — الحالية: `{fmt_bool(enabled)}`\n"
                "> - **مفعّل:** تترجم كل فصل تلقائيًا بعد استخراجه (تزيد مدة التنفيذ قليلًا).\n"
                "> - **معطّل (الافتراضي):** استخراج فقط بدون أي ترجمة."
            ),
            (
                f"### {emoji_manager.placeholder('infocircle')} لغة الترجمة — الحالية: `{self._target_label()}`\n"
                "> اللغة التي ستُترجم إليها الفصول عند تفعيل الميزة."
            ),
        ])
        toggle_row = discord.ui.ActionRow()
        toggle_select = discord.ui.Select(
            placeholder="حالة الترجمة",
            options=[
                discord.SelectOption(label="مفعّلة — ترجمة كل فصل بعد استخراجه", value="on", emoji=emoji_manager.partial("circlecheck"), default=enabled),
                discord.SelectOption(label="معطّلة — استخراج فقط (افتراضي)", value="off", emoji=emoji_manager.partial("circlex"), default=not enabled),
            ],
        )
        toggle_select.callback = self.on_translate_toggle
        toggle_row.add_item(toggle_select)
        container.add_item(toggle_row)

        lang_row = discord.ui.ActionRow()
        lang_select = discord.ui.Select(
            placeholder="لغة الترجمة",
            options=[
                discord.SelectOption(label=f"الترجمة إلى {name}", value=code, emoji=emoji_manager.partial("message"), default=code == s.get("translate_target", "ar"))
                for code, name in TRANSLATION_TARGETS.items()
            ],
        )
        lang_select.callback = self.on_translate_lang
        lang_row.add_item(lang_select)
        container.add_item(lang_row)

        self._add_back_row(container)

    # ---------- الأحداث ----------
    async def _refresh(self, interaction):
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_section_select(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.page = interaction.data["values"][0]
        await self._refresh(interaction)

    async def on_back(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.page = SETTINGS_PAGE_HOME
        await self._refresh(interaction)

    async def on_format_select(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.profile = update_user_settings(self.user, output_format=interaction.data["values"][0])
        await self._refresh(interaction)

    async def on_spacing_select(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.profile = update_user_settings(self.user, bubble_spacing=interaction.data["values"][0] == "on")
        await self._refresh(interaction)

    async def on_sfx_select(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.profile = update_user_settings(self.user, include_sfx=interaction.data["values"][0] == "on")
        await self._refresh(interaction)

    async def on_translate_toggle(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.profile = update_user_settings(self.user, translate_enabled=interaction.data["values"][0] == "on")
        await self._refresh(interaction)

    async def on_translate_lang(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.profile = update_user_settings(self.user, translate_target=interaction.data["values"][0])
        await self._refresh(interaction)


def profile_panel_view(user, profile):
    """
    بطاقة بروفايل احترافية بنظام Components V2:
    - أقسام واضحة بينها فواصل مرئية
    - ترويسة فيها اسم المستخدم مع صورته الشخصية
    - كل قسم مقسم على أسطر متعددة بماركداون ديسكورد فاخر
    """
    if not V2_SUPPORTED:
        return None
    settings = profile["settings"]
    target_label = TRANSLATION_TARGETS.get(settings.get("translate_target", "ar"), "العربية")
    points = int(profile.get("points", 0))
    extractions = int(profile.get("total_extractions", 0))
    blocked = bool(profile.get("is_blocked"))

    container = discord.ui.Container(accent_colour=discord.Colour(THEMES["gold"]["color"]))

    # الترويسة — الصورة الشخصية + اسم المستخدم
    header_display = discord.ui.TextDisplay(emojize(
        f"# {emoji_manager.placeholder('user')} الملف الشخصي\n"
        f"### <@{user.id}>"
    ))
    try:
        header = discord.ui.Section(header_display, accessory=discord.ui.Thumbnail(media=user.display_avatar.url))
    except Exception:
        header = header_display
    container.add_item(header)
    container.add_item(discord.ui.Separator(visible=True))

    # قسم النقاط
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('star')} النقاط\n"
        f"> الرصيد الحالي: `{points}` نقطة\n"
        f"> كل استخراج أو ترجمة ناجحة تستهلك `1` نقطة — وإذا كان للسيرفر رصيد يُخصم منه أولًا"
    )))
    container.add_item(discord.ui.Separator(visible=True))

    # قسم الإحصائيات
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('chartpie')} الإحصائيات\n"
        f"> إجمالي الاستخراجات: `{extractions}` فصل\n"
        f"> الحالة: `{'محظور' if blocked else 'نشط'}`"
    )))
    container.add_item(discord.ui.Separator(visible=True))

    # قسم إعدادات الإخراج
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('settings')} إعدادات الإخراج\n"
        f"> صيغة الناتج: `{str(settings.get('output_format', 'txt')).upper()}`\n"
        f"> المسافات بين الفقاعات: `{fmt_bool(settings.get('bubble_spacing', True))}`\n"
        f"> المؤثرات الصوتية: `{fmt_bool(settings.get('include_sfx', False))}`"
    )))
    container.add_item(discord.ui.Separator(visible=True))

    # قسم الترجمة الأدبية
    container.add_item(discord.ui.TextDisplay(emojize(
        f"### {emoji_manager.placeholder('message')} الترجمة الأدبية\n"
        f"> الحالة: `{fmt_bool(settings.get('translate_enabled', False))}`\n"
        f"> لغة الترجمة: `{target_label}`"
    )))

    # تنبيه الحظر إن وجد
    if blocked:
        container.add_item(discord.ui.Separator(visible=True))
        container.add_item(discord.ui.TextDisplay(emojize(
            f"### {emoji_manager.placeholder('alerttriangle')} تنبيه\n> هذا الحساب محظور من استخدام البوت."
        )))

    # التذييل
    container.add_item(discord.ui.Separator(visible=True))
    container.add_item(discord.ui.TextDisplay(emojize(
        f"-# {emoji_manager.placeholder('shield')} ZEUS Text • الإصدار `{BOT_VERSION}`"
    )))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def profile_embed(user, profile):
    """بديل احتياطي عند عدم دعم Components V2 في نسخة المكتبة."""
    settings = profile["settings"]
    target_label = TRANSLATION_TARGETS.get(settings.get("translate_target", "ar"), "العربية")
    embed = themed_embed(
        title="{emoji:user} بروفايل المستخدم",
        description=(
            f"# <@{user.id}>\n{line()}\n"
            f"{emoji_manager.placeholder('star')} **النقاط المتاحة:** `{profile.get('points', 0)}`\n{line()}\n"
            f"{emoji_manager.placeholder('chartpie')} **إجمالي الاستخراجات:** `{profile.get('total_extractions', 0)}`\n{line()}\n"
            f"{emoji_manager.placeholder('shield')} **الحالة:** `{'محظور' if profile.get('is_blocked') else 'نشط'}`\n{line()}\n"
            f"{emoji_manager.placeholder('settings')} **الإخراج:** `{settings.get('output_format', 'txt').upper()}` | "
            f"**المسافات:** `{fmt_bool(settings.get('bubble_spacing', True))}` | "
            f"**المؤثرات:** `{fmt_bool(settings.get('include_sfx', False))}`\n{line()}\n"
            f"{emoji_manager.placeholder('message')} **الترجمة الأدبية:** `{fmt_bool(settings.get('translate_enabled', False))}` | "
            f"**اللغة:** `{target_label}`"
        ),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed


# ============================================================
# الأوامر
# ============================================================
@bot.event
async def on_ready():
    print(emojize(f"{emoji_manager.placeholder('circlecheck')} ZEUS text bot جاهز (v{BOT_VERSION})"))
    try:
        synced = await bot.tree.sync()
        print(f"تمت مزامنة {len(synced)} أمر.")
    except Exception as e:
        print(f"فشل مزامنة الأوامر: {e}")
    # نظام الإيموجي المخصص:
    # - يُحمّل أولاً من utils/emojis.json إذا كان مطابقًا لتطبيق هذا البوت.
    # - إذا كانت الخريطة فارغة (لا إيموجيات مخصصة) تُجبر المزامنة على العمل
    #   حتى لو كان SYNC_APPLICATION_EMOJIS=false، لأن بدونها ستظهر الإيموجيات العادية.
    # - تُنفذ مرة واحدة فقط لكل جلسة (on_ready قد يتكرر مع إعادة الاتصال).
    if not getattr(bot, "_zeus_emoji_setup_done", False):
        bot._zeus_emoji_setup_done = True
        try:
            emoji_manager.load(bot.user.id)
            sync_enabled = os.getenv("SYNC_APPLICATION_EMOJIS", "true").lower() == "true"
            has_custom = any(not key.startswith("__") for key in emoji_manager.map)
            if sync_enabled or not has_custom:
                if not sync_enabled:
                    print("[EmojiSetup] الخريطة فارغة وستُجبر المزامنة رغم تعطيل الإعداد.")
                result = await asyncio.to_thread(emoji_manager.sync_application_emojis, bot.user.id, BOT_TOKEN)
                emoji_manager.load(bot.user.id)
                print(f"[EmojiSetup] {result}")
            else:
                print(f"[EmojiSetup] تم تحميل {sum(1 for k in emoji_manager.map if not k.startswith('__'))} إيموجي مخصص من الذاكرة المحلية.")
        except Exception as e:
            emoji_manager.load(bot.user.id)
            print(f"[EmojiSetup] failed: {e}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ZEUS Text | /help"))


@bot.tree.command(name="extract", description="استخراج النصوص من صور المانجا")
async def extract(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    user_id = interaction.user.id
    guild_id = interaction.guild_id
    profile = get_user_profile(interaction.user)

    if profile.get("is_blocked"):
        await interaction.followup.send("{emoji:lock} **تم منعك من استخدام البوت.**", ephemeral=True)
        return

    # فحص النقاط: سيرفر أولاً ثم مستخدم
    has_points = (guild_id and server_has_points(guild_id)) or int(profile.get("points", 0)) > 0
    if not has_points:
        support = f"\n{emoji_manager.placeholder('ticket')} افتح تذكرة تجديد النقاط: {SUPPORT_SERVER_URL}" if SUPPORT_SERVER_URL else ""
        await interaction.followup.send(
            f"{emoji_manager.placeholder('circlex')} **لا تملك نقاطًا كافية.**\nكل فصل يستهلك نقطة واحدة.{support}",
            ephemeral=True,
        )
        return

    settings = profile["settings"]

    view = ModeSelectView(user_id)
    await send_mode_selection(interaction.followup, view)
    await view.wait()
    if not view.confirmed:
        await interaction.followup.send("{emoji:clock} **انتهى وقت اختيار وضع المعالجة. أعد تشغيل الأمر عندما تكون جاهزًا.**", ephemeral=True)
        return
    thinking_enabled = view.thinking_enabled

    await send_upload_prompt(interaction.followup)

    try:
        msg = await bot.wait_for(
            'message',
            check=lambda m: m.author == interaction.user and m.channel == interaction.channel,
            timeout=300,
        )
    except asyncio.TimeoutError:
        await interaction.followup.send("{emoji:clock} **انتهى الوقت، أعد الأمر مرة أخرى.**", ephemeral=True)
        return

    status_msg = await send_status(interaction.channel, "جاري قراءة المصدر", "استلمت المصدر، وسأحدّث هذه اللوحة أثناء التحميل والاستخراج.")
    images = []
    source_name = "zeus_1"

    if msg.attachments:
        if len(msg.attachments) > MAX_IMAGES_PER_REQUEST:
            await edit_status(status_msg, "{emoji:circlex} خطأ", f"الحد الأقصى {MAX_IMAGES_PER_REQUEST} صور.", error=True)
            return
        for att in msg.attachments:
            if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                data = ensure_valid_image(await att.read(), att.filename)
                images.append((data, att.filename))
            elif att.filename.lower().endswith(".zip"):
                data = await att.read()
                images.extend(await asyncio.to_thread(extract_images_from_zip_bytes, data))
                source_name = Path(att.filename).stem
            else:
                await edit_status(status_msg, "{emoji:circlex} ملف غير مدعوم", f"الملف `{att.filename}` غير مدعوم — الصور و `ZIP` فقط.", error=True)
                return
    elif msg.content.startswith("http"):
        link = msg.content.strip()
        try:
            await edit_status(status_msg, "جاري تحميل الرابط", "قد يستغرق Google Drive وقتًا أطول حسب حجم الفصل.")
            if "drive.google.com" in link:
                images, source_name = await asyncio.to_thread(process_drive_link, link, include_name=True)
            else:
                data = await asyncio.to_thread(download_image_from_url, link)
                if zipfile.is_zipfile(io.BytesIO(data)):
                    images = extract_images_from_zip_bytes(data)
                    source_name = "zeus_1"
                else:
                    images.append((data, "downloaded_image.jpg"))
                    source_name = "zeus_1"
        except Exception as e:
            await edit_status(status_msg, "{emoji:circlex} فشل معالجة الرابط", f"`{str(e)}`", error=True)
            return
    else:
        await edit_status(status_msg, "{emoji:circlex} مصدر غير صالح", "أرسل صورًا أو ملف ZIP أو رابطًا صالحًا.", error=True)
        return

    if not images:
        await edit_status(status_msg, "{emoji:circlex} لا توجد صور", "لم يتم العثور على صور صالحة.", error=True)
        return

    images.sort(key=lambda x: natural_sort_key(x[1]))
    extraction_started_at = time.monotonic()
    total_images = len(images)
    await edit_status(status_msg, "بدأ الاستخراج الآن", f"تم العثور على `{total_images}` صورة.", current=0, total=total_images)

    combined_text, empty_images, failed_pages = await run_extraction_pipeline(status_msg, user_id, images, thinking_enabled, settings)

    # مرحلة الترجمة الأدبية (اختيارية — معطلة افتراضيًا)
    translated_text = None
    translated_error = None
    if settings.get("translate_enabled", False) and combined_text.strip():
        await edit_status(
            status_msg,
            "جاري الترجمة الأدبية",
            "أعِدت معالجة النص المستخرج أدبياً — المعالجة نصية فقط وقد تطول قليلاً حسب طول الفصل.",
        )
        try:
            translated_text = await asyncio.to_thread(translate_chapter_text, user_id, combined_text, settings)
        except Exception as exc:
            translated_error = str(exc)

    ok, profile_after, point_source = await resolve_and_consume_point(user_id, guild_id)
    if not ok:
        await edit_status(status_msg, "{emoji:circlex} لا توجد نقاط", "لا توجد نقاط كافية لإرسال النتيجة.", error=True)
        return

    await status_msg.close(delete=True)
    await deliver_extraction_result(
        interaction.channel,
        user_id,
        combined_text=combined_text,
        translated_text=translated_text,
        translated_error=translated_error,
        settings=settings,
        source_name=source_name,
        output_format=settings.get("output_format", "txt"),
        total_images=total_images,
        empty_images=empty_images,
        extraction_started_at=extraction_started_at,
        profile_after=profile_after,
        point_source=point_source,
        failed_pages=failed_pages,
    )
    increment_account_usage(user_id, "ocr")


def help_sections():
    """دليل استخدام شامل ومفصل — يشرح ما هو البوت، وكيف يعمل، وخطوات كل أمر."""
    return [
        "# {emoji:photo} ZEUS Text Bot — الدليل الشامل",
        (
            "> بوت متخصص في **استخراج نصوص المانجا والمانهوا** من الصور تلقائيًا وبجودة عالية، "
            "ثم يسلّمها لك **ملفًا منظّمًا** داخل الرسالة مباشرة — مع ميزة **ترجمة أدبية احترافية** اختيارية.\n"
            "> كل عملية استخراج أو ترجمة ناجحة تستهلك **نقطة واحدة** فقط."
        ),
        "## {emoji:playerplay} الأمر `/extract` — الاستخراج خطوة بخطوة",
        (
            "> **ماذا يفعل؟** يبدأ جلسة استخراج كاملة لفصل من أول صفحة لآخرها.\n"
            "> **كيف يعمل؟ اتبع هذه الخطوات بالترتيب:**\n"
            "> {emoji:num_1} نفّذ `/extract` وتظهر لك خيارات المعالجة: **سرعة عالية** (موصى به — نتيجة أسرع بدقة ممتازة) "
            "أو **دقة كاملة** (معالجة أعمق للفصول الصعبة جدًا لكن أبطأ بكثير).\n"
            "> {emoji:num_2} أرسل صور الفصل بإحدى **ثلاث طرق**:\n"
            f"> • إرفاق ما يصل إلى `{MAX_IMAGES_PER_REQUEST}` صور مباشرة (رتبها بالأرقام).\n"
            "> • ملف `ZIP` داخله صور الفصل مرتبة الأسماء.\n"
            "> • رابط `Google Drive` لمجلد أو ملف — وسيقرأ حتى `1000` صورة.\n"
            "> {emoji:num_3} تظهر **لوحة حالة حية** والصفحات تُعالج **بالتوازي** لتقصير المدة — "
            "وإذا تعذّرت قراءة صفحة سيحاول البوت **إعادة المحاولة تلقائيًا** قبل الاستسلام، "
            "وملف الاستخراج يخرج دائمًا **باللغة الأصلية** كما في الصور تمامًا (لا ترجمة فيه إطلاقًا).\n"
            "> {emoji:num_4} تصلك لوحة النتيجة: الملف داخلها مباشرة + **تقرير جودة** (الصور الفارغة، المدة، الصيغة).\n"
            "> **ملاحظات:** النص يُرتّب حسب أسماء الصور (مرّتها = مرتب الناتج)، ويُفصل كل صفحة بسطر `## اسم الصورة`.\n"
            "> **عند تفعيل الترجمة** من الإعدادات تصلك لوحة بملفين: **النص المستخرج (اللغة الأصلية)** + **الترجمة الأدبية**."
        ),
        "## {emoji:zap} الصفحات الشريطية الطويلة — تقسيم عند الحاجة فقط",
        (
            "> كثير من فصول المانهوا تُدمج عموديًا في صورة واحدة بالغة الارتفاع، "
            "وهذا يجعل النص **غير مقروء** لأن الصورة تُضغط بعنف مهما كانت جودتها.\n"
            "> لا يوجد أي تقسيم في الوضع الطبيعي — الصفحة تُقرأ **كاملة أولًا** دائمًا، "
            "وفقط إذا فشلت صفحة **لكبر حجمها** يبدأ التقسيم تلقائيًا بحد أقصى **3 أجزاء** للصفحة كلها، "
            "مع دمج ذكي يُزيل أي تكرار عند الحدود وتحديد دقيق لما فشل بالضبط.\n"
            "> ستشاهد في مؤشر الانتظار رسالة واضحة مثل: **فشل استخراج صورة لكبر حجمها — جاري تقسيمها** "
            "ثم تقدم كل جزء (الجزء `1` من `3`...).\n"
            "> وإذا كان أحد الأجزاء نفسه أطول من اللازم يُعاد تقسيمه **وحده** — ضمان إضافي نادر للصفحات العملاقة."
        ),
        "## {emoji:message} الأمر `/translate` — ترجمة فصل جاهز",
        (
            "> **ماذا يفعل؟** يترجم ملف فصل **جاهز** (نص مستخرج سابقًا) بأسلوب ترجمة أدبية احترافية "
            "يحافظ على كل سطر حوار كفقاعة مستقلة دون دمج الفقاعات، "
            "ودائمًا **فصحى أدبية** بلا عامية ولا لهجات و**بدون حركات/تشكيل** إلا للضرورة القصوى — "
            "في الوضعين (السرعة والدقة) على حد سواء.\n"
            "> ويخضع الناتج ل**مراجعة آلية** تترجم أي مقطع أجنبي متبقٍ وتلصقه مكانه "
            "(مع إبقاء حروف التصنيف مثل `A` `S` `SS` `C` كما هي).\n"
            "> **كيف يعمل؟ نفس تفاعل `/extract` تمامًا:**\n"
            "> {emoji:num_1} نفّذ `/translate` واختر وضع المعالجة: **سرعة عالية** (بدون تفكير — أسرع) أو **دقة كاملة** (مع تفكير — أعمق وأبطأ).\n"
            "> {emoji:num_2} أرسل المصدر: ملف `TXT` أو `DOCX` مرفقًا في رسالة — أو الصق نص الفصل مباشرة في رسالة واحدة.\n"
            "> {emoji:num_3} تظهر **لوحة حالة حية** — والفصول الطويلة تُترجم على أجزاء متتابعة مع عرض تقدم واضح (مثل: الجزء `2` من `4`).\n"
            "> {emoji:num_4} تصلك لوحة النتيجة والملف المترجم داخلها مباشرة.\n"
            "> **ملاحظات:** الترجمة تستهلك **نقطة واحدة** مثل الاستخراج، وتعمل أيضًا بالبريفكس `!ترجمة`، "
            "ولغتها من إعداداتك في `/setting` أو بتمريرها مع الأمر مباشرة."
        ),
        "## {emoji:settings} الأمر `/setting` — الإعدادات",
        (
            "> **ماذا يفعل؟** يفتح لوحة إعدادات **علنية** بأقسام على شكل صفحات: تختار القسم من القائمة، وتضغط زر الرجوع للعودة.\n"
            "> **{emoji:photo} قسم الاستخراج:** صيغة الناتج (`TXT` نص عادي / `DOCX` وورد)، المسافات بين الفقاعات، "
            "والمؤثرات الصوتية (**معطّلة افتراضيًا** لتجاهل الأصوات مثل `بووم!`).\n"
            "> **{emoji:message} قسم الترجمة:** تفعيل الترجمة الأدبية (**معطّلة افتراضيًا**) واختيار اللغة — وستُترجم الفصول تلقائيًا بعد كل استخراج."
        ),
        "## {emoji:user} الأمر `/profile` — البروفايل",
        (
            "> **ماذا يفعل؟** يعرض بطاقة احترافية بحسابك مقسمة إلى أقسام واضحة: النقاط، الإحصائيات، إعدادات الإخراج، وحالة الترجمة الأدبية.\n"
            "> **إضافي:** `/profile @العضو` لعرض بروفايل عضو آخر، ويعمل أيضًا بالبريفكس `!بروفايل`."
        ),
        "## {emoji:star} النقاط والتكلفة",
        (
            "> كل مستخدم جديد يبدأ بـ **5 نقاط مجانية**.\n"
            "> كل فصل ناجح = **نقطة واحدة** حتى لو احتوى بعض الصور على نص قليل.\n"
            "> **أولوية السيرفر:** إذا كان للسيرفر رصيد نقاط يُخصم **منه تلقائيًا** قبل رصيدك الشخصي — "
            "فلن تُلمس نقاطك طالما رصيد السيرفر متوفر.\n"
            "> **نفدت النقاط؟** افتح تذكرة في سيرفر الدعم لتجديدها."
        ),
        "## {emoji:alerttriangle} حدود وملاحظات",
        (
            f"> • الحد الأقصى `{MAX_IMAGES_PER_REQUEST}` صور في الرسالة الواحدة — للفصول الطويلة استخدم `ZIP` أو `Google Drive`.\n"
            "> • الصيغ المدعومة للاستخراج: `PNG` `JPG` `JPEG` `WEBP` `BMP` — وللترجمة: `TXT` `DOCX`.\n"
            "> • الصورة التي تظهر (فارغة) في التقرير تعني أنها **بلا نص مقروء فعلاً** حتى بعد التقسيم وإعادة المحاولة.\n"
            "> • عند تفعيل الترجمة تتم معالجة نصية إضافية بعد الاستخراج، فتطول مدة التنفيذ قليلًا.\n"
            "> • أوامر البريفكس المتاحة: `!استخراج` `!ترجمة` `!اعدادات` `!بروفايل` `!مساعدة`."
        ),
    ]


@bot.tree.command(name="help", description="دليل استخدام البوت الشامل")
async def help_command(interaction: discord.Interaction):
    sections = help_sections()
    view = components_v2_panel(sections=sections)
    if view:
        await interaction.response.send_message(view=view)
    else:
        await interaction.response.send_message(embed=themed_embed(description="\n\n".join(sections)))


@bot.tree.command(name="setting", description="لوحة إعدادات الاستخراج والترجمة — علنية للجميع")
async def setting(interaction: discord.Interaction):
    if V2_SUPPORTED:
        await interaction.response.send_message(view=SettingsPanel(interaction.user), ephemeral=False)
    else:
        await interaction.response.send_message(
            embed=themed_embed(
                title="{emoji:settings} مركز إعدادات ZEUS Text",
                description=(
                    "أقسام اللوحة غير مدعومة في نسخة discord.py الحالية.\n"
                    "حدّث المكتبة إلى `discord.py>=2.6` للحصول على اللوحة التفاعلية الكاملة.\n\n"
                    "**الإعدادات المتاحة:** صيغة الناتج، المسافات بين الفقاعات، المؤثرات الصوتية، الترجمة الأدبية ولغتها."
                ),
            ),
            ephemeral=False,
        )


def translation_upload_sections():
    return [
        f"## {emoji_manager.placeholder('message')} الخطوة الثانية — أرسل الفصل المطلوب ترجمته.",
        f"{emoji_manager.placeholder('folderopen')} **الطريقة الأولى:** أرفق ملف الفصل بصيغة `TXT` أو `DOCX` في رسالة واحدة.",
        f"{emoji_manager.placeholder('list')} **الطريقة الثانية:** الصق نص الفصل مباشرة في رسالة واحدة.",
        f"{emoji_manager.placeholder('clock')} لديك `5` دقائق لإرسال المصدر من لحظة هذه اللوحة.",
        f"-# {emoji_manager.placeholder('infocircle')} الترجمة تستهلك نقطة واحدة مثل الاستخراج — والفصول الطويلة تُترجم على أجزاء تلقائيًا مع عرض التقدم.",
    ]


async def send_translation_upload_prompt(destination):
    return await send_panel(destination, sections=translation_upload_sections())


@bot.tree.command(name="translate", description="ترجمة فصل جاهز — اختر وضع المعالجة ثم أرسل ملف TXT/DOCX أو الصق النص")
@app_commands.describe(
    file="ملف الفصل بصيغة TXT أو DOCX",
    text="أو الصق نص الفصل مباشرة",
    language="لغة الترجمة — الافتراضي من إعداداتك",
)
@app_commands.choices(language=[
    app_commands.Choice(name=f"الترجمة إلى {name}", value=code)
    for code, name in TRANSLATION_TARGETS.items()
])
async def translate(interaction: discord.Interaction,
                    file: Optional[discord.Attachment] = None,
                    text: Optional[str] = None,
                    language: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer(thinking=True)
    user_id = interaction.user.id
    guild_id = interaction.guild_id
    profile = get_user_profile(interaction.user)

    if profile.get("is_blocked"):
        await interaction.followup.send("{emoji:lock} **تم منعك من استخدام البوت.**", ephemeral=True)
        return

    has_points = (guild_id and server_has_points(guild_id)) or int(profile.get("points", 0)) > 0
    if not has_points:
        support = f"\n{emoji_manager.placeholder('ticket')} افتح تذكرة تجديد النقاط: {SUPPORT_SERVER_URL}" if SUPPORT_SERVER_URL else ""
        await interaction.followup.send(
            f"{emoji_manager.placeholder('circlex')} **لا تملك نقاطًا كافية.**\nكل ترجمة تستهلك نقطة واحدة.{support}",
            ephemeral=True,
        )
        return

    target = language.value if language else profile["settings"].get("translate_target", "ar")

    # الخطوة الأولى — اختيار وضع المعالجة (نفس تفاعل /extract تمامًا)
    view = ModeSelectView(user_id)
    await send_mode_selection(interaction.followup, view)
    await view.wait()
    if not view.confirmed:
        await interaction.followup.send("{emoji:clock} **انتهى وقت اختيار وضع المعالجة. أعد تشغيل الأمر عندما تكون جاهزًا.**", ephemeral=True)
        return
    thinking_enabled = view.thinking_enabled

    chapter_text = ""
    source_stem = "chapter"

    if file is not None:
        if not file.filename.lower().endswith((".txt", ".docx")):
            await interaction.followup.send("{emoji:circlex} **صيغة غير مدعومة** — أرفق ملف `TXT` أو `DOCX` فقط.", ephemeral=True)
            return
        if file.size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
            await interaction.followup.send(
                f"{{emoji:circlex}} **حجم الملف كبير جدًا** — الحد الأقصى `{MAX_IMAGE_SIZE_MB}` ميغابايت.",
                ephemeral=True,
            )
            return
        try:
            data = await file.read()
            chapter_text = read_upload_chapter_text(file.filename, data)
            source_stem = Path(file.filename).stem
        except Exception as exc:
            await interaction.followup.send(f"{{emoji:circlex}} {exc}", ephemeral=True)
            return
    elif text and text.strip():
        chapter_text = text.strip()
    else:
        # لا ملف ولا نص — اطلب الإرسال في الرسالة التالية
        await send_translation_upload_prompt(interaction.followup)
        try:
            msg = await bot.wait_for(
                "message",
                check=lambda m: m.author == interaction.user and m.channel == interaction.channel,
                timeout=300,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send("{emoji:clock} **انتهى الوقت، أعد الأمر مرة أخرى.**", ephemeral=True)
            return
        try:
            if msg.attachments:
                att = msg.attachments[0]
                if not att.filename.lower().endswith((".txt", ".docx")):
                    await interaction.followup.send("{emoji:circlex} **صيغة غير مدعومة** — أرفق ملف `TXT` أو `DOCX` فقط.", ephemeral=True)
                    return
                data = await att.read()
                chapter_text = read_upload_chapter_text(att.filename, data)
                source_stem = Path(att.filename).stem
            elif msg.content.strip():
                chapter_text = msg.content.strip()
            else:
                await interaction.followup.send("{emoji:circlex} **لم أستلم ملفًا أو نصًا صالحًا.**", ephemeral=True)
                return
        except Exception as exc:
            await interaction.followup.send(f"{{emoji:circlex}} {exc}", ephemeral=True)
            return

    status_msg = await send_status(interaction.channel, "جاري الترجمة الأدبية", "استلمت الفصل وسأحدّث هذه اللوحة حتى انتهاء الترجمة.")
    await run_translation_with_status(
        status_msg,
        interaction.channel,
        user_id,
        guild_id,
        chapter_text,
        source_stem,
        target,
        profile["settings"].get("output_format", "txt"),
        thinking_enabled=thinking_enabled,
    )


@bot.tree.command(name="profile", description="عرض ملفك أو بروفايل شخص آخر في بوت استخراج النصوص")
@app_commands.describe(user="المستخدم المراد عرض بروفايله")
async def profile(interaction: discord.Interaction, user: Optional[discord.User] = None):
    target = user or interaction.user
    profile_data = get_user_profile(target)
    view = profile_panel_view(target, profile_data)
    if view:
        await interaction.response.send_message(view=view)
    else:
        await interaction.response.send_message(embed=profile_embed(target, profile_data))


def parse_user_id(value):
    match = re.search(r"(\d{15,22})", str(value))
    if not match:
        raise ValueError("missing user id")
    return match.group(1)


# ============================================================
# Modal إدارة نقاط السيرفر
# ============================================================
class ServerPointsModal(discord.ui.Modal, title="إدارة نقاط السيرفر"):
    guild_input = discord.ui.TextInput(
        label="ID السيرفر",
        placeholder="أدخل ID السيرفر",
        required=True,
    )
    amount_input = discord.ui.TextInput(
        label="النقاط المضافة",
        placeholder="عدد النقاط (مثال: 10)",
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("ليست مصرح.", ephemeral=True)
            return
        try:
            guild_id = int(str(self.guild_input.value).strip())
            amount = int(str(self.amount_input.value).strip())
        except ValueError:
            await interaction.response.send_message("{emoji:circlex} قيمة غير صحيحة.", ephemeral=True)
            return
        profile_data = admin_adjust_server(guild_id, points_delta=amount)
        await interaction.response.send_message(
            f"{{emoji:circlecheck}} **تمت إضافة `{amount}` نقطة للسيرفر `{guild_id}`. الرصيد: `{profile_data.get('points', 0)}`**",
            ephemeral=True,
        )


# ============================================================
# لوحة الإدارة
# ============================================================
class AdminActionModal(discord.ui.Modal):
    def __init__(self, action):
        super().__init__(title="لوحة التحكم")
        self.action = action
        self.user_id = discord.ui.TextInput(label="ID المستخدم", placeholder="123456789 أو منشن", required=True)
        self.amount = discord.ui.TextInput(label="النقاط", placeholder="اتركها فارغة للحظر/فك الحظر", required=False)
        self.add_item(self.user_id)
        if action in {"add", "reset"}:
            self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("ليست مصرح لاستخدام الأمر.", ephemeral=True)
            return
        target = parse_user_id(str(self.user_id.value))
        try:
            amount = int(str(self.amount.value or "0"))
        except ValueError:
            await interaction.response.send_message("{emoji:circlex} قيمة النقاط غير صحيحة.", ephemeral=True)
            return
        if self.action == "add":
            profile_data = admin_adjust_user(target, points_delta=amount)
            msg = f"تمت إضافة `{amount}` نقطة. الرصيد: `{profile_data.get('points', 0)}`"
        elif self.action == "reset":
            profile_data = admin_adjust_user(target, set_points=amount)
            msg = f"تم ضبط النقاط إلى `{profile_data.get('points', 0)}`"
        elif self.action == "block":
            admin_adjust_user(target, blocked=True)
            msg = "تم منع المستخدم."
        else:
            admin_adjust_user(target, blocked=False)
            msg = "تم فك منع المستخدم."
        await interaction.response.send_message(f"{{emoji:circlecheck}} **{msg}**", ephemeral=True)


class AdminPanelView(discord.ui.View):
    _BUTTON_EMOJIS = {
        "المستخدمون": "chartpie",
        "إضافة نقاط": "star",
        "ضبط النقاط": "adjustments",
        "منع": "lock",
        "فك المنع": "shieldcheck",
        "نقاط السيرفر": "shieldcheck",
        "حسابات OCR": "user",
        "حالة OCR": "infocircle",
    }

    def __init__(self):
        super().__init__(timeout=240)
        # إعادة تطبيق الإيموجي المخصص وقت التشغيل
        for child in self.children:
            emoji_key = self._BUTTON_EMOJIS.get(getattr(child, "label", ""))
            if emoji_key:
                child.emoji = emoji_manager.partial(emoji_key)

    async def interaction_check(self, interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("ليست مصرح لاستخدام الأمر.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="المستخدمون", emoji=emoji_manager.partial("chartpie"), style=discord.ButtonStyle.secondary)
    async def users(self, interaction, button):
        users = list_user_profiles(25)
        body = "\n".join(
            f"`{u.get('user_id')}` • **{u.get('display_name', u.get('username', 'Unknown'))}** • `{u.get('points', 0)}` نقطة • {'محظور' if u.get('is_blocked') else 'نشط'}"
            for u in users
        ) or "لا يوجد مستخدمون بعد."
        await interaction.response.send_message(embed=themed_embed("{emoji:chartpie} آخر المستخدمين", f"{line()}{body}"), ephemeral=True)

    @discord.ui.button(label="إضافة نقاط", emoji=emoji_manager.partial("star"), style=discord.ButtonStyle.secondary)
    async def add(self, interaction, button):
        await interaction.response.send_modal(AdminActionModal("add"))

    @discord.ui.button(label="ضبط النقاط", emoji=emoji_manager.partial("adjustments"), style=discord.ButtonStyle.secondary)
    async def reset(self, interaction, button):
        await interaction.response.send_modal(AdminActionModal("reset"))

    @discord.ui.button(label="منع", emoji=emoji_manager.partial("lock"), style=discord.ButtonStyle.secondary)
    async def block(self, interaction, button):
        await interaction.response.send_modal(AdminActionModal("block"))

    @discord.ui.button(label="فك المنع", emoji=emoji_manager.partial("shieldcheck"), style=discord.ButtonStyle.secondary)
    async def unblock(self, interaction, button):
        await interaction.response.send_modal(AdminActionModal("unblock"))

    @discord.ui.button(label="نقاط السيرفر", emoji=emoji_manager.partial("shieldcheck"), style=discord.ButtonStyle.secondary, row=1)
    async def server_points(self, interaction, button):
        await interaction.response.send_modal(ServerPointsModal())

    @discord.ui.button(label="حسابات OCR", emoji=emoji_manager.partial("user"), style=discord.ButtonStyle.secondary, row=1)
    async def ocr_accounts(self, interaction, button):
        data = load_accounts_data(OWNER_ID)
        if not data["accounts"]:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await asyncio.to_thread(create_and_save_new_account, OWNER_ID)
            except Exception as e:
                await interaction.followup.send(f"{{emoji:circlex}} فشل إنشاء حساب OCR: `{str(e)}`", ephemeral=True)
                return
            await interaction.followup.send(
                embed=themed_embed("{emoji:user} إدارة حسابات OCR", "اختر حسابًا لإدارته."),
                view=AccountsView(OWNER_ID),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=themed_embed("{emoji:user} إدارة حسابات OCR", "اختر حسابًا لإدارته."),
            view=AccountsView(OWNER_ID),
            ephemeral=True,
        )

    @discord.ui.button(label="حالة OCR", emoji=emoji_manager.partial("infocircle"), style=discord.ButtonStyle.secondary, row=1)
    async def ocr_status(self, interaction, button):
        data = load_accounts_data(OWNER_ID)
        now_ts = int(time.time())
        active = sum(1 for a in data["accounts"] if a.get("ocr_limit_until", 0) <= now_ts)
        total_uses = sum(a.get("ocr_count", 0) for a in data["accounts"])
        embed = themed_embed(
            "{emoji:chartpie} حالة OCR",
            f"إجمالي الحسابات: `{len(data['accounts'])}`\nنشطة: `{active}`\nالاستخدامات: `{total_uses}`",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


def admin_panel_embed():
    return themed_embed(
        "{emoji:layoutdashboard} لوحة التحكم",
        f"# مركز إدارة بوت النصوص{line()}اختر الإجراء من الأزرار. كل العمليات مخفية ولا يراها إلا المالك.",
    )


@bot.tree.command(name="zx", description="...")
async def zx(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("ليست مصرح لاستخدام الأمر.", ephemeral=True)
        return
    await interaction.response.send_message(embed=admin_panel_embed(), view=AdminPanelView())


# ============================================================
# أوامر Prefix بعلامة !
# ============================================================
@bot.command(name="help", aliases=["مساعدة", "اوامر"])
async def prefix_help(ctx):
    await send_panel(ctx.channel, sections=help_sections())


@bot.command(name="profile", aliases=["بروفايل"])
async def prefix_profile(ctx, target: Optional[str] = None):
    user = ctx.author
    if target:
        try:
            user_id = int(parse_user_id(target))
            user = ctx.guild.get_member(user_id) if ctx.guild else None
            user = user or await bot.fetch_user(user_id)
        except Exception:
            await ctx.reply("{emoji:circlex} لم أجد المستخدم المطلوب.", mention_author=False)
            return
    profile_data = get_user_profile(user)
    view = profile_panel_view(user, profile_data)
    if view:
        await ctx.send(view=view)
    else:
        await ctx.send(embed=profile_embed(user, profile_data))


@bot.command(name="translate", aliases=["ترجمة", "ترجم"])
async def prefix_translate(ctx):
    user_id = ctx.author.id
    guild_id = ctx.guild.id if ctx.guild else None
    profile = get_user_profile(ctx.author)

    if profile.get("is_blocked"):
        await ctx.reply("{emoji:lock} **تم منعك من استخدام البوت.**", mention_author=False)
        return

    has_points = (guild_id and server_has_points(guild_id)) or int(profile.get("points", 0)) > 0
    if not has_points:
        await ctx.reply("{emoji:circlex} **لا تملك نقاطًا كافية.**\nكل ترجمة تستهلك نقطة واحدة.", mention_author=False)
        return

    target = profile["settings"].get("translate_target", "ar")

    # الخطوة الأولى — اختيار وضع المعالجة (نفس تفاعل !استخراج تمامًا)
    view = ModeSelectView(user_id)
    await send_mode_selection(ctx, view)
    await view.wait()
    if not view.confirmed:
        await ctx.send("{emoji:clock} **انتهى وقت اختيار وضع المعالجة. أعد تشغيل الأمر عندما تكون جاهزًا.**")
        return
    thinking_enabled = view.thinking_enabled

    chapter_text = ""
    source_stem = "chapter"

    try:
        if ctx.message.attachments:
            att = ctx.message.attachments[0]
            if not att.filename.lower().endswith((".txt", ".docx")):
                await ctx.reply("{emoji:circlex} **صيغة غير مدعومة** — أرفق ملف `TXT` أو `DOCX` فقط.", mention_author=False)
                return
            chapter_text = read_upload_chapter_text(att.filename, await att.read())
            source_stem = Path(att.filename).stem
        else:
            inline_text = re.sub(r"^\S+", "", ctx.message.content.strip(), count=1).strip()
            if inline_text:
                chapter_text = inline_text
            else:
                await send_translation_upload_prompt(ctx)
                msg = await bot.wait_for(
                    "message",
                    check=lambda m: m.author == ctx.author and m.channel == ctx.channel,
                    timeout=300,
                )
                if msg.attachments:
                    att = msg.attachments[0]
                    if not att.filename.lower().endswith((".txt", ".docx")):
                        await ctx.send("{emoji:circlex} **صيغة غير مدعومة** — أرفق ملف `TXT` أو `DOCX` فقط.")
                        return
                    chapter_text = read_upload_chapter_text(att.filename, await att.read())
                    source_stem = Path(att.filename).stem
                elif msg.content.strip():
                    chapter_text = msg.content.strip()
                else:
                    await ctx.send("{emoji:circlex} **لم أستلم ملفًا أو نصًا صالحًا.**")
                    return
    except asyncio.TimeoutError:
        await ctx.send("{emoji:clock} **انتهى الوقت، أعد الأمر مرة أخرى.**")
        return
    except Exception as exc:
        await ctx.send(f"{{emoji:circlex}} {exc}")
        return

    status_msg = await send_status(ctx.channel, "جاري الترجمة الأدبية", "استلمت الفصل وسأحدّث هذه اللوحة حتى انتهاء الترجمة.")
    await run_translation_with_status(
        status_msg,
        ctx.channel,
        user_id,
        guild_id,
        chapter_text,
        source_stem,
        target,
        profile["settings"].get("output_format", "txt"),
        thinking_enabled=thinking_enabled,
    )


@bot.command(name="setting", aliases=["settings", "اعدادات"])
async def prefix_setting(ctx):
    if V2_SUPPORTED:
        await ctx.send(view=SettingsPanel(ctx.author))
    else:
        await ctx.send(embed=themed_embed("{emoji:settings} مركز إعدادات ZEUS Text", "حدّث المكتبة إلى `discord.py>=2.6` للحصول على اللوحة التفاعلية الكاملة."))


@bot.command(name="extract", aliases=["استخراج"])
async def prefix_extract(ctx):
    user_id = ctx.author.id
    guild_id = ctx.guild.id if ctx.guild else None
    profile = get_user_profile(ctx.author)

    if profile.get("is_blocked"):
        await ctx.reply("{emoji:lock} **تم منعك من استخدام البوت.**", mention_author=False)
        return

    has_points = (guild_id and server_has_points(guild_id)) or int(profile.get("points", 0)) > 0
    if not has_points:
        await ctx.reply("{emoji:circlex} **لا تملك نقاطًا كافية.**\nكل فصل يستهلك نقطة واحدة.", mention_author=False)
        return

    settings = profile["settings"]
    view = ModeSelectView(user_id)
    await send_mode_selection(ctx, view)
    await view.wait()
    if not view.confirmed:
        await ctx.send("{emoji:clock} **انتهى وقت اختيار وضع المعالجة. أعد تشغيل الأمر عندما تكون جاهزًا.**")
        return

    await send_upload_prompt(ctx)
    try:
        msg = await bot.wait_for(
            'message',
            check=lambda m: m.author == ctx.author and m.channel == ctx.channel,
            timeout=300,
        )
    except asyncio.TimeoutError:
        await ctx.send("{emoji:clock} **انتهى الوقت، أعد الأمر مرة أخرى.**")
        return

    status_msg = await send_status(ctx.channel, "جاري قراءة المصدر", "استلمت المصدر، وسأحدّث هذه اللوحة أثناء التحميل والاستخراج.")
    images = []
    source_name = "zeus_1"

    try:
        if msg.attachments:
            if len(msg.attachments) > MAX_IMAGES_PER_REQUEST:
                await edit_status(status_msg, "{emoji:circlex} خطأ", f"الحد الأقصى {MAX_IMAGES_PER_REQUEST} صور.", error=True)
                return
            for att in msg.attachments:
                if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                    images.append((ensure_valid_image(await att.read(), att.filename), att.filename))
                elif att.filename.lower().endswith(".zip"):
                    images.extend(await asyncio.to_thread(extract_images_from_zip_bytes, await att.read()))
                    source_name = Path(att.filename).stem
                else:
                    await edit_status(status_msg, "{emoji:circlex} ملف غير مدعوم", f"`{att.filename}`", error=True)
                    return
        elif msg.content.startswith("http"):
            await edit_status(status_msg, "جاري تحميل الرابط", "قد يستغرق Google Drive وقتًا أطول.")
            link = msg.content.strip()
            if "drive.google.com" in link:
                images, source_name = await asyncio.to_thread(process_drive_link, link, include_name=True)
            else:
                data = await asyncio.to_thread(download_image_from_url, link)
                images = extract_images_from_zip_bytes(data) if zipfile.is_zipfile(io.BytesIO(data)) else [(data, "downloaded_image.jpg")]
                source_name = "zeus_1"
        else:
            await edit_status(status_msg, "{emoji:circlex} مصدر غير صالح", "أرسل صورًا أو ZIP أو رابطًا صالحًا.", error=True)
            return
    except Exception as e:
        await edit_status(status_msg, "{emoji:circlex} فشل قراءة المصدر", f"`{str(e)}`", error=True)
        return

    if not images:
        await edit_status(status_msg, "{emoji:circlex} لا توجد صور", "لم يتم العثور على صور صالحة.", error=True)
        return

    images.sort(key=lambda x: natural_sort_key(x[1]))
    extraction_started_at = time.monotonic()
    total_images = len(images)
    await edit_status(status_msg, "بدأ الاستخراج", f"تم العثور على `{total_images}` صورة.", current=0, total=total_images)

    combined_text, empty_images, failed_pages = await run_extraction_pipeline(status_msg, user_id, images, view.thinking_enabled, settings)

    # مرحلة الترجمة الأدبية (اختيارية — معطلة افتراضيًا)
    translated_text = None
    translated_error = None
    if settings.get("translate_enabled", False) and combined_text.strip():
        await edit_status(
            status_msg,
            "جاري الترجمة الأدبية",
            "أعِدت معالجة النص المستخرج أدبياً — المعالجة نصية فقط وقد تطول قليلاً حسب طول الفصل.",
        )
        try:
            translated_text = await asyncio.to_thread(translate_chapter_text, user_id, combined_text, settings)
        except Exception as exc:
            translated_error = str(exc)

    ok, profile_after, point_source = await resolve_and_consume_point(user_id, guild_id)
    if not ok:
        await edit_status(status_msg, "{emoji:circlex} لا توجد نقاط", "لا توجد نقاط كافية لإرسال النتيجة.", error=True)
        return

    await status_msg.close(delete=True)
    await deliver_extraction_result(
        ctx.channel,
        user_id,
        combined_text=combined_text,
        translated_text=translated_text,
        translated_error=translated_error,
        settings=settings,
        source_name=source_name,
        output_format=settings.get("output_format", "txt"),
        total_images=total_images,
        empty_images=empty_images,
        extraction_started_at=extraction_started_at,
        profile_after=profile_after,
        point_source=point_source,
        failed_pages=failed_pages,
    )
    increment_account_usage(user_id, "ocr")


@bot.command(name="لوحة", aliases=["admin", "ادارة"])
async def prefix_admin_panel(ctx):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    await ctx.send(embed=admin_panel_embed(), view=AdminPanelView())


@bot.command(name="عطه", aliases=["addpoints"])
async def prefix_add_points(ctx, target: str, amount: int):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    target_id = parse_user_id(target)
    profile_data = admin_adjust_user(target_id, points_delta=amount)
    await ctx.send(f"{{emoji:circlecheck}} **تمت إضافة `{amount}` نقطة. الرصيد: `{profile_data.get('points', 0)}`**")


@bot.command(name="صفر", aliases=["setpoints"])
async def prefix_set_points(ctx, target: str, amount: int = 0):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    target_id = parse_user_id(target)
    profile_data = admin_adjust_user(target_id, set_points=amount)
    await ctx.send(f"{{emoji:circlecheck}} **تم ضبط الرصيد إلى `{profile_data.get('points', 0)}`.**")


@bot.command(name="منع", aliases=["blockuser"])
async def prefix_block(ctx, target: str):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    admin_adjust_user(parse_user_id(target), blocked=True)
    await ctx.send("{emoji:lock} **تم منع المستخدم.**")


@bot.command(name="فك", aliases=["unblockuser"])
async def prefix_unblock(ctx, target: str):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    admin_adjust_user(parse_user_id(target), blocked=False)
    await ctx.send("{emoji:circlecheck} **تم فك منع المستخدم.**")


@bot.command(name="سيرفر", aliases=["serverpoints"])
async def prefix_server_points(ctx, guild_id_str: str, amount: int):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    try:
        guild_id = int(guild_id_str.strip())
    except ValueError:
        await ctx.reply("{emoji:circlex} ID السيرفر غير صحيح.", mention_author=False)
        return
    profile_data = admin_adjust_server(guild_id, points_delta=amount)
    await ctx.send(
        f"{{emoji:circlecheck}} **تمت إضافة `{amount}` نقطة للسيرفر `{guild_id}`. الرصيد: `{profile_data.get('points', 0)}`**"
    )


# ============================================================
# معالجة الأخطاء
# ============================================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"{emoji_manager.placeholder('clock')} الأمر قيد التهدئة. حاول بعد `{error.retry_after:.1f}` ثانية.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"{emoji_manager.placeholder('circlex')} حدث خطأ غير متوقع: {sanitize_error(error)}", ephemeral=True)


# ============================================================
# تشغيل البوت
# ============================================================
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("DISCORD_TOKEN مفقود من متغيرات البيئة الخاصة بـ text_bot/.env")
    try:
        emoji_manager.load()
        bot.run(BOT_TOKEN)
    finally:
        close_mongo()

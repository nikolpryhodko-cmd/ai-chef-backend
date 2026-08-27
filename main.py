"""
AI Chef — FastAPI backend entry point.

Route map (all under settings.API_PREFIX, default "/api/v1"):
  POST   /users/register                get-or-create user profile, handles referral bonus
  GET    /users/{user_id}                fetch profile
  PATCH  /users/{user_id}/settings       update language / allergies / appliances / chef persona
  POST   /users/{user_id}/trial          activate the one-time free trial bonus
  GET    /users/{user_id}/usage          today's remaining free generations
  POST   /recipes/generate               text-based recipe generation (consumes 1 daily slot)
  POST   /recipes/generate-from-photo    photo-based recipe generation (consumes 1 daily slot)
  POST   /recipes/generate-image         Imagen 3 photo of the finished dish
  GET    /links                          Wheel of Fortune + feedback form URLs
  GET    /health                         liveness probe
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from models import (
    ChefPersona,
    ExternalLinksResponse,
    GenerateImageRequest,
    GenerateImageResponse,
    Language,
    MealCategory,
    RecipeGenerateRequest,
    RecipeResponse,
    UsageStatus,
    UserProfile,
    UserRegisterRequest,
    UserSettingsUpdateRequest,
)
from services import gemini_service, user_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai_chef")

settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    description="Backend API for the AI Chef smart culinary assistant.",
    version="1.0.0",
    debug=settings.DEBUG,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PREFIX = settings.API_PREFIX


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "env": settings.APP_ENV}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@app.post(f"{PREFIX}/users/register", response_model=UserProfile, tags=["users"])
async def register_user(payload: UserRegisterRequest) -> UserProfile:
    return await user_service.get_or_create_user(payload)


@app.get(f"{PREFIX}/users/{{user_id}}", response_model=UserProfile, tags=["users"])
async def get_user(user_id: str) -> UserProfile:
    try:
        return await user_service.get_user_by_id(user_id)
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@app.patch(f"{PREFIX}/users/{{user_id}}/settings", response_model=UserProfile, tags=["users"])
async def update_user_settings(user_id: str, payload: UserSettingsUpdateRequest) -> UserProfile:
    try:
        return await user_service.update_settings(user_id, payload)
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@app.post(f"{PREFIX}/users/{{user_id}}/trial", response_model=UserProfile, tags=["users"])
async def activate_trial(user_id: str) -> UserProfile:
    try:
        return await user_service.activate_trial(user_id)
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except user_service.LimitExceededError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@app.get(f"{PREFIX}/users/{{user_id}}/usage", response_model=UsageStatus, tags=["users"])
async def get_usage(user_id: str) -> UsageStatus:
    try:
        return await user_service.get_usage_status(user_id)
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------
@app.post(f"{PREFIX}/recipes/generate", response_model=RecipeResponse, tags=["recipes"])
async def generate_recipe_from_text(payload: RecipeGenerateRequest) -> RecipeResponse:
    profile = await _get_profile_or_404(payload.user_id)

    await _consume_slot_or_409(payload.user_id)

    return await gemini_service.generate_recipe(
        ingredients_text=payload.ingredients_text,
        language=profile.language,
        chef_persona=profile.chef_persona,
        allergies=profile.allergies,
        appliances=[a.value if hasattr(a, "value") else a for a in profile.appliances],
        meal_category=payload.meal_category,
        max_cooking_minutes=payload.max_cooking_minutes,
    )


@app.post(f"{PREFIX}/recipes/generate-from-photo", response_model=RecipeResponse, tags=["recipes"])
async def generate_recipe_from_photo(
    user_id: str = Form(...),
    meal_category: MealCategory | None = Form(default=None),
    max_cooking_minutes: int | None = Form(default=None),
    photo: UploadFile = File(...),
) -> RecipeResponse:
    profile = await _get_profile_or_404(user_id)

    if not photo.content_type or not photo.content_type.startswith("image/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file must be an image.")

    image_bytes = await photo.read()
    if not image_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded image is empty.")

    await _consume_slot_or_409(user_id)

    ingredients = await gemini_service.recognize_ingredients_from_photo(image_bytes, photo.content_type)
    if not ingredients:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Couldn't identify any ingredients in that photo — try a clearer, well-lit shot.",
        )

    return await gemini_service.generate_recipe(
        ingredients_text=", ".join(ingredients),
        language=profile.language,
        chef_persona=profile.chef_persona,
        allergies=profile.allergies,
        appliances=[a.value if hasattr(a, "value") else a for a in profile.appliances],
        meal_category=meal_category,
        max_cooking_minutes=max_cooking_minutes,
    )


@app.post(f"{PREFIX}/recipes/generate-image", response_model=GenerateImageResponse, tags=["recipes"])
async def generate_dish_image(payload: GenerateImageRequest) -> GenerateImageResponse:
    await _get_profile_or_404(payload.user_id)  # ensures caller is a known user
    try:
        return await gemini_service.generate_dish_image(payload.dish_name, payload.image_prompt)
    except Exception as exc:  # pragma: no cover - upstream API failure
        logger.exception("Imagen generation failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Image generation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# External integrations (Wheel of Fortune WebApp + feedback form)
# ---------------------------------------------------------------------------
@app.get(f"{PREFIX}/links", response_model=ExternalLinksResponse, tags=["misc"])
async def get_external_links() -> ExternalLinksResponse:
    return ExternalLinksResponse(
        wheel_of_fortune_url=settings.WHEEL_OF_FORTUNE_URL,
        feedback_form_url=settings.FEEDBACK_FORM_URL,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
async def _get_profile_or_404(user_id: str) -> UserProfile:
    try:
        return await user_service.get_user_by_id(user_id)
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


async def _consume_slot_or_409(user_id: str) -> None:
    try:
        await user_service.consume_generation_slot(user_id)
    except user_service.LimitExceededError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
@app.post("/api/v1/telegram/webhook")
async def telegram_webhook(request: Request):
    """
    Telegram webhook handler.

    Supported:
    - /start [referral]  — registers the user (if needed) and replies with a welcome message.
    - /help              — shows available commands.
    - /usage             — shows today's usage status for the caller (creates user if missing).
    Any other text => short help reply.

    Replies are sent via Telegram Bot API using settings.TELEGRAM_BOT_TOKEN.
    """
    import httpx

    data = await request.json()
    logger.info("Telegram webhook received: %s", {"update": data.get("update_id")})
    logger.info("Telegram update keys: %s", list(data.keys()))

    # Accept several common update shapes
    message = data.get("message") or data.get("edited_message") or data.get("channel_post")
    callback_query = data.get("callback_query")
    my_chat_member = data.get("my_chat_member") or data.get("chat_member")

    # If it's a callback_query (inline button), the user text/command may be in callback_query['data']
    if not message and callback_query:
        message = callback_query.get("message") or {}
        if callback_query.get("data"):
            # put callback data into the message text so existing handlers continue to work
            message["text"] = callback_query["data"]

    # If it's a my_chat_member/chat_member update (bot added/started), attempt to register the user
    if not message and my_chat_member:
        logger.info("Received my_chat_member/chat_member update: %s", my_chat_member)
        chat = my_chat_member.get("chat", {})
        chat_id = chat.get("id")
        # In private chats the chat_id equals the user's Telegram ID; create the user record proactively
        if chat_id:
            tg_user_id = chat_id
            token = settings.TELEGRAM_BOT_TOKEN
            if token:
                async def _send_message_local(text_to_send: str):
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    payload = {"chat_id": chat_id, "text": text_to_send, "disable_web_page_preview": True}
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            resp = await client.post(url, json=payload)
                            resp.raise_for_status()
                    except Exception:
                        logger.exception("Failed to send message to Telegram (my_chat_member welcome)")

                try:
                    reg = UserRegisterRequest(external_id=str(tg_user_id), source="telegram")
                    profile = await user_service.get_or_create_user(reg)
                    welcome = (
                        "Привет! Я — AI Chef. Я помогу готовить из того, что у вас есть.\n\n"
                        "Доступные команды:\n"
                        "/help — список команд\n"
                        "/usage — сколько осталось бесплатных генераций сегодня\n\n"
                        "Если хотите — пришлите фото ингредиентов (через бота) или используйте мобильное приложение."
                    )
                    await _send_message_local(welcome)
                except Exception as exc:
                    logger.exception("Registration from my_chat_member failed for id=%s: %s", tg_user_id, exc)
            return {"ok": True}

    if not message:
        # nothing to do (inline_query, callback_query without message, etc.)
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text") or message.get("caption") or ""
    from_user = message.get("from", {}) or {}
    tg_user_id = from_user.get("id") or chat.get("id")

    # Log the normalized text and sender so we can see why commands didn't match
    logger.info("Telegram normalized text=%r from_user_id=%r chat_id=%r", text, tg_user_id, chat_id)

    if not chat_id:
        return {"ok": True}

    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not configured; webhook will not send replies")
        return {"ok": True}

    async def _send_message(text_to_send: str):
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text_to_send,
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
        except Exception:
            logger.exception("Failed to send message to Telegram")

    text = (text or "").strip()

    # Handle /start (optionally with referral code): "/start REF123"
    if text.startswith("/start"):
        # Telegram may send "/start <payload>" — treat second token as referral external_id
        parts = text.split(maxsplit=1)
        referral = parts[1].strip() if len(parts) > 1 else None

        if not tg_user_id:
            await _send_message("Не удалось определить ваш Telegram ID. Попробуйте ещё раз.")
            return {"ok": True}

        try:
            # Ensure user exists (and give referral bonus if provided)
            # Pass exactly the fields expected by user_service.get_or_create_user
            reg = UserRegisterRequest(
                external_id=str(tg_user_id),
                source="telegram",
                referral_code=referral,
            )
            profile = await user_service.get_or_create_user(reg)
            welcome = (
                "Привет! Я — AI Chef. Я помогу готовить из того, что у вас есть.\n\n"
                "Доступные команды:\n"
                "/help — список команд\n"
                "/usage — сколько осталось бесплатных генераций сегодня\n\n"
                "Если хотите — пришлите фото ингредиентов (через бота) или используйте мобильное приложение."
            )
            # Use language from profile to make simple short reply — here we keep a neutral RU message.
            await _send_message(welcome)
        except Exception as exc:
            # Log the full exception details and the registration payload so Render logs show the root cause
            logger.exception(
                "Registration or welcome failed while creating user external_id=%s referral=%s: %s",
                str(tg_user_id),
                referral,
                exc,
            )
            await _send_message("Ошибка при регистрации. Пожалуйста, попробуйте позже.")
        return {"ok": True}

    # Help
    if text.startswith("/help"):
        help_text = (
            "Список команд:\n"
            "/start [referral] — зарегистрироваться (при первом запуске)\n"
            "/help — показать это сообщение\n"
            "/usage — узнать оставшиеся бесплатные генерации сегодня\n\n"
            "Пока бот не умеет принимать произвольный текст для генерации рецепта. "
            "Чтобы генерировать рецепт, используйте мобильное приложение или интеграцию, "
            "либо пришлите фото ингредиентов (если бот настроен на приём файлов)."
        )
        await _send_message(help_text)
        return {"ok": True}

    # Usage status
    if text.startswith("/usage"):
        if not tg_user_id:
            await _send_message("Не удалось определить ваш Telegram ID.")
            return {"ok": True}
        try:
            reg = UserRegisterRequest(external_id=str(tg_user_id), source="telegram")
            profile = await user_service.get_or_create_user(reg)
            status = await user_service.get_usage_status(profile.id)
            usage_text = (
                f"Статус использования на {status.date}:\n"
                f"Использовано: {status.used_today}\n"
                f"Ежедневный лимит: {status.daily_limit}\n"
                f"Бонусные запросы: {status.bonus_requests}\n"
                f"Осталось: {status.remaining}\n"
                f"Сброс в: {status.resets_at}"
            )
            await _send_message(usage_text)
        except Exception as exc:
            logger.exception("Failed to fetch usage status for telegram_id=%s: %s", str(tg_user_id), exc)
            await _send_message("Ошибка при получении статуса использования. Попробуйте позже.")
        return {"ok": True}

    # Default reply for unsupported messages
    await _send_message("Я пока поддерживаю только базовые команды. Отправьте /help для списка команд.")
    return {"ok": True}

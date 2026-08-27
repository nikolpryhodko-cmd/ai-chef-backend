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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
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
    data = await request.json()
    # Здесь сервер принимает сообщения от Telegram
    return {"ok": True}

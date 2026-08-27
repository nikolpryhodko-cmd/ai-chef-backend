"""
All calls to Google's Gemini (text + vision) and Imagen (image generation)
APIs live here. Uses the official `google-genai` SDK, which ships a native
async client (`client.aio`), so no thread-offloading is needed.

The system prompts below are direct ports of the original n8n agents'
`systemMessage` fields ("Шеф-ИИ" and "Распознать блюдо"): the strict
language-lock rule, the five chef personas, and the category/time/appliance/
allergy constraints. They were translated into a single reusable prompt
builder instead of being duplicated per node.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Optional

from google import genai
from google.genai import types

from config import get_settings
from models import (
    ChefPersona,
    GenerateImageResponse,
    Language,
    MealCategory,
    RecipeResponse,
    RecipeStep,
    SafetyStatus,
)

settings = get_settings()
_client = genai.Client(api_key=settings.GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# Persona voice reference — ported from the "Шеф-ИИ" agent's systemMessage.
# ---------------------------------------------------------------------------
_PERSONA_VOICE: dict[ChefPersona, str] = {
    ChefPersona.classic: (
        "Classic chef: confident, friendly, charismatic. Warm and encouraging, "
        "no gimmicks — just solid, clear culinary guidance."
    ),
    ChefPersona.cute: (
        "Sweet Chef: warm, caring, encouraging mentor. Uses gentle pet names and "
        "little emoji touches (\u2728, \U0001F468\u200D\U0001F373, \U0001F49B); every tip feels like a hug."
    ),
    ChefPersona.michelin: (
        "Michelin Chef: an aesthete and perfectionist. Dry, polite, precise. "
        "Uses terms like 'texture', 'flavor balance', 'deconstruction', 'plating'."
    ),
    ChefPersona.rude: (
        "Rude Chef: blunt, sarcastic, a little aggressive, teases the user — but "
        "the culinary advice underneath is still genuinely good."
    ),
    ChefPersona.barinov: (
        "Parody hot-headed sports-fan chef: loud, sarcastic, over-the-top theatrical "
        "personality, but ends every rant with a genuinely brilliant cooking tip."
    ),
}

_LANGUAGE_NAME = {Language.ru: "Russian", Language.ua: "Ukrainian", Language.en: "English"}


def _language_rule(language: Language) -> str:
    # Direct port of the original's "ПРАВИЛО ЯЗЫКА (КРИТИЧЕСКИ СТРОГО)":
    # always answer in the user's configured profile language, regardless of
    # what language the user's own message/ingredients are written in.
    return (
        f"LANGUAGE RULE (STRICT): Respond ENTIRELY in {_LANGUAGE_NAME[language]}, "
        "including the dish name, description, and every step. Ignore whatever "
        "language the ingredient list is written in — always answer in the "
        "configured profile language."
    )


def _build_recipe_system_prompt(
    language: Language,
    chef_persona: ChefPersona,
    allergies: list[str],
    appliances: list[str],
    meal_category: Optional[MealCategory],
    max_cooking_minutes: Optional[int],
) -> str:
    category_rule = (
        f"The dish MUST belong to the '{meal_category.value}' category — no exceptions "
        "(e.g. never propose a heavy soup or meat dish for breakfast; desserts must be "
        "sweets/baking/fruit-based)."
        if meal_category
        else "No meal category was specified — infer a sensible one from the ingredients."
    )
    time_rule = (
        f"Total cooking time MUST NOT exceed {max_cooking_minutes} minutes."
        if max_cooking_minutes
        else "No time limit was specified — any complexity is fine."
    )
    appliances_rule = (
        f"Available kitchen appliances: {', '.join(appliances)}. Only use techniques these support."
        if appliances
        else "No appliances were specified — assume only a basic stovetop."
    )
    allergy_rule = (
        f"CRITICAL — the user is allergic to / avoids: {', '.join(allergies)}. "
        "NEVER include these ingredients or any obvious derivative of them. If the "
        "available ingredients make an allergen-free dish impossible, say so explicitly "
        "instead of generating an unsafe recipe."
        if allergies
        else "The user has no declared allergies or dietary restrictions."
    )

    return (
        "You are a professional AI chef for the 'AI Chef' app. You analyze the "
        "ingredients a user has on hand and produce one appetizing, realistic, "
        "step-by-step recipe.\n\n"
        f"{_language_rule(language)}\n\n"
        f"PERSONA — write in this voice throughout: {_PERSONA_VOICE[chef_persona]}\n\n"
        f"{category_rule}\n{time_rule}\n{appliances_rule}\n{allergy_rule}\n\n"
        "Respond ONLY with valid JSON matching the provided schema. No markdown, "
        "no commentary outside the JSON."
    )


_RECIPE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "dish_name": {"type": "STRING"},
        "description": {"type": "STRING"},
        "estimated_minutes": {"type": "INTEGER"},
        "ingredients": {"type": "ARRAY", "items": {"type": "STRING"}},
        "steps": {"type": "ARRAY", "items": {"type": "STRING"}},
        "image_prompt": {
            "type": "STRING",
            "description": (
                "English-only photorealistic prompt for image generation, following the "
                "template: 'A photorealistic delicious <DISH>, beautifully plated, warm "
                "studio lighting, shallow depth of field, 8k, mouthwatering professional "
                "food photography'. Depict only the plated dish — no people, text, or logos."
            ),
        },
    },
    "required": ["dish_name", "description", "ingredients", "steps", "image_prompt"],
}


async def generate_recipe(
    ingredients_text: str,
    language: Language,
    chef_persona: ChefPersona,
    allergies: list[str],
    appliances: list[str],
    meal_category: Optional[MealCategory],
    max_cooking_minutes: Optional[int],
) -> RecipeResponse:
    system_prompt = _build_recipe_system_prompt(
        language, chef_persona, allergies, appliances, meal_category, max_cooking_minutes
    )

    response = await _client.aio.models.generate_content(
        model=settings.GEMINI_TEXT_MODEL,
        contents=f"Available ingredients: {ingredients_text}",
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=_RECIPE_SCHEMA,
            temperature=0.9,
        ),
    )

    data = json.loads(response.text)
    safety_status, safety_notes = check_allergens(data.get("ingredients", []), allergies)

    return RecipeResponse(
        dish_name=data["dish_name"],
        description=data["description"],
        meal_category=meal_category,
        estimated_minutes=data.get("estimated_minutes"),
        ingredients=data.get("ingredients", []),
        steps=[RecipeStep(step_number=i + 1, instruction=s) for i, s in enumerate(data.get("steps", []))],
        chef_persona=chef_persona,
        language=language,
        safety_status=safety_status,
        safety_notes=safety_notes,
        image_prompt=data["image_prompt"],
    )


_INGREDIENTS_SCHEMA = {
    "type": "OBJECT",
    "properties": {"ingredients": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["ingredients"],
}


async def recognize_ingredients_from_photo(image_bytes: bytes, mime_type: str) -> list[str]:
    """
    Vision step: given a photo of a fridge/pantry/ingredients laid out on a
    counter, return a clean list of recognizable food items. Equivalent to
    the original workflow's photo-input branch feeding into the "Шеф-ИИ" agent.
    """
    response = await _client.aio.models.generate_content(
        model=settings.GEMINI_VISION_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            "List every distinct food ingredient visible in this photo.",
        ],
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are a food-recognition module for a cooking app. Identify every "
                "distinct edible ingredient visible in the photo (fridge contents, pantry "
                "items, or ingredients laid on a counter). Ignore packaging text, brand "
                "logos, and non-food objects. Respond ONLY with valid JSON."
            ),
            response_mime_type="application/json",
            response_schema=_INGREDIENTS_SCHEMA,
            temperature=0.2,
        ),
    )
    data = json.loads(response.text)
    return data.get("ingredients", [])


async def generate_dish_image(dish_name: str, image_prompt: str) -> GenerateImageResponse:
    """Generates a photorealistic image of the finished dish via Imagen 3."""
    result = await _client.aio.models.generate_images(
        model=settings.IMAGEN_MODEL,
        prompt=image_prompt,
        config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="1:1"),
    )
    image_bytes = result.generated_images[0].image.image_bytes
    return GenerateImageResponse(
        dish_name=dish_name,
        image_base64=base64.b64encode(image_bytes).decode("ascii"),
        mime_type="image/png",
    )


# ---------------------------------------------------------------------------
# Allergen / safety check — simplified port of the "Проверка безопасности"
# code node: canonicalizes ingredient names via aliases and flags any that
# collide with the user's declared allergies.
# ---------------------------------------------------------------------------
_ALIASES: dict[str, list[str]] = {
    "milk": ["milk", "whole milk", "cream", "sour cream"],
    "cheese": ["cheese", "hard cheese", "cottage cheese"],
    "yogurt": ["yogurt", "greek yogurt", "kefir"],
    "egg": ["egg", "eggs"],
    "fish": ["fish", "salmon", "tuna", "herring"],
    "seafood": ["shrimp", "mussels", "squid", "seafood"],
    "nuts": ["nuts", "peanut", "peanuts", "almond", "walnut", "hazelnut", "cashew"],
    "gluten": ["wheat", "flour", "bread", "pasta", "gluten"],
    "soy": ["soy", "soy sauce", "tofu", "edamame"],
    "alcohol": ["wine", "beer", "vodka", "rum", "cognac", "liqueur", "alcohol"],
}


def _canonicalize(terms: list[str]) -> set[str]:
    canon: set[str] = set()
    blob = " ".join(t.lower() for t in terms)
    for key, variants in _ALIASES.items():
        if any(re.search(rf"\b{re.escape(v)}\b", blob) for v in variants):
            canon.add(key)
    return canon


def check_allergens(recipe_ingredients: list[str], user_allergies: list[str]) -> tuple[SafetyStatus, list[str]]:
    if not user_allergies:
        return SafetyStatus.allow, []

    recipe_canon = _canonicalize(recipe_ingredients)
    allergy_canon = _canonicalize(user_allergies)
    hits = recipe_canon & allergy_canon

    if hits:
        return (
            SafetyStatus.block,
            [f"Recipe contains '{h}', which conflicts with a declared allergy/restriction." for h in sorted(hits)],
        )
    return SafetyStatus.allow, []

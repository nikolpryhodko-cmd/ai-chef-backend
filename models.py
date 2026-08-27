"""
Pydantic v2 schemas shared by the API layer and the service layer.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums — mirrors the fixed option sets from the original bot's menus
# ---------------------------------------------------------------------------
class Language(str, Enum):
    ru = "ru"
    ua = "ua"
    en = "en"


class ChefPersona(str, Enum):
    classic = "classic"       # Классический уверенный, дружелюбный шеф
    cute = "cute"              # Милый, заботливый шеф
    michelin = "michelin"      # Шеф-Мишлен: эстет и перфекционист
    rude = "rude"              # Токсичный / грубый шеф
    barinov = "barinov"        # Пародийный саркастичный персонаж


class Appliance(str, Enum):
    oven = "oven"
    blender = "blender"
    air_fryer = "air_fryer"
    multicooker = "multicooker"
    microwave = "microwave"
    stovetop = "stovetop"
    grill = "grill"


class MealCategory(str, Enum):
    breakfast = "breakfast"
    lunch = "lunch"
    dinner = "dinner"
    dessert = "dessert"
    snack = "snack"


class SafetyStatus(str, Enum):
    allow = "ALLOW"
    allow_with_note = "ALLOW_WITH_NOTE"
    review = "REVIEW"
    block = "BLOCK"


# ---------------------------------------------------------------------------
# User / profile
# ---------------------------------------------------------------------------
class UserRegisterRequest(BaseModel):
    """Called once when a client (mobile app or Telegram) first opens the app."""
    external_id: str = Field(..., description="Stable client identifier: telegram_id or device/auth id")
    source: str = Field(default="app", description="'app' or 'telegram'")
    referral_code: Optional[str] = Field(default=None, description="external_id of the referring user, if any")
    language: Language = Language.en


class UserSettingsUpdateRequest(BaseModel):
    language: Optional[Language] = None
    allergies: Optional[list[str]] = None
    appliances: Optional[list[Appliance]] = None
    chef_persona: Optional[ChefPersona] = None


class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    external_id: str
    language: Language
    allergies: list[str] = Field(default_factory=list)
    appliances: list[Appliance] = Field(default_factory=list)
    chef_persona: ChefPersona = ChefPersona.classic
    is_premium: bool = False
    premium_expires_at: Optional[datetime] = None
    trial_used: bool = False
    referred_by: Optional[str] = None
    created_at: Optional[datetime] = None


class UsageStatus(BaseModel):
    date: str
    used_today: int
    daily_limit: int
    bonus_requests: int
    remaining: int
    is_premium: bool
    resets_at: str = Field(description="ISO timestamp of the next midnight reset (server's local day boundary, UTC)")


# ---------------------------------------------------------------------------
# Recipe generation
# ---------------------------------------------------------------------------
class RecipeGenerateRequest(BaseModel):
    """
    Text-only generation request. For photo-based generation use the
    multipart /recipes/generate-from-photo endpoint instead.
    """
    user_id: str
    ingredients_text: str = Field(..., min_length=1, description="Free-form list of available ingredients")
    meal_category: Optional[MealCategory] = None
    max_cooking_minutes: Optional[int] = Field(default=None, gt=0)


class RecipeStep(BaseModel):
    step_number: int
    instruction: str


class RecipeResponse(BaseModel):
    dish_name: str
    description: str
    meal_category: Optional[MealCategory] = None
    estimated_minutes: Optional[int] = None
    ingredients: list[str]
    steps: list[RecipeStep]
    chef_persona: ChefPersona
    language: Language
    safety_status: SafetyStatus
    safety_notes: list[str] = Field(default_factory=list)
    image_prompt: str = Field(description="English prompt ready for the image generation endpoint")


class GenerateImageRequest(BaseModel):
    user_id: str
    dish_name: str
    image_prompt: str


class GenerateImageResponse(BaseModel):
    dish_name: str
    image_base64: str
    mime_type: str = "image/png"


class ExternalLinksResponse(BaseModel):
    wheel_of_fortune_url: str
    feedback_form_url: str

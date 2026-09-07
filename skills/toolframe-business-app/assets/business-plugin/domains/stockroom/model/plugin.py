"""One concrete implementation of the Stockroom business capability."""

from fastapi_cordis import Plugin
from pydantic import BaseModel, ConfigDict, Field
from toolframe_database_service import DATABASE

from .contract import STOCKROOM
from .service import StockroomService


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_delivery_quantity: int = Field(default=1000, ge=1, le=100_000)


def setup(context):
    database = context.require(DATABASE)
    # The database plugin applies domain schemas before this dependency starts.
    database.query("SELECT id FROM stockroom_deliveries LIMIT 0")
    return StockroomService(database, context.config.max_delivery_quantity)


PLUGIN = Plugin(
    "stockroom.local",
    setup,
    requires=(DATABASE,),
    provides=STOCKROOM,
    config_model=Settings,
    description="Record deliveries once and report stock totals",
)

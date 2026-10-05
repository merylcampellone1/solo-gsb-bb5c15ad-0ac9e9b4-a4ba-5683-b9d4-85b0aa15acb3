"""Pydantic request/response models.

Concentrations are exact rationals: JSON strings like "2/3" or "0.125", or
JSON integers.  JSON floating-point numbers are deliberately rejected because
they cannot represent the exact concentrations the protocol requires.
Volumes are always integer nanoliters.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------- #
# Concentration type
# --------------------------------------------------------------------------- #


class Concentration:
    """Wrapper validating rational concentration input into Fraction."""

    @classmethod
    def validate(cls, value, field_name: str = "concentration") -> Fraction:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be a string or integer, not bool")
        if isinstance(value, Fraction):
            f = value
        elif isinstance(value, int):
            f = Fraction(value)
        elif isinstance(value, str):
            s = value.strip()
            if not s:
                raise ValueError(f"{field_name} must not be empty")
            try:
                f = Fraction(s)
            except (ValueError, ZeroDivisionError):
                raise ValueError(
                    f"{field_name} must be a rational number, e.g. '2/3', '0.25', '5'"
                )
        elif isinstance(value, float):
            raise ValueError(
                f"{field_name} must be a string or integer; "
                f"floats are not accepted (use '0.1' instead of 0.1)"
            )
        else:
            raise ValueError(f"{field_name} has an unsupported type")
        if f < 0:
            raise ValueError(f"{field_name} must be non-negative")
        # Store canonically as a string; the solver converts to Fraction.
        return str(f)


# --------------------------------------------------------------------------- #
# Request pieces
# --------------------------------------------------------------------------- #


class StockIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    concentration: Union[str, int]
    available_nl: int = Field(gt=0)

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if v.startswith("@"):
            raise ValueError("ids must not start with '@' (reserved prefix)")
        return v

    @field_validator("concentration")
    @classmethod
    def _conc(cls, v):
        return Concentration.validate(v)


class DiluentIn(BaseModel):
    available_nl: int = Field(ge=0)


class WellIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    final_volume_nl: int = Field(gt=0)
    final_concentration: Union[str, int]
    capacity_nl: Optional[int] = Field(default=None, gt=0)

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if v.startswith("@"):
            raise ValueError("ids must not start with '@' (reserved prefix)")
        return v

    @field_validator("final_concentration")
    @classmethod
    def _conc(cls, v):
        return Concentration.validate(v, "final_concentration")


class ContainerIn(BaseModel):
    id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    capacity_nl: int = Field(gt=0)

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if v is None:
            return v
        if v.startswith("@"):
            raise ValueError("ids must not start with '@' (reserved prefix)")
        return v


class PipetteIn(BaseModel):
    min_nl: int = Field(gt=0)
    max_nl: int = Field(gt=0)
    step_nl: int = Field(gt=0)


class LimitsIn(BaseModel):
    max_depth: int = Field(default=3, ge=1, le=6)
    max_search_nodes: int = Field(default=200_000, ge=100, le=20_000_000)
    max_enumeration_units: int = Field(default=20_000, ge=10, le=2_000_000)
    max_factor: int = Field(default=200_000, ge=2, le=100_000_000)


class PlanRequest(BaseModel):
    stocks: list[StockIn] = Field(min_length=1)
    diluent: DiluentIn
    wells: list[WellIn] = Field(min_length=1)
    containers: list[ContainerIn] = Field(default_factory=list)
    pipette: PipetteIn
    default_well_capacity_nl: int = Field(gt=0)
    limits: LimitsIn = Field(default_factory=LimitsIn)


# --------------------------------------------------------------------------- #
# Inventory mode (registered stock batches)
# --------------------------------------------------------------------------- #


class BatchRegisterIn(BaseModel):
    """Registration of a physical stock batch."""
    id: str = Field(min_length=1, max_length=64)
    concentration: Union[str, int]
    initial_volume_nl: int = Field(gt=0)

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if v.startswith("@"):
            raise ValueError("ids must not start with '@' (reserved prefix)")
        return v

    @field_validator("concentration")
    @classmethod
    def _conc(cls, v):
        return Concentration.validate(v)


class InventoryPlanIn(BaseModel):
    """Plan request that draws from registered batches instead of
    request-local stock definitions."""
    batch_ids: list[str] = Field(min_length=1)
    diluent: DiluentIn
    wells: list[WellIn] = Field(min_length=1)
    containers: list[ContainerIn] = Field(default_factory=list)
    pipette: PipetteIn
    default_well_capacity_nl: int = Field(gt=0)
    limits: LimitsIn = Field(default_factory=LimitsIn)

    @field_validator("batch_ids")
    @classmethod
    def _batch_ids(cls, v):
        for b in v:
            if not b or len(b) > 64:
                raise ValueError("batch ids must be 1..64 characters")
            if b.startswith("@"):
                raise ValueError(
                    "ids must not start with '@' (reserved prefix)")
        return v


class RecallIn(BaseModel):
    """Irrevocable recall of a contaminated stock batch.

    The batch id is carried in the URL path; the recall is identified by its
    (batch, reason) pair: repeating the same reason replays the original
    result, a different reason conflicts.
    """
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v):
        s = v.strip()
        if not s:
            raise ValueError("reason must not be empty")
        return s


# --------------------------------------------------------------------------- #
# Response pieces
# --------------------------------------------------------------------------- #


class NodeRef(BaseModel):
    kind: str
    id: Optional[str] = None
    container: Optional[str] = None


class TransferOut(BaseModel):
    step: int
    source: NodeRef
    destination: NodeRef
    volume_nl: int


class RecipeOut(BaseModel):
    id: str
    container: str
    concentration: str
    produced_nl: int
    remaining_nl: int
    recipe: dict


class WellResultOut(BaseModel):
    id: str
    final_volume_nl: int
    final_concentration: str


class PlanPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    transfers: list[TransferOut]
    intermediates: list[RecipeOut]
    wells: list[WellResultOut]

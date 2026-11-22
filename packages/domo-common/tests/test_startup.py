"""Start-up helpers shared by the core and the gatekeeper."""

import logging
import uuid
from typing import Self

import pytest
from domo_common.startup import describe_settings_error, pin_loggers_at_warning
from pydantic import BaseModel, Field, ValidationError, model_validator


class _Settings(BaseModel):
    data_dir: str
    public_port: int = Field(ge=1, le=65535)


def test_a_settings_error_names_each_variable_without_echoing_its_value():
    with pytest.raises(ValidationError) as excinfo:
        _Settings.model_validate({"public_port": "not-a-port-0b7f"})
    description = describe_settings_error(excinfo.value)
    assert "DOMO_DATA_DIR:" in description and "DOMO_PUBLIC_PORT:" in description
    assert "not-a-port-0b7f" not in description
    assert description.count(";") == 1


def test_an_error_without_a_location_is_about_the_settings_as_a_whole():
    class _Ports(BaseModel):
        a: int
        b: int

        @model_validator(mode="after")
        def _distinct(self) -> Self:
            if self.a == self.b:
                raise ValueError("the two ports must differ")
            return self

    with pytest.raises(ValidationError) as excinfo:
        _Ports(a=1, b=1)
    assert describe_settings_error(excinfo.value).startswith("settings: ")


def test_pinned_loggers_stay_at_warning_or_above():
    lax, strict = f"test-lax-{uuid.uuid4().hex}", f"test-strict-{uuid.uuid4().hex}"
    logging.getLogger(lax).setLevel(logging.DEBUG)
    logging.getLogger(strict).setLevel(logging.ERROR)
    pin_loggers_at_warning((lax, strict))
    assert logging.getLogger(lax).level == logging.WARNING
    # A logger that is already stricter is left alone.
    assert logging.getLogger(strict).level == logging.ERROR

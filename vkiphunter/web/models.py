"""Strict validation at the HTTP boundary."""
from typing import Annotated, Literal
from ipaddress import IPv4Address

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..cidr import validate_networks

Text = Annotated[str, Field(max_length=256)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Subnet(Input):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    cidr: Text = ""


class HuntInput(Input):
    mode: Literal["port", "floatingip"]
    network: Annotated[str, Field(min_length=1, max_length=128)]
    prefix: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")]
    interval: int = Field(ge=1, le=86400)
    workers: int = Field(ge=1, le=8)
    port_timeout: int = Field(ge=10, le=600)
    server_timeout: int = Field(ge=60, le=1800)
    targets: list[Text] = Field(min_length=1, max_length=256)
    excludes: list[Text] = Field(max_length=256)
    subnets: list[Subnet] = Field(max_length=64)
    report_every: int = Field(ge=1, le=10000)
    max_attempts: int = Field(ge=0, le=10000000)
    volume_size: int = Field(ge=1, le=10000)
    volume_type: Text

    @field_validator("targets", "excludes")
    @classmethod
    def cidrs(cls, values):
        result = validate_networks(values)
        if any(":" in value for value in result):
            raise ValueError("поддерживается только IPv4")
        return result


class ConnectionInput(Input):
    method: Literal["password", "appcred", "simulate"]
    openrc: Annotated[str, Field(max_length=32768)] = ""
    auth_url: Text = ""
    region: Text = "RegionOne"
    username: Text = ""
    password: Annotated[str, Field(max_length=4096)] = ""
    project_id: Text = ""
    project_name: Text = ""
    user_domain_name: Text = "users"
    app_cred_id: Text = ""
    app_cred_secret: Annotated[str, Field(max_length=4096)] = ""


class LoginInput(Input):
    password: Annotated[str, Field(min_length=1, max_length=4096)]


class ConfirmInput(Input):
    confirm: Literal[True]


class CreateVM(ConfirmInput):
    port_id: Text
    name: Annotated[str, Field(min_length=1, max_length=128)]
    flavor_id: Annotated[str, Field(min_length=1, max_length=128)]
    image_id: Annotated[str, Field(min_length=1, max_length=128)]
    key_name: Text = ""


class AttachIP(ConfirmInput):
    port_id: Text
    server_id: Annotated[str, Field(min_length=1, max_length=128)]


class DeleteIP(ConfirmInput):
    resource_id: Annotated[str, Field(min_length=1, max_length=128)]
    kind: Literal["port", "floatingip"]
    ip: Text

    @field_validator('ip')
    @classmethod
    def ipv4(cls, value):
        return str(IPv4Address(value))

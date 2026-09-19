"""Version-bound access to previously extracted public web evidence."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class WebSourceAccess(BaseModel):
    """Coverage of one exact extracted snapshot, not of the original whole page."""

    source_id: str = Field(min_length=1, max_length=200)
    selected_url: str = Field(min_length=1, max_length=2048)
    resolved_url: str = Field(min_length=1, max_length=2048)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_chars: int = Field(ge=0)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    next_start_char: int | None = Field(default=None, ge=0)
    content_complete: bool = False
    available: bool = True
    read_tool: Literal["read_web_source_window"] = "read_web_source_window"
    limitation: str = "Coverage describes extracted text only; linked references are not fetched."

    @model_validator(mode="after")
    def validate_coverage(self) -> "WebSourceAccess":
        if not self.start_char <= self.end_char <= self.total_chars:
            raise ValueError("Web source window must be within the extracted snapshot.")
        expected_next = (
            self.end_char if self.available and self.end_char < self.total_chars else None
        )
        if self.next_start_char != expected_next:
            raise ValueError("Web continuation must start at the end of the retained window.")
        if self.content_complete and not (
            self.start_char == 0 and self.end_char == self.total_chars
        ):
            raise ValueError("Complete web evidence must cover the entire extracted snapshot.")
        return self

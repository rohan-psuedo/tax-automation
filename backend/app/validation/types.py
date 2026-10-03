from typing import Literal

from pydantic import BaseModel

Severity = Literal["error", "warning"]


class Issue(BaseModel):
    """One finding shown on the review screen.

    error   -> blocks posting until fixed.
    warning -> posting allowed, but the entry goes to review instead of straight to ready.
    """

    code: str  # stable machine code, e.g. "totals_mismatch"
    severity: Severity
    message: str  # plain sentence for the reviewer, says what is wrong and what to do
    field: str | None = None  # invoice field path it concerns, e.g. "grand_total"


class ValidationReport(BaseModel):
    issues: list[Issue]
    confidence: float  # 0..1 overall
    route: Literal["ready", "needs_review"]

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

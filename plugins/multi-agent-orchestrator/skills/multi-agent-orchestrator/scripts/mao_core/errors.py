from __future__ import annotations


ERROR_CODES: frozenset[str] = frozenset(
    {
        "CLI_NOT_FOUND",
        "AUTH_REQUIRED",
        "AUTH_EXPIRED",
        "MODEL_LIST_UNSUPPORTED",
        "MODEL_UNAVAILABLE",
        "SESSION_START_FAILED",
        "QUOTA_EXHAUSTED",
        "NETWORK_ERROR",
        "TIMEOUT",
        "INVALID_RESULT",
        "CRITIC_MUTATED_WORKSPACE",
        "REVIEW_BUDGET_EXHAUSTED",
        "USAGE_UNAVAILABLE",
        "CONFIG_INVALID",
        "INSTALL_INVALID",
        "PACKET_PATH_INVALID",
        "STATE_TRANSITION_INVALID",
    }
)


class MaoError(Exception):
    def __init__(self, code: str, message: str, details: dict):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details)

    def as_dict(self):
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }

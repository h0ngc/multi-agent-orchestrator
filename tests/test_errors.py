from mao_core.errors import ERROR_CODES, MaoError


def test_stable_error_codes_cover_public_contract():
    assert {
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
        "PACKET_PATH_INVALID",
        "STATE_TRANSITION_INVALID",
    } <= ERROR_CODES
    assert isinstance(ERROR_CODES, frozenset)


def test_mao_error_serializes_public_error_envelope():
    error = MaoError("CONFIG_INVALID", "Invalid configuration", {"key": "MAO_X"})

    assert isinstance(error, Exception)
    assert str(error) == "Invalid configuration"
    assert error.as_dict() == {
        "error": {
            "code": "CONFIG_INVALID",
            "message": "Invalid configuration",
            "details": {"key": "MAO_X"},
        }
    }

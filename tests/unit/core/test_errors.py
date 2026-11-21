from crewgate.core.errors import ErrorCategory, PartnerError


def test_5xx_maps_to_upstream_and_is_retryable():
    e = PartnerError.from_status(503, "down")
    assert e.category is ErrorCategory.UPSTREAM_UNAVAILABLE and e.retryable


def test_410_gone_is_conflict():
    assert PartnerError.from_status(410, "offer expired").category is ErrorCategory.CONFLICT

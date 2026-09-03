"""Los tests de invariantes reutilizan las fixtures de integración."""

from tests.integration.conftest import (  # noqa: F401
    ApiUser,
    auth_client,
    make_user,
    other_user,
    user,
)
